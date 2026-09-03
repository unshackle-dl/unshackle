"""`add_keys_with_progress` gives SQL vaults the whole service in one call and chunks network vaults."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, Optional, Union
from uuid import UUID

import pytest

from unshackle.commands.kv import add_keys_with_progress
from unshackle.core.vault import Vault
from unshackle.vaults.SQLite import SQLite

LOG = logging.getLogger("kv-test")


def make_keys(n: int) -> dict[str, str]:
    return {f"{i:032x}": f"{i + 1:032x}" for i in range(n)}


class RecordingVault(Vault):
    """Counts the batch sizes it receives, like a network vault would."""

    def __init__(self) -> None:
        super().__init__("net")
        self.batches: list[int] = []

    def get_key(self, kid: Union[UUID, str], service: str) -> Optional[str]:
        return None

    def get_keys(self, service: str) -> Iterator[tuple[str, str]]:
        return iter(())

    def add_key(self, service: str, kid: Union[UUID, str], key: str) -> bool:
        return False

    def add_keys(self, service: str, kid_keys: dict[Union[UUID, str], str]) -> int:
        self.batches.append(len(kid_keys))
        return len(kid_keys)

    def get_services(self) -> Iterator[str]:
        return iter(())


def test_sql_vault_gets_one_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = SQLite("local", tmp_path / "keys.db")
    calls: list[int] = []
    real = vault.add_keys

    def spy(service: str, kid_keys: dict[Union[UUID, str], str]) -> int:
        calls.append(len(kid_keys))
        return real(service, kid_keys)

    monkeypatch.setattr(vault, "add_keys", spy)
    keys = make_keys(1201)
    assert add_keys_with_progress(vault, "SVC", keys, LOG) == 1201
    assert calls == [1201]
    assert add_keys_with_progress(vault, "SVC", keys, LOG) == 0


def test_network_vault_probes_then_batches() -> None:
    vault = RecordingVault()
    assert add_keys_with_progress(vault, "SVC", make_keys(1201), LOG) == 1201
    assert vault.batches == [1, 500, 500, 200]
