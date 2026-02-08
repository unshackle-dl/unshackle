from __future__ import annotations

from typing import Any


def is_remote_cdm(cdm: Any) -> bool:
    """
    Return True if the CDM instance is backed by a remote/service CDM.

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
    Return True if the given CDM should be treated as PlayReady.

    This intentionally supports both:
    - Local PlayReady CDMs (pyplayready.cdm.Cdm)
    - Remote/wrapper CDMs (e.g. DecryptLabsRemoteCDM) that expose `is_playready`
    """

    if cdm is None:
        return False

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


def is_widevine_cdm(cdm: Any) -> bool:
    """
    Return True if the given CDM should be treated as Widevine.

    Note: for remote/wrapper CDMs that expose `is_playready`, Widevine is treated
    as the logical opposite.
    """

    if cdm is None:
        return False

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
