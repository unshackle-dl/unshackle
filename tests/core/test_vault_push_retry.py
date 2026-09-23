"""A failed vault push is reported as a failure, and a server does not store the same pair twice."""

from typing import Any
from uuid import UUID

import pytest

from unshackle.core.api import handlers
from unshackle.core.vaults import Vaults
from unshackle.vaults.HTTP import HTTP

KID = UUID("11111111-2222-3333-4444-555555555555")
KEY = "a" * 32


def test_an_unreachable_json_vault_fails_the_push(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = HTTP("json", "https://vault.invalid/rpc", "token", api_mode="json")

    def down(method: str, params: dict) -> dict:
        raise ConnectionError("vault is down")

    monkeypatch.setattr(vault, "request", down)
    vaults = Vaults("SVC")
    vaults.vaults = [vault]
    assert vaults.add_keys({KID: KEY}) == 0


class FakeVault:
    no_push = False

    def __init__(self, ok: bool) -> None:
        self.ok = ok


def fake_server_vaults(pushes: list, vault_list: list[FakeVault]) -> Any:
    class FakeVaults:
        vaults = vault_list

        def __len__(self) -> int:
            return len(vault_list)

        def add_keys(self, key_map: dict) -> int:
            pushes.append(key_map)
            return sum(vault.ok for vault in vault_list)

    return FakeVaults()


def test_server_does_not_push_a_pair_it_already_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    pushes: list = []
    monkeypatch.setattr(handlers, "load_server_vaults", lambda name: fake_server_vaults(pushes, [FakeVault(True)]))
    monkeypatch.setattr(handlers, "CACHED_PAIRS", set())
    handlers.cache_to_vaults({KID.hex: KEY}, "SVC")
    handlers.cache_to_vaults({KID.hex: KEY}, "SVC")
    assert pushes == [{KID: KEY}]


def test_server_pushes_again_after_one_vault_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    pushes: list = []
    vault_list = [FakeVault(True), FakeVault(False)]
    monkeypatch.setattr(handlers, "load_server_vaults", lambda name: fake_server_vaults(pushes, vault_list))
    monkeypatch.setattr(handlers, "CACHED_PAIRS", set())
    handlers.cache_to_vaults({KID.hex: KEY}, "SVC")
    handlers.cache_to_vaults({KID.hex: KEY}, "SVC")
    assert len(pushes) == 2
