"""The CDM catalogue: what a device name in config is, and how to build it.

A device is a ``remote_cdm`` entry or a local .prd or .wvd file. :func:`find_device` reads its DRM
system from config without loading it, and :func:`load_cdm` builds it. Which name a service uses
is the caller's choice (see ``choice.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

log = logging.getLogger("cdm")


def system_of(device_type: Any, device_name: Any = None) -> str:
    """``playready`` for a PLAYREADY device type or a PlayReady level name (SL2, SL3000); else ``widevine``."""
    playready = str(device_type or "").upper() == "PLAYREADY" or str(device_name or "").upper().startswith("SL")
    return "playready" if playready else "widevine"


def entry_system(entry: dict) -> str:
    """The DRM system of a ``remote_cdm`` entry, read from the keys its CDM class reads.

    A serve entry names no level, so only its device type counts.
    """
    if entry.get("type") == "custom_api":
        device = entry.get("device") or {}
        return system_of(device.get("type"), device.get("name"))
    if entry.get("type") == "decrypt_labs":
        return system_of(entry.get("device_type"), entry.get("device_name"))
    return system_of(entry.get("Device Type", entry.get("device_type")))


@dataclass(frozen=True, eq=False)
class Device:
    """A device config names: its DRM system, and its ``remote_cdm`` entry or its local file."""

    system: str
    source: Union[dict[str, Any], Path]


def find_device(name: str, app_config: Any = None) -> Optional[Device]:
    """The device ``name`` names in ``remote_cdm`` or the device folders; None when there is none.

    ``app_config`` is an API key's view of the config; None reads the config itself.
    """
    if app_config is None:
        from unshackle.core.config import config

        app_config = config
    entry = next((e for e in getattr(app_config, "remote_cdm", None) or [] if e.get("name") == name), None)
    if entry:
        return Device(entry_system(entry), entry)
    directories = app_config.directories
    for path in (directories.prds / f"{name}.prd", directories.wvds / f"{name}.prd"):
        if path.is_file():
            return Device("playready", path)
    path = directories.wvds / f"{name}.wvd"
    return Device("widevine", path) if path.is_file() else None


def device_system(name: str, app_config: Any = None) -> Optional[str]:
    """``widevine`` or ``playready`` for the device ``name`` names, without loading it; None when there is none."""
    device = find_device(name, app_config)
    return device.system if device else None


def load_cdm(
    cdm_name: str,
    *,
    service_name: str = "",
    vaults: Optional[Any] = None,
) -> Any:
    """Build the CDM a device name names: a remote CDM for a ``remote_cdm`` entry, else the local device.

    Raises ValueError if the device cannot be found or loaded.
    """
    device = find_device(cdm_name)
    if device is None:
        raise ValueError(f"{cdm_name} does not exist or is not a file")
    if isinstance(device.source, dict):
        return load_remote_cdm(cdm_name, device.source, device.system, service_name, vaults)
    return load_local_cdm(cdm_name, device.system, device.source)


def load_remote_cdm(name: str, entry: dict[str, Any], system: str, service_name: str, vaults: Optional[Any]) -> Any:
    """Build a remote CDM from its ``remote_cdm`` entry."""
    from unshackle.core.config import config

    cdm_api: Any = dict(entry)
    cdm_type = cdm_api.pop("type", None)
    cdm_api.pop("name", None)

    if cdm_type == "decrypt_labs":
        from unshackle.core.cdm.decrypt_labs_remote_cdm import DecryptLabsRemoteCDM

        if not cdm_api.get("secret"):
            if config.decrypt_labs_api_key:
                cdm_api["secret"] = config.decrypt_labs_api_key
            else:
                raise ValueError(
                    f"No secret provided for DecryptLabs CDM '{name}' and no global decrypt_labs_api_key configured"
                )
        return DecryptLabsRemoteCDM(service_name=service_name, vaults=vaults, **cdm_api)

    if cdm_type == "custom_api":
        from unshackle.core.cdm.custom_remote_cdm import CustomRemoteCDM

        return CustomRemoteCDM(service_name=service_name, vaults=vaults, **cdm_api)

    from unshackle.core.cdm.unprobed_remote_cdm import UnprobedPlayReadyRemoteCdm, UnprobedWidevineRemoteCdm

    security_level = cdm_api.get("Security Level", cdm_api.get("security_level", 3000))
    host = cdm_api.get("Host", cdm_api.get("host"))
    secret = cdm_api.get("Secret", cdm_api.get("secret"))
    device_name = cdm_api.get("Device Name", cdm_api.get("device_name"))
    if system == "playready":
        return UnprobedPlayReadyRemoteCdm(security_level, host, secret, device_name)
    return UnprobedWidevineRemoteCdm(
        device_type=cdm_api.get("Device Type", cdm_api.get("device_type", "")),
        system_id=cdm_api.get("System ID", cdm_api.get("system_id", "")),
        security_level=security_level,
        host=host,
        secret=secret,
        device_name=device_name,
    )


def load_local_cdm(name: str, system: str, path: Path) -> Any:
    """Build a local CDM from its .prd or .wvd file."""
    if system == "playready":
        from pyplayready.cdm import Cdm as PlayReadyCdm
        from pyplayready.device import Device as PlayReadyDevice

        return PlayReadyCdm.from_device(PlayReadyDevice.load(path))

    from construct import ConstError
    from pywidevine.cdm import Cdm as WidevineCdm
    from pywidevine.device import Device as WidevineDevice

    try:
        wvd = WidevineDevice.load(path)
    except ConstError as e:
        if "expected 2 but parsed 1" in str(e):
            raise ValueError(
                f"{name}.wvd seems to be a v1 WVD file, use `pywidevine migrate --help` to migrate it to v2."
            )
        raise ValueError(f"{name}.wvd is an invalid or corrupt Widevine Device file, {e}")

    return WidevineCdm.from_device(wvd)
