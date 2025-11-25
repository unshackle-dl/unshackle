"""Local client-side session cache for remote services.

Sessions are stored ONLY on the client machine, never on the server.
The server is completely stateless and receives session data with each request.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("LocalSessionCache")


class LocalSessionCache:
    """
    Client-side session cache.

    Stores authenticated sessions locally (similar to cookies/cache).
    Server never stores sessions - client sends session with each request.
    """

    def __init__(self, cache_dir: Path):
        """
        Initialize local session cache.

        Args:
            cache_dir: Directory to store session cache files
        """
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_file = cache_dir / "remote_sessions.json"

        # Load existing sessions
        self.sessions: Dict[str, Dict[str, Dict[str, Any]]] = self._load_sessions()

    def _load_sessions(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Load sessions from cache file."""
        if not self.sessions_file.exists():
            return {}

        try:
            data = json.loads(self.sessions_file.read_text(encoding="utf-8"))
            log.debug(f"Loaded {len(data)} remote sessions from cache")
            return data
        except Exception as e:
            log.error(f"Failed to load sessions cache: {e}")
            return {}

    def _save_sessions(self) -> None:
        """Save sessions to cache file."""
        try:
            self.sessions_file.write_text(
                json.dumps(self.sessions, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            log.debug(f"Saved {len(self.sessions)} remote sessions to cache")
        except Exception as e:
            log.error(f"Failed to save sessions cache: {e}")

    def store_session(
        self,
        remote_url: str,
        service_tag: str,
        profile: str,
        session_data: Dict[str, Any]
    ) -> None:
        """
        Store an authenticated session locally.

        Args:
            remote_url: Remote server URL (as key)
            service_tag: Service tag
            profile: Profile name
            session_data: Authenticated session data
        """
        # Create nested structure
        if remote_url not in self.sessions:
            self.sessions[remote_url] = {}
        if service_tag not in self.sessions[remote_url]:
            self.sessions[remote_url][service_tag] = {}

        # Store session with metadata
        self.sessions[remote_url][service_tag][profile] = {
            "session_data": session_data,
            "cached_at": time.time(),
            "service_tag": service_tag,
            "profile": profile,
        }

        self._save_sessions()
        log.info(f"Cached session for {service_tag} (profile: {profile}, remote: {remote_url})")

    def get_session(
        self,
        remote_url: str,
        service_tag: str,
        profile: str
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve a cached session.

        Args:
            remote_url: Remote server URL
            service_tag: Service tag
            profile: Profile name

        Returns:
            Session data or None if not found/expired
        """
        try:
            session_entry = self.sessions[remote_url][service_tag][profile]

            # Check if expired (24 hours)
            age = time.time() - session_entry["cached_at"]
            if age > 86400:  # 24 hours
                log.info(f"Session expired for {service_tag} (age: {age:.0f}s)")
                self.delete_session(remote_url, service_tag, profile)
                return None

            log.debug(f"Using cached session for {service_tag} (profile: {profile})")
            return session_entry["session_data"]

        except KeyError:
            log.debug(f"No cached session for {service_tag} (profile: {profile})")
            return None

    def has_session(
        self,
        remote_url: str,
        service_tag: str,
        profile: str
    ) -> bool:
        """
        Check if a valid session exists.

        Args:
            remote_url: Remote server URL
            service_tag: Service tag
            profile: Profile name

        Returns:
            True if valid session exists
        """
        session = self.get_session(remote_url, service_tag, profile)
        return session is not None

    def delete_session(
        self,
        remote_url: str,
        service_tag: str,
        profile: str
    ) -> bool:
        """
        Delete a cached session.

        Args:
            remote_url: Remote server URL
            service_tag: Service tag
            profile: Profile name

        Returns:
            True if session was deleted
        """
        try:
            del self.sessions[remote_url][service_tag][profile]

            # Clean up empty nested dicts
            if not self.sessions[remote_url][service_tag]:
                del self.sessions[remote_url][service_tag]
            if not self.sessions[remote_url]:
                del self.sessions[remote_url]

            self._save_sessions()
            log.info(f"Deleted cached session for {service_tag} (profile: {profile})")
            return True

        except KeyError:
            return False

    def list_sessions(self, remote_url: Optional[str] = None) -> list[Dict[str, Any]]:
        """
        List all cached sessions.

        Args:
            remote_url: Optional filter by remote URL

        Returns:
            List of session metadata
        """
        sessions = []

        remotes = [remote_url] if remote_url else self.sessions.keys()

        for remote in remotes:
            if remote not in self.sessions:
                continue

            for service_tag, profiles in self.sessions[remote].items():
                for profile, entry in profiles.items():
                    age = time.time() - entry["cached_at"]

                    sessions.append({
                        "remote_url": remote,
                        "service_tag": service_tag,
                        "profile": profile,
                        "cached_at": entry["cached_at"],
                        "age_seconds": int(age),
                        "expired": age > 86400,
                        "has_cookies": bool(entry["session_data"].get("cookies")),
                        "has_headers": bool(entry["session_data"].get("headers")),
                    })

        return sessions

    def cleanup_expired(self) -> int:
        """
        Remove expired sessions (older than 24 hours).

        Returns:
            Number of sessions removed
        """
        removed = 0
        current_time = time.time()

        for remote_url in list(self.sessions.keys()):
            for service_tag in list(self.sessions[remote_url].keys()):
                for profile in list(self.sessions[remote_url][service_tag].keys()):
                    entry = self.sessions[remote_url][service_tag][profile]
                    age = current_time - entry["cached_at"]

                    if age > 86400:  # 24 hours
                        del self.sessions[remote_url][service_tag][profile]
                        removed += 1
                        log.info(f"Removed expired session for {service_tag} (age: {age:.0f}s)")

                # Clean up empty dicts
                if not self.sessions[remote_url][service_tag]:
                    del self.sessions[remote_url][service_tag]
            if not self.sessions[remote_url]:
                del self.sessions[remote_url]

        if removed > 0:
            self._save_sessions()

        return removed


# Global instance
_local_session_cache: Optional[LocalSessionCache] = None


def get_local_session_cache() -> LocalSessionCache:
    """
    Get the global local session cache instance.

    Returns:
        LocalSessionCache instance
    """
    global _local_session_cache

    if _local_session_cache is None:
        from unshackle.core.config import config
        cache_dir = config.directories.cache / "remote_sessions"
        _local_session_cache = LocalSessionCache(cache_dir)

        # Clean up expired sessions on init
        _local_session_cache.cleanup_expired()

    return _local_session_cache


__all__ = ["LocalSessionCache", "get_local_session_cache"]
