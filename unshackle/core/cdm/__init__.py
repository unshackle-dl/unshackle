"""
CDM helpers and implementations.

Keep this module import-light: downstream code frequently imports helpers from
`unshackle.core.cdm.detect`, which requires importing this package first.
Some CDM implementations pull in optional/heavy dependencies, so we lazily
import them via `__getattr__` (PEP 562).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DecryptLabsRemoteCDM",
    "CustomRemoteCDM",
    "MonaLisaCDM",
    "is_remote_cdm",
    "is_local_cdm",
    "cdm_location",
    "is_playready_cdm",
    "is_widevine_cdm",
]


def __getattr__(name: str) -> Any:
    if name == "DecryptLabsRemoteCDM":
        from .decrypt_labs_remote_cdm import DecryptLabsRemoteCDM

        return DecryptLabsRemoteCDM
    if name == "CustomRemoteCDM":
        from .custom_remote_cdm import CustomRemoteCDM

        return CustomRemoteCDM
    if name == "MonaLisaCDM":
        from .monalisa import MonaLisaCDM

        return MonaLisaCDM

    if name in {
        "is_remote_cdm",
        "is_local_cdm",
        "cdm_location",
        "is_playready_cdm",
        "is_widevine_cdm",
    }:
        from .detect import cdm_location, is_local_cdm, is_playready_cdm, is_remote_cdm, is_widevine_cdm

        return {
            "is_remote_cdm": is_remote_cdm,
            "is_local_cdm": is_local_cdm,
            "cdm_location": cdm_location,
            "is_playready_cdm": is_playready_cdm,
            "is_widevine_cdm": is_widevine_cdm,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
