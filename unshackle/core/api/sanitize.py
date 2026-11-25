"""Helpers for safely logging and handling user-provided values."""

from __future__ import annotations

import json
import re
from pathlib import PureWindowsPath

MAX_CACHE_KEY_DEPTH = 8
MAX_SESSION_CACHE_KEYS = 256


CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
CONTROL_CHARS_BUT_LF = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def sanitize_log(value: object, keep_lines: bool = False) -> str:
    """Remove control characters, and so ANSI escapes, from a value; newlines too unless ``keep_lines``."""
    return (CONTROL_CHARS_BUT_LF if keep_lines else CONTROL_CHARS).sub("", str(value))


def safe_cache_key(key: object) -> str | None:
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


SAFE_PICKLE_TAGS = frozenset({"py/object", "py/type", "py/reduce", "py/tuple", "py/set", "py/b64", "py/id"})
SAFE_PICKLE_TYPES = frozenset(
    {"datetime.datetime", "datetime.date", "datetime.time", "datetime.timedelta", "datetime.timezone"}
)
MAX_CACHE_PAYLOAD_DEPTH = 100


def safe_cache_payload(text: str) -> bool:
    """True when a peer-supplied cache file is JSON that jsonpickle decodes to plain data and datetimes only.

    ``Cacher`` decodes cache files with jsonpickle, which imports and calls any type a file names,
    so a file from another machine may name the datetime types and nothing else. Nesting deeper than
    ``MAX_CACHE_PAYLOAD_DEPTH`` is refused, because the jsonpickle decode is recursive.
    """
    try:
        root = json.loads(text)
    except (ValueError, RecursionError):
        return False
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_CACHE_PAYLOAD_DEPTH:
            return False
        if isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key.startswith("py/") and key not in SAFE_PICKLE_TAGS:
                    return False
                if key in ("py/object", "py/type") and not (isinstance(value, str) and value in SAFE_PICKLE_TYPES):
                    return False
                if key in ("py/reduce", "__reduce__") and not (
                    isinstance(value, list) and value and isinstance(value[0], dict) and value[0].keys() == {"py/type"}
                ):
                    return False
                stack.append((value, depth + 1))
    return True
