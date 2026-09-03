"""Request counters and a log ring buffer for the developer dashboard."""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from aiohttp import web

from unshackle.core import __code_hash__, __version__
from unshackle.core.api.events import bus
from unshackle.core.config import config

RATE_LIMIT_WINDOW = 3600.0


def _users() -> Dict[str, Any]:
    users = (config.serve or {}).get("users")
    return users if isinstance(users, dict) else {}


def _user_config(key: Optional[str]) -> Optional[Dict[str, Any]]:
    user = _users().get(key) if key else None
    return user if isinstance(user, dict) else None


def configured_key(key: Optional[str]) -> Optional[str]:
    """*key* when the config knows it (a user key, the master secret or the dashboard key), else None.

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
    """The tier name a key references, when it names one that exists."""
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
        """Seconds to wait when *key* is over its hourly limit, else None after counting the request.

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
    if response.status == 401:
        stats.requests_rejected += 1
    else:
        entry = stats.key_stats(configured_key(request_secret_key(request)))
        entry.requests += 1
        entry.last_seen = time.time()
        entry.bytes_out += getattr(response, "content_length", None) or 0
        if response.status >= 400:
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
                "status": response.status,
                "ms": round((time.perf_counter() - started) * 1000, 1),
                "bytes_in": request.content_length or 0,
                "bytes_out": getattr(response, "content_length", None) or 0,
            },
        )
    return response


class RingLogHandler(logging.Handler):
    """Keep the last N log records in memory and publish each one on the event bus."""

    def __init__(self, maxlen: int = 1000) -> None:
        super().__init__()
        self.records: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self.seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.seq += 1
        item = {
            "seq": self.seq,
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "msg": self.format(record),
        }
        self.records.append(item)
        bus.publish("log", item)

    def since(self, seq: int = 0, level: Optional[str] = None, logger: Optional[str] = None) -> List[Dict[str, Any]]:
        min_level = logging.getLevelName(level.upper()) if level else 0
        if not isinstance(min_level, int):
            min_level = 0
        return [
            r
            for r in self.records
            if r["seq"] > seq
            and logging.getLevelName(r["level"]) >= min_level
            and (not logger or r["logger"] == logger or r["logger"].startswith(logger + "."))
        ]


ring = RingLogHandler()
