"""Request counters and a log ring buffer for the developer dashboard."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Deque, Dict, List, Optional, Tuple

from aiohttp import web

from unshackle.core import __code_hash__, __version__
from unshackle.core.api.events import bus
from unshackle.core.config import config
from unshackle.core.utils.redact import redact_secrets

RATE_LIMIT_WINDOW = 3600.0
CDM_OPS = frozenset(
    {
        "open",
        "close",
        "set_service_certificate",
        "get_service_certificate",
        "get_license_challenge",
        "parse_license",
        "get_keys",
    }
)
CDM_ERROR_LIMIT = 300
CDM_SESSION_ID_LIMIT = 64
CDM_DEVICE_LIMIT = 255


def _users() -> Dict[str, Any]:
    users = (config.serve or {}).get("users")
    return users if isinstance(users, dict) else {}


def _user_config(key: Optional[str]) -> Optional[Dict[str, Any]]:
    user = _users().get(key) if key else None
    return user if isinstance(user, dict) else None


def configured_key(key: Optional[str]) -> Optional[str]:
    """``key`` when the config knows it (a user API key, the master secret or the dashboard API key), else None.

    Open routes answer 200 to any header value, so attributing by the raw header would let an
    unauthenticated caller grow ``stats.keys`` without bound.
    """
    if not key:
        return None
    serve = config.serve or {}
    dashboard = serve.get("dashboard")
    known = {str(k) for k in _users()} | {str(serve.get("api_secret") or "")}
    if isinstance(dashboard, dict) and dashboard.get("key"):
        known.add(str(dashboard["key"]))
    return key if key in known else None


def mask_key(key: Optional[str]) -> str:
    """A display label for an API key: its configured username, else a truncated prefix."""
    if not key:
        return "anonymous"
    user = _user_config(key)
    if user and user.get("username"):
        return str(user["username"])
    return key[:4] + "…"


def key_id(key: Optional[str]) -> str:
    """A stable id for an API key. A hash prefix, so two unnamed keys never collide the way
    ``mask_key`` does, and the id carries no key material."""
    if not key:
        return "anonymous"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def key_tier(key: Optional[str]) -> Optional[str]:
    """The tier name an API key references, when it names one that exists."""
    user = _user_config(key)
    tier = user.get("tier") if user else None
    return str(tier) if tier else None


def key_rate_limit(key: Optional[str]) -> Optional[int]:
    """Requests per hour allowed for a key: its own override, else its tier's, else unlimited."""
    user = _user_config(key)
    if user is None:
        return None
    own = user.get("rate_limit")
    if isinstance(own, int) and own > 0:
        return own
    tiers = (config.serve or {}).get("tiers")
    tier = tiers.get(user.get("tier")) if isinstance(tiers, dict) and user.get("tier") else None
    limit = tier.get("rate_limit") if isinstance(tier, dict) else None
    return limit if isinstance(limit, int) and limit > 0 else None


