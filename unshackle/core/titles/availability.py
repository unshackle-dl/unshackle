"""Public, account-scoped availability metadata supplied by a service.

Keep this allowlist separate from title.data: that dict can contain credentials,
playback URLs and other private service state which must never cross the API.
"""

from typing import Any


def availability_metadata(value: Any) -> dict[str, Any] | None:
    """Return only well-typed display fields; absent metadata remains absent."""
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    if isinstance(value.get("paid"), bool):
        result["paid"] = value["paid"]
    access = value.get("access")
    if isinstance(access, str) and access in ("granted", "denied", "unknown"):
        result["access"] = access
    return result or None
