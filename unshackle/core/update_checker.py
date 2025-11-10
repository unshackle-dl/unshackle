from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

import requests


class UpdateChecker:
    """
    Check for available updates from the GitHub repository.

    This class provides functionality to check for newer versions of the application
    by querying the GitHub releases API. It includes rate limiting, caching, and
    both synchronous and asynchronous interfaces.

    Attributes:
        REPO_URL: GitHub API URL for latest release
        TIMEOUT: Request timeout in seconds
        DEFAULT_CHECK_INTERVAL: Default time between checks in seconds (24 hours)
    """

    REPO_URL = "https://api.github.com/repos/unshackle-dl/unshackle/releases/latest"
    TIMEOUT = 5
    DEFAULT_CHECK_INTERVAL = 24 * 60 * 60

    @classmethod
    def get_cache_file(cls) -> Path:
        """Get the path to the update check cache file."""
        from unshackle.core.config import config

        return config.directories.cache / "update_check.json"

    @classmethod
    def load_cache_data(cls) -> dict:
        """
        Load cache data from file.

        Returns:
            Cache data dictionary or empty dict if loading fails
        """
        cache_file = cls.get_cache_file()

        if not cache_file.exists():
            return {}

        try:
            with open(cache_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def parse_version(version_string: str) -> str:
        """
        Parse and normalize version string by removing 'v' prefix.

        Args:
            version_string: Raw version string from API

        Returns:
            Cleaned version string
        """
        return version_string.lstrip("v")

    @staticmethod
    def _is_valid_version(version: str) -> bool:
        """
        Validate version string format.

        Args:
            version: Version string to validate

        Returns:
            True if version string is valid semantic version, False otherwise
        """
        if not version or not isinstance(version, str):
            return False

        try:
            parts = version.split(".")
            if len(parts) < 2:
                return False

            for part in parts:
                int(part)

            return True
        except (ValueError, AttributeError):
            return False

    @classmethod
    def _fetch_latest_version(cls) -> Optional[str]:
        """
        Fetch the latest version from GitHub API.

        Returns:
            Latest version string if successful, None otherwise
        """
        try:
            response = requests.get(cls.REPO_URL, timeout=cls.TIMEOUT)

            if response.status_code != 200:
                return None

            data = response.json()
            latest_version = cls.parse_version(data.get("tag_name", ""))

            return latest_version if cls._is_valid_version(latest_version) else None

        except Exception:
            return None

    @classmethod
    def _should_check_for_updates(cls, check_interval: int = DEFAULT_CHECK_INTERVAL) -> bool:
        """
        Check if enough time has passed since the last update check.

        Args:
            check_interval: Time in seconds between checks (default: 24 hours)

        Returns:
            True if we should check for updates, False otherwise
        """
        cache_data = cls.load_cache_data()

        if not cache_data:
            return True

        last_check = cache_data.get("last_check", 0)
        current_time = time.time()

        return (current_time - last_check) >= check_interval

    @classmethod
    def _update_cache(cls, latest_version: Optional[str] = None, current_version: Optional[str] = None) -> None:
        """
        Update the cache file with the current timestamp and version info.

        Args:
            latest_version: The latest version found, if any
            current_version: The current version being used
        """
        cache_file = cls.get_cache_file()

        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)

            cache_data = {
                "last_check": time.time(),
                "latest_version": latest_version,
                "current_version": current_version,
            }

            with open(cache_file, "w") as f:
                json.dump(cache_data, f, indent=2)

        except (OSError, json.JSONEncodeError):
            pass

    @staticmethod
    def _compare_versions(current: str, latest: str) -> bool:
        """
        Simple semantic version comparison.

        Args:
            current: Current version string (e.g., "1.1.0")
            latest: Latest version string (e.g., "1.2.0")

        Returns:
            True if latest > current, False otherwise
        """
        if not UpdateChecker._is_valid_version(current) or not UpdateChecker._is_valid_version(latest):
            return False

        try:
            current_parts = [int(x) for x in current.split(".")]
            latest_parts = [int(x) for x in latest.split(".")]

            max_length = max(len(current_parts), len(latest_parts))
            current_parts.extend([0] * (max_length - len(current_parts)))
            latest_parts.extend([0] * (max_length - len(latest_parts)))

            for current_part, latest_part in zip(current_parts, latest_parts):
                if latest_part > current_part:
                    return True
                elif latest_part < current_part:
                    return False

            return False
        except (ValueError, AttributeError):
            return False

    @classmethod
    async def check_for_updates(cls, current_version: str) -> Optional[str]:
        """
        Check if there's a newer version available on GitHub.

        Args:
            current_version: The current version string (e.g., "1.1.0")

        Returns:
            The latest version string if an update is available, None otherwise
        """
        if not cls._is_valid_version(current_version):
            return None

        try:
            loop = asyncio.get_event_loop()
            latest_version = await loop.run_in_executor(None, cls._fetch_latest_version)

            if latest_version and cls._compare_versions(current_version, latest_version):
                return latest_version

        except Exception:
            pass

        return None

    @classmethod
    def _get_cached_update_info(cls, current_version: str) -> Optional[str]:
        """
        Check if there's a cached update available for the current version.

        Args:
            current_version: The current version string

        Returns:
            The latest version string if an update is available from cache, None otherwise
        """
        cache_data = cls.load_cache_data()

        if not cache_data:
            return None

        cached_current = cache_data.get("current_version")
        cached_latest = cache_data.get("latest_version")

        if cached_current == current_version and cached_latest:
            if cls._compare_versions(current_version, cached_latest):
                return cached_latest

        return None

    @classmethod
    def check_for_updates_sync(cls, current_version: str, check_interval: Optional[int] = None) -> Optional[str]:
        """
        Synchronous version of update check with rate limiting.

        Args:
            current_version: The current version string (e.g., "1.1.0")
            check_interval: Time in seconds between checks (default: from config)

        Returns:
            The latest version string if an update is available, None otherwise
        """
        if not cls._is_valid_version(current_version):
            return None

        if check_interval is None:
            from unshackle.core.config import config

            check_interval = config.update_check_interval * 60 * 60

        if not cls._should_check_for_updates(check_interval):
            return cls._get_cached_update_info(current_version)

        latest_version = cls._fetch_latest_version()
        cls._update_cache(latest_version, current_version)
        if latest_version and cls._compare_versions(current_version, latest_version):
            return latest_version

        return None
