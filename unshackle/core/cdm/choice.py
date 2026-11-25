"""Which device and DRM system a ``cdm`` config entry gives a service. Config in, answer out: nothing loads here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from unshackle.core.utils.collections import ci_get

SYSTEMS = ("widevine", "playready")
SYSTEM_NAMES = {"wv": "widevine", "widevine": "widevine", "pr": "playready", "playready": "playready"}


def drm_system(name: Any) -> Optional[str]:
    """``widevine`` or ``playready`` for either name or its short form in any case; None for anything else."""
    return SYSTEM_NAMES.get(name.lower()) if isinstance(name, str) else None


@dataclass(frozen=True)
class CdmChoice:
    """The device an entry gives, its DRM system config keys in order, and whether it names a device for both."""

    device: Any
    systems: tuple[str, ...]
    both: bool

    @property
    def system(self) -> Optional[str]:
        """The DRM system the entry settles by its keys alone: the only one it names."""
        return self.systems[0] if len(self.systems) == 1 else None

    @property
    def name(self) -> Optional[str]:
        """The device as a device name; None when the entry gives none or an inline mapping."""
        return self.device if isinstance(self.device, str) and self.device else None


def choose_cdm(
    cdm: dict[str, Any],
    service: str,
    profile: Optional[str] = None,
    drm: Optional[str] = None,
    *,
    entry: Any = None,
    view: Literal["load", "licence", "stub"] = "load",
) -> CdmChoice:
    """The choice the ``cdm`` mapping makes for ``service`` and the named ``drm`` system, else the first one it lists.

    ``licence`` and ``stub`` read no ``cdm.default`` for a service with no entry. ``licence`` never gives the
    other system's device, only the entry's ``default``. ``stub`` ignores ``drm`` and the profile.
    """
    if entry is None:
        entry = ci_get(cdm, service) if service else None
        if not entry and view == "load":
            entry = ci_get(cdm, "default")
    if not isinstance(entry, dict):
        return CdmChoice(entry, (), False)

    lower = {key.lower(): value for key, value in entry.items()}
    systems = tuple(key for key in lower if key in SYSTEMS)
    both = set(SYSTEMS) <= {key.lower() for key, value in entry.items() if value}
    if view == "licence":
        device = (lower.get(drm) if drm in SYSTEMS else None) or lower.get("default") or ci_get(cdm, "default")
    elif view == "stub" and systems:
        device = next(filter(None, (lower[system] for system in systems)), None)
    elif view == "stub":
        device = entry.get("default") or next(iter(entry.values()), None)
    elif systems:
        device = lower.get(drm_system(drm) or systems[0]) or lower.get("playready")
    else:
        device = entry.get(profile) or entry.get("default") or ci_get(cdm, "default")
    return CdmChoice(device, systems, both)
