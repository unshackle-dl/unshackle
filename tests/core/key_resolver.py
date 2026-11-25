"""A ContentKeyResolver with stand-ins for the run it serves, for tests of content-key resolution."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from rich.table import Table

from unshackle.core.cdm.detect import cdm_type_stub
from unshackle.core.drm.key_resolution import ContentKeyResolver


class NoVaults:
    """No key vault: every lookup misses and nothing is stored."""

    sources: dict = {}
    flagged: set = set()

    def __bool__(self) -> bool:
        return False

    def __len__(self) -> int:
        return 0

    def get_key(self, kid: UUID) -> tuple[None, None]:
        return None, None


def stand_in_service() -> SimpleNamespace:
    """A service whose licence functions return nothing."""
    return SimpleNamespace(
        get_widevine_license=lambda **_: None,
        get_playready_license=lambda **_: None,
        get_widevine_service_certificate=lambda **_: None,
        get_clearkey_license=lambda **_: None,
        session=None,
    )


WIDEVINE = cdm_type_stub("widevine")


def resolver(
    vaults: Any = None,
    cdm: Any = WIDEVINE,
    cdm_for: Callable[..., Any] | None = None,
    tag: str = "TEST",
    **flags: Any,
) -> ContentKeyResolver:
    """A ContentKeyResolver over ``vaults`` (none by default) and a Widevine CDM, with an empty run key cache."""
    ContentKeyResolver.LICENSE_KEY_CACHE.clear()
    ContentKeyResolver.DRM_LOCKS.clear()
    cdms = SimpleNamespace(
        cdm=cdm,
        cdm_for=cdm_for or (lambda system, quality=None: cdm_type_stub(system)),
    )
    return ContentKeyResolver(NoVaults() if vaults is None else vaults, cdms, stand_in_service(), tag, **flags)


def key_table() -> Table:
    """A download table that holds the selected tracks row, as dl builds it."""
    table = Table.grid()
    table.add_row("")
    return table