def rate_limit_error(where: str, value: Any) -> Optional[str]:
    """Why *value* cannot serve as a rate limit, or None when it can. Unset is fine.

    ``key_rate_limit`` reads anything else as "no limit", so a mistyped value would silently
    remove the cap the operator wrote down. It lives beside the reader so the two rules cannot
    drift apart.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return f"{where}.rate_limit must be a positive whole number of requests per hour, not {value!r}"
    return None


@dataclass
class KeyStats:
    """Per-key counters and the current rate-limit window."""

    label: str
    requests: int = 0
    rejected: int = 0
    bytes_out: int = 0
    last_seen: float = 0.0
    window_start: float = 0.0
    window_used: int = 0


@dataclass
class ServerStats:
    started_at: float = field(default_factory=time.time)
    host: str = ""
    port: int = 0
    mode: str = "full"
    requests_total: int = 0
    requests_rejected: int = 0
    keys: Dict[str, KeyStats] = field(default_factory=dict)

    def key_stats(self, key: Optional[str]) -> KeyStats:
        """The counters for an API key, created on first sight."""
        kid = key_id(key)
        entry = self.keys.get(kid)
        if entry is None:
            entry = KeyStats(label=mask_key(key))
            self.keys[kid] = entry
        else:
            entry.label = mask_key(key)
        return entry

    @property
    def requests_by_key(self) -> Counter:
        """Legacy label-keyed request counts. Two keys sharing a masked label still merge here;
        the dashboard reads /api/dashboard/keys for unambiguous per-key figures."""
        counter: Counter = Counter()
        for entry in self.keys.values():
            counter[entry.label] += entry.requests
        return counter

    def check_rate_limit(self, key: str) -> Optional[int]:
        """Seconds to wait when ``key`` is over its hourly limit, else None after counting the request.

        A fixed window, not a sliding one: cheap, and an operator cap does not need the precision.
        """
        limit = key_rate_limit(key)
        if limit is None:
            return None
        entry = self.key_stats(key)
        now = time.time()
        if now - entry.window_start >= RATE_LIMIT_WINDOW:
            entry.window_start = now
            entry.window_used = 0
        if entry.window_used >= limit:
            return max(1, int(entry.window_start + RATE_LIMIT_WINDOW - now))
        entry.window_used += 1
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": __version__,
            "code_hash": __code_hash__,
            "host": self.host,
            "port": self.port,
            "mode": self.mode,
            "started_at": self.started_at,
            "uptime_seconds": int(time.time() - self.started_at),
            "requests_total": self.requests_total,
            "requests_rejected": self.requests_rejected,
            "requests_by_key": dict(self.requests_by_key),
        }


stats = ServerStats()


@web.middleware
async def stats_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    from unshackle.core.api.handlers import request_secret_key

    stats.requests_total += 1
    started = time.perf_counter()
    response = await handler(request)
    status = response.get("late_status", response.status)
    bytes_out = getattr(response, "content_length", None) or response.body_length
    if status == 401:
        stats.requests_rejected += 1
    else:
        entry = stats.key_stats(configured_key(request_secret_key(request)))
        entry.requests += 1
        entry.last_seen = time.time()
        entry.bytes_out += bytes_out
        if status >= 400:
            entry.rejected += 1
    session_id = request.match_info.get("session_id")
    if session_id and request.method != "OPTIONS" and not request.path.endswith("/logs"):
        from unshackle.core.api.session_store import get_session_store

        get_session_store().record_action(
            session_id,
            {
                "ts": time.time(),
                "method": request.method,
                "action": request.path.split(f"/{session_id}", 1)[-1].strip("/") or "info",
                "query": dict(request.query),
                "status": status,
                "ms": round((time.perf_counter() - started) * 1000, 1),
                "bytes_in": request.content_length or 0,
                "bytes_out": bytes_out,
            },
        )
    return response


class SeqRing:
    """Keep the last N records in memory, each with a ``seq`` one above the record before it."""

    def __init__(self, maxlen: int) -> None:
        self.records: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self.seq = 0

    def append(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Buffer one record; ``seq`` moves only after the append, as in ``EventBus``."""
        seq = self.seq + 1
        item = {"seq": seq, **record}
        self.records.append(item)
        self.seq = seq
        return item

    def since(self, seq: int = 0) -> List[Dict[str, Any]]:
        """The records after ``seq``, oldest first, from a copy: ``append()`` can run on a worker thread."""
        return [r for r in self.records.copy() if r["seq"] > seq]


