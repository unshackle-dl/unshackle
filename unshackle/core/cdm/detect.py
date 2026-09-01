from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def cdm_type_stub(cdm_type: str) -> SimpleNamespace:
    """Type-only CDM stand-in so is_widevine_cdm()/is_playready_cdm() (and thus
    get_drm_for_cdm) route correctly without loading a real device. Used for
    server_cdm mode, where the server holds the device and returns the keys."""
    return SimpleNamespace(is_playready=cdm_type == "playready")


def is_remote_cdm(cdm: Any) -> bool:
    """
    Return True if a remote CDM or a service CDM backs the CDM instance.

    This is useful for service logic that needs to know whether the CDM runs
    locally (in-process) vs over HTTP/RPC (remote).
    """

    if cdm is None:
        return False

    if hasattr(cdm, "is_remote_cdm"):
        try:
            return bool(getattr(cdm, "is_remote_cdm"))
        except Exception:
            pass

    try:
        from pyplayready.remote.remotecdm import RemoteCdm as PlayReadyRemoteCdm
    except Exception:
        PlayReadyRemoteCdm = None

    if PlayReadyRemoteCdm is not None:
        try:
            if isinstance(cdm, PlayReadyRemoteCdm):
                return True
        except Exception:
            pass

    try:
        from pywidevine.remotecdm import RemoteCdm as WidevineRemoteCdm
    except Exception:
        WidevineRemoteCdm = None

    if WidevineRemoteCdm is not None:
        try:
            if isinstance(cdm, WidevineRemoteCdm):
                return True
        except Exception:
            pass

    cls = getattr(cdm, "__class__", None)
    mod = getattr(cls, "__module__", "") or ""
    name = getattr(cls, "__name__", "") or ""

    if mod == "unshackle.core.cdm.decrypt_labs_remote_cdm" and name == "DecryptLabsRemoteCDM":
        return True
    if mod == "unshackle.core.cdm.custom_remote_cdm" and name == "CustomRemoteCDM":
        return True

    if mod.startswith("pyplayready.remote") or mod.startswith("pywidevine.remote"):
        return True
    if "remote" in mod.lower() and name.lower().endswith("cdm"):
        return True
    if name.lower().endswith("remotecdm"):
        return True

    return False


def is_local_cdm(cdm: Any) -> bool:
    """
    Return True if the CDM instance is local/in-process.

    Unknown CDM types return False (use `cdm_location()` if you need 3-state).
    """

    if cdm is None:
        return False

    if is_remote_cdm(cdm):
        return False

    if is_playready_cdm(cdm) or is_widevine_cdm(cdm):
        return True

    cls = getattr(cdm, "__class__", None)
    mod = getattr(cls, "__module__", "") or ""
    name = getattr(cls, "__name__", "") or ""
    if mod == "unshackle.core.cdm.monalisa.monalisa_cdm" and name == "MonaLisaCDM":
        return True

    return False


def cdm_location(cdm: Any) -> str:
    """
    Return one of: "local", "remote", "unknown".
    """

    if is_remote_cdm(cdm):
        return "remote"
    if is_local_cdm(cdm):
        return "local"
    return "unknown"


def is_playready_cdm(cdm: Any) -> bool:
    """
    Return True if unshackle treats the given CDM as PlayReady.

    This intentionally supports both:
    - Local PlayReady CDMs (pyplayready.cdm.Cdm)
    - Remote/wrapper CDMs (for example DecryptLabsRemoteCDM) that have `is_playready`
    """

    if cdm is None:
        return False

    drm = getattr(cdm, "drm", None)
    if drm:
        return str(drm).lower() == "playready"

    if hasattr(cdm, "is_playready"):
        try:
            return bool(getattr(cdm, "is_playready"))
        except Exception:
            pass

    try:
        from pyplayready.cdm import Cdm as PlayReadyCdm
    except Exception:
        PlayReadyCdm = None

    if PlayReadyCdm is not None:
        try:
            return isinstance(cdm, PlayReadyCdm)
        except Exception:
            pass

    try:
        from pyplayready.remote.remotecdm import RemoteCdm as PlayReadyRemoteCdm
    except Exception:
        PlayReadyRemoteCdm = None

    if PlayReadyRemoteCdm is not None:
        try:
            return isinstance(cdm, PlayReadyRemoteCdm)
        except Exception:
            pass

    mod = getattr(getattr(cdm, "__class__", None), "__module__", "") or ""
    return "pyplayready" in mod


def is_remote_playready_cdm(cdm: Any) -> bool:
    """Return True if the CDM is a remote PlayReady CDM (as opposed to a local .prd)."""

    return is_playready_cdm(cdm) and is_remote_cdm(cdm)


def is_remote_widevine_cdm(cdm: Any) -> bool:
    """Return True if the CDM is a remote Widevine CDM (as opposed to a local .wvd)."""

    return is_widevine_cdm(cdm) and is_remote_cdm(cdm)


def is_widevine_cdm(cdm: Any) -> bool:
    """
    Return True if unshackle treats the given CDM as Widevine.

    Note: for remote/wrapper CDMs that have `is_playready`, unshackle treats
    Widevine as the logical opposite.
    """

    if cdm is None:
        return False

    drm = getattr(cdm, "drm", None)
    if drm:
        return str(drm).lower() == "widevine"

    if hasattr(cdm, "is_playready"):
        try:
            return not bool(getattr(cdm, "is_playready"))
        except Exception:
            pass

    try:
        from pywidevine.cdm import Cdm as WidevineCdm
    except Exception:
        WidevineCdm = None

    if WidevineCdm is not None:
        try:
            return isinstance(cdm, WidevineCdm)
        except Exception:
            pass

    try:
        from pywidevine.remotecdm import RemoteCdm as WidevineRemoteCdm
    except Exception:
        WidevineRemoteCdm = None

    if WidevineRemoteCdm is not None:
        try:
            return isinstance(cdm, WidevineRemoteCdm)
        except Exception:
            pass

    mod = getattr(getattr(cdm, "__class__", None), "__module__", "") or ""
    return "pywidevine" in mod
