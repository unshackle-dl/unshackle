"""Helpers for safely logging and handling user-provided values."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def sanitize_log(value: object) -> str:
    """Sanitize a value for safe logging by removing newlines and control characters."""
    return str(value).replace("\n", "").replace("\r", "").replace("\x00", "")


def safe_cache_key(key: object) -> Optional[str]:
    """Return a bare filename for a peer-supplied cache key, or None if it escapes its directory.

    Cache keys are written as ``<key>.json`` inside a fixed directory. A key containing a
    path separator, ``..``, or an absolute path would let the peer write outside that
    directory, so anything that is not already a plain filename is rejected.
    """
    text = str(key)
    name = Path(text).name
    if not name or name != text or name in {".", ".."}:
        return None
    return name
