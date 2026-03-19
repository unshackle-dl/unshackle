"""Shared CDM loading utility.

Instantiates a CDM object (local or remote) given a resolved device name.
Name resolution (quality-based, profile-based, DRM-type) is the caller's
responsibility — this module only handles the instantiation step.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("cdm")


def load_cdm(
    cdm_name: str,
    *,
    service_name: str = "",
    vaults: Optional[Any] = None,
) -> Any:
    """Instantiate a CDM by device name.

    Looks up the name in config.remote_cdm first (for remote/API CDMs),
    then falls back to local .prd / .wvd files.

    Returns a CDM object (WidevineCdm, PlayReadyCdm, RemoteCdm,
    DecryptLabsRemoteCDM, CustomRemoteCDM, or PlayReadyRemoteCdm).

    Raises ValueError if the device cannot be found or loaded.
    """
    from unshackle.core.config import config

    cdm_api = next(iter(x.copy() for x in config.remote_cdm if x["name"] == cdm_name), None)
    if cdm_api:
        return _load_remote_cdm(cdm_api, cdm_name, service_name, vaults)

    return _load_local_cdm(cdm_name)


def _load_remote_cdm(
    cdm_api: dict,
    cdm_name: str,
    service_name: str,
    vaults: Optional[Any],
) -> Any:
    """Instantiate a remote CDM from a config.remote_cdm entry."""
    from unshackle.core.config import config

    cdm_type = cdm_api.get("type")

    if cdm_type == "decrypt_labs":
        from unshackle.core.cdm.decrypt_labs_remote_cdm import DecryptLabsRemoteCDM

        del cdm_api["name"]
        del cdm_api["type"]

        if "secret" not in cdm_api or not cdm_api["secret"]:
            if config.decrypt_labs_api_key:
                cdm_api["secret"] = config.decrypt_labs_api_key
            else:
                raise ValueError(
                    f"No secret provided for DecryptLabs CDM '{cdm_name}' and no global "
                    "decrypt_labs_api_key configured"
                )

        return DecryptLabsRemoteCDM(service_name=service_name, vaults=vaults, **cdm_api)

    if cdm_type == "custom_api":
        from unshackle.core.cdm.custom_remote_cdm import CustomRemoteCDM

        del cdm_api["name"]
        del cdm_api["type"]
        return CustomRemoteCDM(service_name=service_name, vaults=vaults, **cdm_api)

    device_type = cdm_api.get("Device Type", cdm_api.get("device_type", ""))
    if str(device_type).upper() == "PLAYREADY":
        from pyplayready.remote.remotecdm import RemoteCdm as PlayReadyRemoteCdm

        return PlayReadyRemoteCdm(
            security_level=cdm_api.get("Security Level", cdm_api.get("security_level", 3000)),
            host=cdm_api.get("Host", cdm_api.get("host")),
            secret=cdm_api.get("Secret", cdm_api.get("secret")),
            device_name=cdm_api.get("Device Name", cdm_api.get("device_name")),
        )

    from pywidevine.remotecdm import RemoteCdm

    return RemoteCdm(
        device_type=cdm_api.get("Device Type", cdm_api.get("device_type", "")),
        system_id=cdm_api.get("System ID", cdm_api.get("system_id", "")),
        security_level=cdm_api.get("Security Level", cdm_api.get("security_level", 3000)),
        host=cdm_api.get("Host", cdm_api.get("host")),
        secret=cdm_api.get("Secret", cdm_api.get("secret")),
        device_name=cdm_api.get("Device Name", cdm_api.get("device_name")),
    )


def _load_local_cdm(cdm_name: str) -> Any:
    """Instantiate a local CDM from a .prd or .wvd file."""
    from unshackle.core.config import config

    prd_path = config.directories.prds / f"{cdm_name}.prd"
    if not prd_path.is_file():
        prd_path = config.directories.wvds / f"{cdm_name}.prd"
    if prd_path.is_file():
        from pyplayready.cdm import Cdm as PlayReadyCdm
        from pyplayready.device import Device as PlayReadyDevice

        return PlayReadyCdm.from_device(PlayReadyDevice.load(prd_path))

    cdm_path = config.directories.wvds / f"{cdm_name}.wvd"
    if not cdm_path.is_file():
        raise ValueError(f"{cdm_name} does not exist or is not a file")

    from construct import ConstError
    from pywidevine.cdm import Cdm as WidevineCdm
    from pywidevine.device import Device

    try:
        device = Device.load(cdm_path)
    except ConstError as e:
        if "expected 2 but parsed 1" in str(e):
            raise ValueError(
                f"{cdm_name}.wvd seems to be a v1 WVD file, use `pywidevine migrate --help` to migrate it to v2."
            )
        raise ValueError(f"{cdm_name}.wvd is an invalid or corrupt Widevine Device file, {e}")

    return WidevineCdm.from_device(device)
