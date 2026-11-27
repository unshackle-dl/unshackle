"""A ContentKeyResolver with stand-ins for the run it serves, for tests of content-key resolution."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from rich.table import Table

from unshackle.core.cdm.detect import cdm_type_stub
from unshackle.core.drm.key_resolution import ContentKeyResolver
from unshackle.core.vault import Vault
from unshackle.core.vaults import Vaults


class MemoryVault(Vault):
    """A key vault in memory that records each lookup in ``asked``, each push in ``pushes`` and ``added``, and each flag in ``told``."""

    def __init__(
        self, keys: dict[UUID, str] | None = None, local: bool = True, name: str = "memory", no_push: bool = False
    ) -> None:
        super().__init__(name, no_push)
        self.local = local
        self.keys: dict[UUID | str, str] = dict(keys or {})
        self.asked: list[UUID | str] = []
        self.pushes: list[dict[UUID | str, str]] = []
        self.added: dict[UUID | str, str] = {}
        self.told: list[tuple[UUID | str, str]] = []

    def get_key(self, kid: UUID | str, service: str) -> str | None:
        self.asked.append(kid)
        return self.keys.get(kid)

    def get_keys(self, service: str) -> Iterator[tuple[str, str]]:
        return iter((str(kid), key) for kid, key in self.keys.items())

    def add_key(self, service: str, kid: UUID | str, key: str) -> bool:
        return bool(self.add_keys(service, {kid: key}))

    def add_keys(self, service: str, kid_keys: dict[UUID | str, str]) -> int:
        self.pushes.append(dict(kid_keys))
        self.added.update(kid_keys)
        self.keys.update(kid_keys)
        return len(kid_keys)

    def get_services(self) -> Iterator[str]:
        return iter(())

    def flag_bad_key(self, service: str, kid: UUID | str, key: str, source: str) -> None:
        self.told.append((kid, key))


def vaults_of(*loaded: Vault, service: str = "TEST") -> Vaults:
    """A Vaults over ``loaded``, with no config."""
    vaults = Vaults(service)
    vaults.vaults = list(loaded)
    return vaults


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
    """A ContentKeyResolver over ``vaults`` (no vault by default) and a Widevine CDM, with an empty run key cache."""
    ContentKeyResolver.LICENSE_KEY_CACHE.clear()
    ContentKeyResolver.DRM_LOCKS.clear()
    cdms = SimpleNamespace(
        cdm=cdm,
        cdm_for=cdm_for or (lambda system, quality=None: cdm_type_stub(system)),
    )
    return ContentKeyResolver(vaults_of() if vaults is None else vaults, cdms, stand_in_service(), tag, **flags)


def key_table() -> Table:
    """A download table that holds the selected tracks row, as dl builds it."""
    table = Table.grid()
    table.add_row("")
    return table
