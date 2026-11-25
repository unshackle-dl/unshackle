"""What a built CDM object is: its DRM system and whether its device runs on another machine.

unshackle's own CDM classes and stand-ins carry ``is_playready``; a pywidevine or pyplayready CDM
is known by its class. For a device name in config, see ``loader.find_device``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def cdm_type_stub(cdm_type: str, security_level: int | None = None) -> SimpleNamespace:
    """A CDM stand-in that only carries its DRM system, for when another machine holds the device.

    ``security_level`` lets a service pick a robustness tier; the attribute is absent when unknown.
    """
    stub = SimpleNamespace(is_playready=cdm_type == "playready")
    if security_level is not None:
        stub.security_level = security_level
    return stub


def cdm_system(cdm: Any) -> str | None:
    """``playready`` or ``widevine`` for a CDM object; None for no CDM or one of neither system."""
    if cdm is None:
        return None
    playready = getattr(cdm, "is_playready", None)
    if isinstance(playready, bool):
        return "playready" if playready else "widevine"

    from pyplayready.cdm import Cdm as PlayReadyCdm
    from pywidevine.cdm import Cdm as WidevineCdm

    if isinstance(cdm, PlayReadyCdm):
        return "playready"
    if isinstance(cdm, WidevineCdm):
        return "widevine"
    return None


def is_playready_cdm(cdm: Any) -> bool:
    """True for a PlayReady CDM."""
    return cdm_system(cdm) == "playready"


def is_widevine_cdm(cdm: Any) -> bool:
    """True for a Widevine CDM."""
    return cdm_system(cdm) == "widevine"


def is_remote_cdm(cdm: Any) -> bool:
    """True for a CDM whose device runs on another machine."""
    if getattr(cdm, "is_remote_cdm", False):
        return True

    from pyplayready.remote.remotecdm import RemoteCdm as PlayReadyRemoteCdm
    from pywidevine.remotecdm import RemoteCdm as WidevineRemoteCdm

    return isinstance(cdm, (PlayReadyRemoteCdm, WidevineRemoteCdm))


def is_remote_playready_cdm(cdm: Any) -> bool:
    """True for a PlayReady CDM whose device runs on another machine."""
    return is_playready_cdm(cdm) and is_remote_cdm(cdm)
