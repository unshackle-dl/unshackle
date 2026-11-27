"""`add_keys_with_progress` gives SQL vaults the whole service in one call and chunks network vaults."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
import requests

from tests.core.key_resolver import fake_response
from unshackle.commands.kv import add_keys_with_progress
from unshackle.core.vault import Vault
from unshackle.vaults.API import API
from unshackle.vaults.SQLite import SQLite

LOG = logging.getLogger("kv-test")
JSON = "application/json"


def make_keys(n: int) -> dict[str, str]:
    return {f"{i:032x}": f"{i + 1:032x}" for i in range(n)}


class RecordingVault(Vault):
    """Counts the batch sizes it receives, like a network vault would."""

    def __init__(self) -> None:
        super().__init__("net")
        self.batches: list[int] = []

    def get_key(self, kid: UUID | str, service: str) -> str | None:
        return None

    def get_keys(self, service: str) -> Iterator[tuple[str, str]]:
        return iter(())

    def add_key(self, service: str, kid: UUID | str, key: str) -> bool:
        return False

    def add_keys(self, service: str, kid_keys: dict[UUID | str, str]) -> int:
        self.batches.append(len(kid_keys))
        return len(kid_keys)

    def get_services(self) -> Iterator[str]:
        return iter(())


def test_sql_vault_gets_one_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = SQLite("local", tmp_path / "keys.db")
    calls: list[int] = []
    real = vault.add_keys

    def spy(service: str, kid_keys: dict[UUID | str, str]) -> int:
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


def test_a_vault_that_refuses_batches_gets_one_key_per_call() -> None:
    vault = RecordingVault()
    vault.batch_insert = False
    assert add_keys_with_progress(vault, "SVC", make_keys(3), LOG) == 3
    assert vault.batches == [1, 1, 1]


def test_an_api_vault_keeps_the_batch_size_its_server_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = API("api", "https://api.example/", "tok")
    posts: list[int] = []

    def capped(*args: object, json: dict, **kw: object) -> requests.Response:
        size = len(json["content_keys"])
        posts.append(size)
        return fake_response(413 if size > 300 else 200, f'{{"code": 0, "added": {size}}}'.encode(), JSON)

    monkeypatch.setattr(vault.session, "post", capped)
    assert add_keys_with_progress(vault, "SVC", make_keys(1001), LOG) == 1001
    assert posts == [1, 500, 400, 300, 200, 300, 200]


@pytest.mark.parametrize("statuses", [[400] * 5 + [200, 400], [503, 200]], ids=["call-fails", "server-error"])
def test_an_api_vault_keeps_its_batch_size_when_size_was_not_the_cause(
    statuses: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = API("api", "https://api.example/", "tok")
    replies = iter(statuses)

    def answer(*args: object, json: dict, **kw: object) -> requests.Response:
        return fake_response(next(replies), f'{{"code": 0, "added": {len(json["content_keys"])}}}'.encode(), JSON)

    monkeypatch.setattr(vault.session, "post", answer)
    with contextlib.suppress(requests.HTTPError):
        vault.add_keys("SVC", make_keys(3))
    assert vault.batch_size == 500
