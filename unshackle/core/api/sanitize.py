"""Helpers for safely logging and handling user-provided values."""

from __future__ import annotations

import re
from pathlib import PureWindowsPath
from typing import Optional

MAX_CACHE_KEY_DEPTH = 8
MAX_SESSION_CACHE_KEYS = 64


def sanitize_log(value: object) -> str:
    """Sanitise a value for safe logging by removing newlines and control characters."""
    return str(value).replace("\n", "").replace("\r", "").replace("\x00", "")


def safe_cache_key(key: object) -> Optional[str]:
    """Return a relative posix path for a peer-supplied cache key, or None if it escapes its directory.

    unshackle writes each cache key as ``<key>.json`` inside a fixed directory, and a key may
    hold ``/`` to place the file in a subdirectory (``Cacher`` keys such as ``session_web/<sha1>``).
    A cache key with a drive letter, a root, a ``..`` or ``.`` segment, an empty segment, a
    control character, or more than ``MAX_CACHE_KEY_DEPTH`` segments would let the peer write
    outside that directory or build an unbounded tree, so this function rejects it.

    PureWindowsPath treats both ``/`` and ``\\`` as separators, so this function rejects a
    cache key like ``..\\..\\secret`` even on POSIX (where backslash is a plain character).
    The function turns a surviving backslash into ``/`` so the same cache key names the
    same file on every platform.
    """
    text = str(key)
    if not text or any(ord(ch) < 32 or ch == "\x7f" for ch in text):
        return None
    path = PureWindowsPath(text)
    if path.drive or path.root or path.anchor:
        return None
    parts = re.split(r"[\\/]", text)
    if len(parts) > MAX_CACHE_KEY_DEPTH:
        return None
    if any(not part or part in {".", ".."} or part.endswith((" ", ".")) or ":" in part for part in parts):
        return None
    return "/".join(parts)
