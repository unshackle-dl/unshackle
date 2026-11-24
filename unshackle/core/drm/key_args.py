"""Decrypter key arguments and output filtering shared by the CENC DRM systems."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

ZERO_KID = "00" * 16
SHAKA_NOISE = re.compile(r"^(?:[IWV]\d{4} |\[[^\]]*:(?:INFO|WARNING|VERBOSE\d*):)")


def _hex(value: Any) -> str:
    """Return a KID or content key as lowercase hex without dashes."""
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "hex") and not isinstance(value, str):
        return str(value.hex)
    return str(value).replace("-", "").lower()


def zero_kid_key(content_keys: dict[Any, Any], own_kids: Iterable[Any]) -> Optional[str]:
    """Return the content key to give the all-zero KID, or None when no content key is safe.

    Some tracks carry an all-zero default KID in the init segment and signal the real KID
    out of band. The decrypter then needs the track's content key under the zero KID as well.

    A decrypter applies the last content key given for a repeated KID. Thus only one content
    key can go under the zero KID, and it must be the content key of the track:

    - The DRM holds exactly one content key: that content key.
    - The DRM holds several content keys: the content key of the one own KID that has one.
    - Otherwise: None. A missing content key makes the decrypt fail loudly, but an incorrect
      content key gives output that does not decode, and the verify step then flags good
      content keys as bad.
    """
    keys = {_hex(kid): _hex(key) for kid, key in content_keys.items()}
    if ZERO_KID in keys or not keys:
        return None
    if len(keys) == 1:
        return next(iter(keys.values()))
    matches = {_hex(kid) for kid in own_kids} & keys.keys()
    if len(matches) == 1:
        return keys[matches.pop()]
    return None


def track_key_args(content_keys: dict[Any, Any], track_kids: Optional[Mapping[int, Any]]) -> list[str]:
    """Build the mp4decrypt --key arguments by track ID for each track whose own KID has a content key."""
    keys = {_hex(kid): _hex(key) for kid, key in content_keys.items()}
    key_args: list[str] = []
    for track_id, kid in (track_kids or {}).items():
        if _hex(kid) in keys:
            key_args.extend(["--key", f"{track_id}:{keys[_hex(kid)]}"])
    return key_args


def mp4decrypt_key_args(
    content_keys: dict[Any, Any], own_kids: Iterable[Any], track_kids: Optional[Mapping[int, Any]] = None
) -> list[str]:
    """Build the mp4decrypt --key arguments: every content key, the zero-KID fallback and the ``track_kids`` keys."""
    key_args: list[str] = []
    for kid, key in content_keys.items():
        key_args.extend(["--key", f"{_hex(kid)}:{_hex(key)}"])
    fallback = zero_kid_key(content_keys, own_kids)
    if fallback:
        key_args.extend(["--key", f"{ZERO_KID}:{fallback}"])
    return key_args + track_key_args(content_keys, track_kids)


def shaka_keys(content_keys: dict[Any, Any], own_kids: Iterable[Any]) -> str:
    """Build the shaka-packager --keys value: every content key, then the zero-KID fallback."""
    entries = [f"label={i}:key_id={_hex(kid)}:key={_hex(key)}" for i, (kid, key) in enumerate(content_keys.items())]
    fallback = zero_kid_key(content_keys, own_kids)
    if fallback:
        entries.append(f"label={len(entries)}:key_id={ZERO_KID}:key={fallback}")
    return ",".join(entries)


def shaka_noise(line: str) -> bool:
    """True for a shaka-packager log line below ERROR, in its glog (I1103 ...) or bracket ([1103/...:WARNING:...]) form."""
    return bool(SHAKA_NOISE.match(line))
