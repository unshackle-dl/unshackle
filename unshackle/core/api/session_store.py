"""Server-side session store for remote-dl client-server architecture.

Maintains authenticated service instances between API calls so that
a client can authenticate once and then make multiple requests (list tracks,
resolve segments, proxy license) using the same session.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from unshackle.core.api.input_bridge import AuthStatus, InputBridge
from unshackle.core.config import config
from unshackle.core.tracks import Track

log = logging.getLogger("api.session")


@dataclass
class SessionEntry:
    """A single authenticated session with a service."""

    session_id: str
    service_tag: str
    service_instance: Any  # Service instance (authenticated)
    titles: Any = None  # Titles_T from get_titles()
    title_map: Dict[str, Any] = field(default_factory=dict)  # title_id -> Title object
    tracks: Dict[str, Track] = field(default_factory=dict)  # track_id -> Track object
    tracks_by_title: Dict[str, Dict[str, Track]] = field(default_factory=dict)  # title_key -> {track_id -> Track}
    chapters_by_title: Dict[str, List[Any]] = field(default_factory=dict)  # title_key -> [Chapter]
    creator_ip: Optional[str] = None
    cache_tag: Optional[str] = None  # per-session cache directory tag
    input_bridge: Optional[InputBridge] = None
    auth_status: AuthStatus = AuthStatus.AUTHENTICATED
    auth_error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self) -> None:
        """Update last_accessed timestamp."""
        self.last_accessed = datetime.now(timezone.utc)


class SessionStore:
    """Thread-safe session store with TTL-based expiration."""

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionEntry] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    @property
    def _ttl(self) -> int:
        """Session TTL in seconds from config."""
        return config.serve.get("session_ttl", 300)  # 5 min default

    @property
    def _max_sessions(self) -> int:
        """Max concurrent sessions from config."""
        return config.serve.get("max_sessions", 100)

    async def create(
        self,
        service_tag: str,
        service_instance: Any,
        session_id: Optional[str] = None,
    ) -> SessionEntry:
        """Create a new session with an authenticated service instance."""
        async with self._lock:
            if len(self._sessions) >= self._max_sessions:
                oldest_id = min(self._sessions, key=lambda k: self._sessions[k].last_accessed)
                log.warning(f"Max sessions reached ({self._max_sessions}), evicting oldest: {oldest_id}")
                del self._sessions[oldest_id]

            session_id = session_id or str(uuid.uuid4())
            entry = SessionEntry(
                session_id=session_id,
                service_tag=service_tag,
                service_instance=service_instance,
            )
            self._sessions[session_id] = entry
            log.info(f"Created session {session_id} for service {service_tag}")
            return entry

    async def get(self, session_id: str) -> Optional[SessionEntry]:
        """Get a session by ID, returns None if not found or expired."""
        async with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None

            if entry.auth_status not in (AuthStatus.AUTHENTICATING, AuthStatus.PENDING_INPUT):
                elapsed = (datetime.now(timezone.utc) - entry.last_accessed).total_seconds()
                if elapsed > self._ttl:
                    log.info(f"Session {session_id} expired (elapsed={elapsed:.0f}s, ttl={self._ttl}s)")
                    del self._sessions[session_id]
                    return None

            entry.touch()
            return entry

    async def delete(self, session_id: str) -> bool:
        """Delete a session. Returns True if it existed."""
        async with self._lock:
            entry = self._sessions.pop(session_id, None)
            if entry:
                if entry.input_bridge:
                    entry.input_bridge.cancel()
                self._cleanup_cache_dir(entry.cache_tag)
                log.info(f"Deleted session {session_id}")
                return True
            return False

    async def cleanup_expired(self) -> int:
        """Remove all expired sessions. Returns count of removed sessions."""
        async with self._lock:
            now = datetime.now(timezone.utc)
            expired = []
            for sid, entry in self._sessions.items():
                elapsed = (now - entry.last_accessed).total_seconds()
                if entry.auth_status in (AuthStatus.AUTHENTICATING, AuthStatus.PENDING_INPUT):
                    if elapsed > 600:
                        expired.append(sid)
                elif elapsed > self._ttl:
                    expired.append(sid)
            for sid in expired:
                entry = self._sessions.pop(sid)
                if entry.input_bridge:
                    entry.input_bridge.cancel()
                self._cleanup_cache_dir(entry.cache_tag)
            if expired:
                log.info(f"Cleaned up {len(expired)} expired sessions")
            return len(expired)

    async def start_cleanup_loop(self) -> None:
        """Start periodic cleanup of expired sessions."""
        if self._cleanup_task is not None:
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(60)  # Check every minute
                try:
                    await self.cleanup_expired()
                except Exception:
                    log.exception("Error during session cleanup")

        self._cleanup_task = asyncio.create_task(_loop())
        log.info("Session cleanup loop started")

    async def stop_cleanup_loop(self) -> None:
        """Stop the periodic cleanup task."""
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            self._cleanup_task = None

    async def cancel_all_bridges(self) -> None:
        """Cancel all active input bridges (called on server shutdown)."""
        async with self._lock:
            for entry in self._sessions.values():
                if entry.input_bridge:
                    entry.input_bridge.cancel()
            count = len(self._sessions)
        if count:
            log.info(f"Cancelled bridges for {count} active session(s)")

    @staticmethod
    def _cleanup_cache_dir(cache_tag: Optional[str]) -> None:
        """Remove session cache directory and empty parents."""
        if not cache_tag:
            return
        import shutil

        cache_dir = config.directories.cache / cache_tag
        if cache_dir.is_dir():
            try:
                shutil.rmtree(cache_dir)
            except Exception as e:
                log.warning(f"Failed to remove session cache {cache_dir}: {e}")
        for parent in cache_dir.parents:
            if parent == config.directories.cache:
                break
            try:
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except Exception:
                break

    @property
    def session_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)


# Singleton instance
_session_store: Optional[SessionStore] = None


def get_session_store() -> SessionStore:
    """Get or create the global session store singleton."""
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store
