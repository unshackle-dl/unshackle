"""Server-side remote session store for the remote-dl client-server architecture.

Maintains authenticated service instances between API calls so that
a client can authenticate once and then make several requests (get the tracks,
get the segment URLs, get the licence in proxy mode) with the same
remote session.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from unshackle.core.api.events import bus
from unshackle.core.api.input_bridge import AUTH_INPUT_TIMEOUT, AuthStatus, InputBridge
from unshackle.core.api.sanitize import sanitize_log
from unshackle.core.config import config
from unshackle.core.tracks import Track

log = logging.getLogger("api.session")


@dataclass
class SessionEntry:
    """A single authenticated remote session with a service."""

    session_id: str
    service_tag: str
    service_instance: Any
    titles: Any = None  # Titles_T from get_titles()
    title_map: Dict[str, Any] = field(default_factory=dict)
    tracks: Dict[str, Track] = field(default_factory=dict)
    tracks_by_title: Dict[str, Dict[str, Track]] = field(default_factory=dict)
    chapters_by_title: Dict[str, List[Any]] = field(default_factory=dict)
    creator_ip: Optional[str] = None
    owner_key: Optional[str] = None  # X-Secret-Key that owns this session
    cache_tag: Optional[str] = None
    server_account: Optional[str] = None  # profile name when the server lent its own account
    input_bridge: Optional[InputBridge] = None
    log_buffer: Optional[Any] = None  # SessionLogBuffer mirroring the service's self.log
    auth_status: AuthStatus = AuthStatus.AUTHENTICATED
    auth_error: Optional[str] = None
    client: Dict[str, Any] = field(default_factory=dict)  # version/argv/platform the client reported
    actions: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=500))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self) -> None:
        """Update last_accessed timestamp."""
        self.last_accessed = datetime.now(timezone.utc)

    def summary(self) -> Dict[str, Any]:
        """Dashboard view of the session: no service instance, no raw owner key."""
        from unshackle.core.api.stats import mask_key

        now = datetime.now(timezone.utc)
        return {
            "id": self.session_id,
            "owner": mask_key(self.owner_key),
            "creator_ip": self.creator_ip,
            "service": self.service_tag,
            "title_id": next(iter(self.title_map), None),
            "title": str(next(iter(self.title_map.values()), "")) or None,
            "titles": len(self.title_map),
            "tracks": len(self.tracks),
            "auth_status": self.auth_status.value,
            "auth_error": self.auth_error,
            "server_account": self.server_account,
            "log_seq": self.log_buffer.last_seq if self.log_buffer else 0,
            "client": self.client,
            "actions": list(self.actions),
            "created_at": self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "created_ts": self.created_at.timestamp(),
            "last_accessed_ts": self.last_accessed.timestamp(),
            "age_seconds": int((now - self.created_at).total_seconds()),
            "idle_seconds": int((now - self.last_accessed).total_seconds()),
        }


def _publish(action: str, entry: SessionEntry, reason: Optional[str] = None) -> None:
    data = {"action": action, **entry.summary()}
    if reason:
        data["reason"] = reason
    bus.publish("session", data)


class SessionStore:
    """Thread-safe remote session store with TTL-based expiration."""

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionEntry] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    @property
    def ttl(self) -> int:
        """Remote session TTL in seconds from config."""
        return config.serve.get("session_ttl", 300)

    @property
    def max_sessions(self) -> Optional[int]:
        """Max concurrent sessions from config; None when the operator set no limit.

        ``max_sessions: null`` or ``0`` means unlimited, so a dashboard can tell "no cap" from
        a cap that happens to sit at the default.
        """
        limit = config.serve.get("max_sessions", 100)
        return limit if isinstance(limit, int) and limit > 0 else None

    async def create(
        self,
        service_tag: str,
        service_instance: Any,
        session_id: Optional[str] = None,
        owner_key: Optional[str] = None,
        creator_ip: Optional[str] = None,
        server_account: Optional[str] = None,
    ) -> SessionEntry:
        """Make a new remote session with an authenticated service instance."""
        async with self._lock:
            max_sessions = self.max_sessions
            if max_sessions is not None and len(self._sessions) >= max_sessions:
                oldest_id = min(self._sessions, key=lambda k: self._sessions[k].last_accessed)
                log.warning(f"Max sessions reached ({max_sessions}), evicting oldest: {oldest_id}")
                _publish("delete", self._sessions.pop(oldest_id), "evicted")

            session_id = session_id or str(uuid.uuid4())
            entry = SessionEntry(
                session_id=session_id,
                service_tag=service_tag,
                service_instance=service_instance,
                owner_key=owner_key,
                creator_ip=creator_ip,
                server_account=server_account,
            )
            self._sessions[session_id] = entry
            _publish("create", entry)
            log.info(f"Created session {sanitize_log(session_id)} for service {sanitize_log(service_tag)}")
            return entry

    async def get(self, session_id: str) -> Optional[SessionEntry]:
        """Get a remote session by ID, returns None if not found or expired."""
        async with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None

            if entry.auth_status not in (AuthStatus.AUTHENTICATING, AuthStatus.PENDING_INPUT):
                elapsed = (datetime.now(timezone.utc) - entry.last_accessed).total_seconds()
                if elapsed > self.ttl:
                    log.info(f"Session {sanitize_log(session_id)} expired (elapsed={elapsed:.0f}s, ttl={self.ttl}s)")
                    _publish("delete", self._sessions.pop(session_id), "expired")
                    return None

            entry.touch()
            return entry

    def peek(self, session_id: str) -> Optional[SessionEntry]:
        """A session entry without touching it, for read-only observers.

        ``get`` refreshes ``last_accessed`` and expires stale entries, so an observer polling
        through it would keep an idle session alive and always report it as active.
        """
        return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> bool:
        """Delete a remote session. Returns True if it existed."""
        async with self._lock:
            entry = self._sessions.pop(session_id, None)
            if entry:
                if entry.input_bridge:
                    entry.input_bridge.cancel()
                self.cleanup_cache_dir(entry.cache_tag)
                _publish("delete", entry, "closed")
                log.info(f"Deleted session {sanitize_log(session_id)}")
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
                    if elapsed > AUTH_INPUT_TIMEOUT:
                        expired.append(sid)
                elif elapsed > self.ttl:
                    expired.append(sid)
            for sid in expired:
                entry = self._sessions.pop(sid)
                if entry.input_bridge:
                    entry.input_bridge.cancel()
                self.cleanup_cache_dir(entry.cache_tag)
                _publish("delete", entry, "expired")
            if expired:
                log.info(f"Cleaned up {len(expired)} expired sessions")
            return len(expired)

    async def start_cleanup_loop(self) -> None:
        """Start periodic cleanup of expired sessions."""
        if self._cleanup_task is not None:
            return

        async def loop() -> None:
            while True:
                await asyncio.sleep(60)
                try:
                    await self.cleanup_expired()
                except Exception:
                    log.exception("Error during session cleanup")

        self._cleanup_task = asyncio.create_task(loop())
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
    def cleanup_cache_dir(cache_tag: Optional[str]) -> None:
        """Remove the remote session cache directory and empty parents."""
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
            except OSError:
                break

    @staticmethod
    def publish_update(entry: SessionEntry) -> None:
        """Tell dashboard listeners that a session's summary changed (titles or tracks loaded, auth settled)."""
        _publish("update", entry)

    def record_action(self, session_id: str, action: Dict[str, Any]) -> None:
        """Append one request to the session's action log and tell dashboard listeners."""
        entry = self._sessions.get(session_id)
        if entry is None:
            return
        entry.actions.append(action)
        _publish("update", entry)

    def list(self) -> List[SessionEntry]:
        """Snapshot of every live session."""
        return list(self._sessions.values())

    @property
    def session_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)


session_store: Optional[SessionStore] = None


def get_session_store() -> SessionStore:
    """Get or make the global remote session store singleton."""
    global session_store
    if session_store is None:
        session_store = SessionStore()
    return session_store
