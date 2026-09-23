"""Decrypter key arguments shared by the Widevine and PlayReady DRM systems."""

from __future__ import annotations

from typing import Any, Iterable, Optional

ZERO_KID = "00" * 16


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


def mp4decrypt_key_args(content_keys: dict[Any, Any], own_kids: Iterable[Any]) -> list[str]:
    """Build the mp4decrypt --key arguments: every content key, then the zero-KID fallback."""
    key_args: list[str] = []
    for kid, key in content_keys.items():
        key_args.extend(["--key", f"{_hex(kid)}:{_hex(key)}"])
    fallback = zero_kid_key(content_keys, own_kids)
    if fallback:
        key_args.extend(["--key", f"{ZERO_KID}:{fallback}"])
    return key_args


def shaka_keys(content_keys: dict[Any, Any], own_kids: Iterable[Any]) -> str:
    """Build the shaka-packager --keys value: every content key, then the zero-KID fallback."""
    entries = [f"label={i}:key_id={_hex(kid)}:key={_hex(key)}" for i, (kid, key) in enumerate(content_keys.items())]
    fallback = zero_kid_key(content_keys, own_kids)
    if fallback:
        entries.append(f"label={len(entries)}:key_id={ZERO_KID}:key={fallback}")
    return ",".join(entries)
