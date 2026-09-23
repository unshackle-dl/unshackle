import base64
from typing import Any, Union
from uuid import UUID

from unshackle.core.drm.clearkey import ClearKey
from unshackle.core.drm.clearkey_cenc import ClearKeyCENC
from unshackle.core.drm.monalisa import MonaLisa
from unshackle.core.drm.playready import PlayReady
from unshackle.core.drm.widevine import Widevine

DRM_T = Union[ClearKey, ClearKeyCENC, Widevine, PlayReady, MonaLisa]


def drm_from_dict(data: dict[str, Any]) -> Union[Widevine, PlayReady, ClearKeyCENC]:
    """Reconstruct a Widevine/PlayReady/ClearKeyCENC DRM instance from its ``to_dict()`` form.

    Rebuilds the PSSH from the stored base64 (KIDs for ClearKey, which has no PSSH)
    and re-injects any saved content keys so the resulting object can decrypt without
    contacting a license server.
    """
    system = data.get("system")
    pssh_b64 = data.get("pssh_b64")
    kids = data.get("kids") or []
    content_keys = data.get("content_keys") or {}

    if system == "ClearKeyCENC":
        drm: Union[Widevine, PlayReady, ClearKeyCENC] = ClearKeyCENC(kids=kids, laurl=data.get("laurl"))
    elif not pssh_b64:
        raise ValueError("Cannot reconstruct DRM without a stored PSSH.")
    elif system == "PlayReady":
        from pyplayready.system.pssh import PSSH as PlayReadyPSSH

        drm = PlayReady(pssh=PlayReadyPSSH(base64.b64decode(pssh_b64)), pssh_b64=pssh_b64)
    elif system == "Widevine":
        from pywidevine.pssh import PSSH as WidevinePSSH

        wv_pssh = WidevinePSSH(pssh_b64)
        # kids repeats the PSSH KIDs, so kids[0] is not the track's own KID; only a PSSH
        # without KIDs needs it

        drm = Widevine(pssh=wv_pssh, kid=kids[0] if kids and not wv_pssh.key_ids else None)
    else:
        raise ValueError(f"Unsupported DRM system for reconstruction: {system!r}")

    for kid_hex, key in content_keys.items():
        drm.content_keys[UUID(hex=kid_hex)] = key

    return drm


def real_kids(drm: Any) -> list[UUID]:
    """Return the KIDs a DRM object names, without the all-zero and test-pattern placeholders."""
    return [kid for kid in getattr(drm, "kids", None) or [] if kid.int and kid not in Widevine.PLACEHOLDER_KIDS]


def own_kids(drm: Any) -> list[UUID]:
    """Return the real KIDs that identify the track's own content key, as the DRM object knows them."""
    own = getattr(drm, "own_kids", None)
    kids = own() if callable(own) else getattr(drm, "kids", None) or []
    return [kid for kid in kids if kid.int and kid not in Widevine.PLACEHOLDER_KIDS]


__all__ = (
    "ClearKey",
    "ClearKeyCENC",
    "Widevine",
    "PlayReady",
    "MonaLisa",
    "DRM_T",
    "drm_from_dict",
    "real_kids",
    "own_kids",
)
