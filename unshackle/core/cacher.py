from __future__ import annotations

import time
import zlib
from datetime import datetime
from os import stat_result
from pathlib import Path
from typing import Any, Optional, Union

import jsonpickle
import jwt

from unshackle.core.config import config

EXP_T = Union[datetime, str, int, float]

# A number below this is seconds until expiration; at or above it, a unix timestamp (2001-09-09 onwards).
ABSOLUTE_TIMESTAMP_MIN = 1_000_000_000


class Cacher:
    """Cacher for Services to get and set arbitrary data with expiration dates."""

    def __init__(
        self,
        service_tag: str,
        key: Optional[str] = None,
        version: Optional[int] = 1,
        data: Optional[Any] = None,
        expiration: Optional[datetime] = None,
    ) -> None:
        self.service_tag = service_tag
        self.key = key
        self.version = version
        self.data = data or {}
        self.expiration = expiration

        if self.expiration and self.expired:
            self.data = None
            self.path.unlink()

    def __bool__(self) -> bool:
        return bool(self.data)

    @property
    def path(self) -> Path:
        """Get the path at which the cache will be read and written."""
        return (config.directories.cache / self.service_tag / self.key).with_suffix(".json")

    @property
    def expired(self) -> bool:
        """Compare as unix time: wall-clock comparison is wrong for an hour at each DST change."""
        return bool(self.expiration and self.expiration.timestamp() < time.time())

    def get(self, key: str, version: int = 1) -> Cacher:
        """
        Get the cached data of the service by cache key.
        :param key: the filename to save the data to, should be url-safe.
        :param version: the config data version you expect to use.
        :returns: Cache object containing the cached data or None if the file does not exist.
        """
        cache = Cacher(self.service_tag, key, version)
        if cache.path.is_file():
            data = jsonpickle.loads(cache.path.read_text(encoding="utf8"))
            payload = data.copy()
            del payload["crc32"]
            checksum = data["crc32"]
            calculated = zlib.crc32(jsonpickle.dumps(payload).encode("utf8"))
            if calculated != checksum:
                raise ValueError(f"Cache payload checksum mismatch: {checksum} != calculated {calculated}")
            cache.data = data["data"]
            # The pickled `expiration` is local wall-clock and only right in the zone that wrote it.
            expires_at = data.get("expires_at")
            cache.expiration = datetime.fromtimestamp(expires_at) if expires_at is not None else data["expiration"]
            cache.version = data["version"]
            if cache.version != version:
                raise ValueError(f"The {self.service_tag} {key} cache is outdated, delete {cache.path}")
        return cache

    def set(self, data: Any, expiration: Optional[EXP_T] = None) -> Any:
        """
        Set the cached data of the service by cache key.
        :param data: absolutely anything including None.
        :param expiration: when the data expires, optional. Can be ISO 8601, seconds
            til expiration, unix timestamp, or a datetime object.
        :returns: the data provided for quick wrapping of functions or vars.
        """
        self.data = data

        if expiration is None:
            try:
                expiration = jwt.decode(self.data, options={"verify_signature": False})["exp"]
            except jwt.DecodeError:
                pass

        self.expiration = self.resolve_datetime(expiration) if expiration is not None else None

        payload = {
            "data": self.data,
            "expiration": self.expiration,
            "expires_at": self.expiration.timestamp() if self.expiration else None,
            "version": self.version,
        }
        payload["crc32"] = zlib.crc32(jsonpickle.dumps(payload).encode("utf8"))

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(jsonpickle.dumps(payload))

        return self.data

    def stat(self) -> stat_result:
        """
        Get Cache file OS Stat data like Creation Time, Modified Time, and such.
        :returns: an os.stat_result tuple
        """
        return self.path.stat()

    @staticmethod
    def resolve_datetime(timestamp: EXP_T) -> datetime:
        """
        Change an expiration in one of several formats to a naive local datetime.

        A datetime or an ISO 8601 string is taken as given, and one with a UTC offset
        (or a final `Z`) is converted to local time. A number is a unix timestamp, in
        seconds or 13-digit milliseconds, or the seconds until expiration when it is
        smaller than `ABSOLUTE_TIMESTAMP_MIN`. A numeric string is read as a number.
        """
        if isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp)
            except ValueError:
                timestamp = float(str(timestamp))
        if isinstance(timestamp, datetime):
            # naive local time is the one form every consumer of `expiration` can do arithmetic with
            return timestamp.astimezone().replace(tzinfo=None) if timestamp.tzinfo else timestamp
        try:
            if len(str(abs(int(timestamp)))) == 13:  # JS-style timestamp
                timestamp /= 1000
            if timestamp < ABSOLUTE_TIMESTAMP_MIN:
                timestamp += time.time()
            return datetime.fromtimestamp(timestamp)
        except (ValueError, OverflowError, OSError):
            raise ValueError(f"Unrecognized Timestamp value {timestamp!r}")