class RingLogHandler(SeqRing, logging.Handler):
    """Keep the last N log records in memory and publish each one on the event bus."""

    def __init__(self, maxlen: int = 1000) -> None:
        logging.Handler.__init__(self)
        SeqRing.__init__(self, maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer and publish one record."""
        item = self.append(
            {
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "msg": redact_secrets(self.format(record)),
            }
        )
        bus.publish("log", item)

    def since(self, seq: int = 0, level: Optional[str] = None, logger: Optional[str] = None) -> List[Dict[str, Any]]:
        """The records after ``seq`` at or above ``level``, from ``logger`` or one of its children."""
        min_level = logging.getLevelName(level.upper()) if level else 0
        if not isinstance(min_level, int):
            min_level = 0
        return [
            r
            for r in super().since(seq)
            if logging.getLevelName(r["level"]) >= min_level
            and (not logger or r["logger"] == logger or r["logger"].startswith(logger + "."))
        ]


ring = RingLogHandler()

cdm_calls = SeqRing(maxlen=5000)


@lru_cache(maxsize=None)
def _cdm_op(pattern: str) -> Optional[Tuple[str, str]]:
    """The ``(drm, op)`` of a route pattern, else None. Cached: the route table does not change."""
    parts = pattern.strip("/").split("/")
    drm = "widevine"
    if parts[:1] == ["playready"]:
        drm, parts = "playready", parts[1:]
    if len(parts) < 2 or parts[0] != "{device}" or parts[1] not in CDM_OPS:
        return None
    return drm, parts[1]


def _cdm_route(request: web.Request) -> Optional[Tuple[str, str]]:
    """The ``(drm, op)`` of a CDM device route, else None.

    Reads the matched route pattern, not the path, so a device name cannot pass for an operation
    or for the ``/playready`` prefix.
    """
    resource = request.match_info.route.resource
    return _cdm_op(resource.canonical) if resource is not None else None


def _hex_id(value: Any) -> Optional[str]:
    """``value`` in lowercase when it is a hexadecimal string of a plausible length, else None."""
    if not isinstance(value, str) or not value or len(value) > CDM_SESSION_ID_LIMIT:
        return None
    try:
        bytes.fromhex(value)
    except ValueError:
        return None
    return value.lower()


def _key_fields(secret_key: Optional[str]) -> Dict[str, str]:
    """The ``key_id`` and ``user`` of an API key: the ``id`` and ``label`` of its ``stats.keys`` entry."""
    key = configured_key(secret_key)
    return {"key_id": key_id(key), "user": mask_key(key)}


def _reply_json(response: Any) -> Dict[str, Any]:
    """The JSON object a handler answered, or ``{}``. ``compression_middleware`` runs inside the
    CDM log middleware, so the body can arrive as gzip."""
    try:
        body = response.body
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        data = json.loads(body)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def _request_session_id(request: web.Request) -> Any:
    """The ``session_id`` of the JSON request body, when the handler consumed it: aiohttp caches that read.

    A body nobody read belongs to a call refused before the handler, a 401 or a 429. It stays
    unread: reading it would spend time and memory on an unauthenticated caller, and wait on a
    body that never finishes arriving.
    """
    if not request.content.at_eof():
        return None
    try:
        body = await request.json()
    except Exception:
        return None
    return body.get("session_id") if isinstance(body, dict) else None


async def _cdm_record(request: web.Request, drm: str, op: str, outcome: Any) -> Dict[str, Any]:
    """The record of one call. ``outcome`` is the response, or the exception the handler raised.

    An exception that is not an HTTP one gives its class name only: its text can hold the
    presented API key. The reply body is read only for a refusal and for the session id of an
    ``open``, so a challenge, a licence or a content key never passes through here.
    """
    from unshackle.core.api.handlers import request_secret_key

    reply: Dict[str, Any] = {}
    if isinstance(outcome, web.StreamResponse):
        status = outcome.status
        if status >= 400 or op == "open":
            reply = _reply_json(outcome)
        message = reply.get("message")
        error = message if isinstance(message, str) else outcome.reason
    else:
        status, error = 500, type(outcome).__name__
    ok = status < 400
    if op == "open":
        data = reply.get("data") if ok else None
        session_id = data.get("session_id") if isinstance(data, dict) else None
    elif op == "close":
        session_id = request.match_info.get("session_id")
    else:
        session_id = await _request_session_id(request)
    return {
        "drm": drm,
        "op": op,
        "device": request.match_info["device"][:CDM_DEVICE_LIMIT],
        **_key_fields(request_secret_key(request)),
        "session_id": _hex_id(session_id),
        "status": status,
        "ok": ok,
        "error": None if ok else str(error)[:CDM_ERROR_LIMIT],
    }


async def _record_cdm_call(
    request: web.Request, route: Tuple[str, str], ts: float, started: float, outcome: Any
) -> None:
    """Append one call to ``cdm_calls``. Logs a failure and does not raise it: it must never fail the CDM call."""
    ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        cdm_calls.append({"ts": ts, **await _cdm_record(request, *route, outcome), "ms": ms})
    except Exception:
        logging.getLogger("serve").debug("CDM call not recorded", exc_info=True)


@web.middleware
async def cdm_log_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Record each call to a CDM device route in ``cdm_calls``.

    Must run outside the serve authentication middleware, so the log holds a 401 too.
    """
    route = _cdm_route(request)
    if route is None:
        return await handler(request)
    ts, started = time.time(), time.perf_counter()
    try:
        response = await handler(request)
    except Exception as e:
        await _record_cdm_call(request, route, ts, started, e)
        raise
    await _record_cdm_call(request, route, ts, started, response)
    return response


def cdm_sessions(app: web.Application) -> List[Dict[str, Any]]:
    """One row per live Cdm object: its open session count and the most it allows.

    Both CDM libraries refuse an ``open`` only above ``MAX_NUM_OF_SESSIONS``, so the true limit
    is one more. A Cdm with a ``SESSION_TIMEOUT`` drops older sessions on its next ``open``,
    so those do not count as open.
    """
    now = time.time()
    rows: List[Dict[str, Any]] = []
    for drm, owner in (("widevine", app), ("playready", app.get("playready_app"))):
        cdms = owner.get("cdms") if owner is not None else None
        for (secret_key, device), cdm in (cdms or {}).items():
            sessions = getattr(cdm, "_Cdm__sessions", {}).values()
            timeout = getattr(cdm, "SESSION_TIMEOUT", None)
            rows.append(
                {
                    "drm": drm,
                    "device": device,
                    **_key_fields(secret_key),
                    "open": len(sessions) if timeout is None else sum(now - s.opened_at <= timeout for s in sessions),
                    "max": cdm.MAX_NUM_OF_SESSIONS + 1,
                }
            )
    return rows
