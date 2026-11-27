from typing import Any
from uuid import UUID

from unshackle.core.drm.blowfish_stripe import BlowfishStripe
from unshackle.core.drm.cenc import PLACEHOLDER_KIDS
from unshackle.core.drm.clearkey import ClearKey
from unshackle.core.drm.clearkey_cenc import ClearKeyCENC
from unshackle.core.drm.clearkey_ctr import ClearKeyCTR, whole_file_cipher
from unshackle.core.drm.monalisa import MonaLisa
from unshackle.core.drm.paged_aac import PAGED_AAC
from unshackle.core.drm.playready import PlayReady
from unshackle.core.drm.widevine import Widevine
from unshackle.core.vault import is_null_key

# BlowfishStripe stays out: it holds its key from the start, so no licensing path receives it
DRM_T = ClearKey | ClearKeyCENC | Widevine | PlayReady | MonaLisa
# A whole-file cipher has no KID to probe and no fragment to check
WHOLE_FILE_DRM = (BlowfishStripe, ClearKeyCTR)


def drop_null_keys(drm: Any) -> None:
    """Remove the null keys from a DRM object's content keys. A read-only key view stays as it is."""
    content_keys = getattr(drm, "content_keys", None)
    if isinstance(content_keys, dict):
        for kid in [kid for kid, key in content_keys.items() if is_null_key(key)]:
            del content_keys[kid]


def real_kids(drm: Any) -> list[UUID]:
    """Return the KIDs a DRM object names, without the all-zero and test-pattern placeholders."""
    return [kid for kid in getattr(drm, "kids", None) or [] if kid not in PLACEHOLDER_KIDS]


def own_kids(drm: Any) -> list[UUID]:
    """Return the real KIDs that identify the track's own content key, as the DRM object knows them."""
    own = getattr(drm, "own_kids", None)
    kids = own() if callable(own) else getattr(drm, "kids", None) or []
    return [kid for kid in kids if kid not in PLACEHOLDER_KIDS]


__all__ = (
    "BlowfishStripe",
    "ClearKey",
    "ClearKeyCENC",
    "ClearKeyCTR",
    "Widevine",
    "PlayReady",
    "MonaLisa",
    "PAGED_AAC",
    "DRM_T",
    "WHOLE_FILE_DRM",
    "whole_file_cipher",
    "drop_null_keys",
    "is_null_key",
    "real_kids",
    "own_kids",
)
