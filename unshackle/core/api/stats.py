"""Request counters and a log ring buffer for the developer dashboard."""

from __future__ import annotations

import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from aiohttp import web

from unshackle.core import __code_hash__, __version__
from unshackle.core.api.events import bus
from unshackle.core.config import config


def mask_key(key: Optional[str]) -> str:
    """A display label for an API key: its configured username, else a truncated prefix."""
    if not key:
        return "anonymous"
    user = (config.serve or {}).get("users", {}).get(key)
    if isinstance(user, dict) and user.get("username"):
        return str(user["username"])
    return key[:4] + "…"


@dataclass
class ServerStats:
    started_at: float = field(default_factory=time.time)
    host: str = ""
    port: int = 0
    mode: str = "full"
    requests_total: int = 0
    requests_rejected: int = 0
    requests_by_key: Counter = field(default_factory=Counter)

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
        stats.requests_by_key[mask_key(request_secret_key(request))] += 1
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
