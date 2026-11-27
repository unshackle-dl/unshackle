"""A failed vault push or lookup is reported as a failure, and a server does not store the same pair twice."""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import requests

from unshackle.commands.kv import copy_service_data
from unshackle.core.api import handlers
from unshackle.core.vault import RateLimited, json_body
from unshackle.core.vaults import Vaults
from unshackle.vaults.API import API
from unshackle.vaults.HTTP import HTTP
from unshackle.vaults.SQLite import SQLite

KID = UUID("11111111-2222-3333-4444-555555555555")
KID2 = UUID("66666666-7777-8888-9999-000000000000")
KEY = "a" * 32


PARKED_PAGE = b"<!DOCTYPE html><html><body>This domain may be for sale.</body></html>"


def fake_response(
    status: int = 200, body: bytes = PARKED_PAGE, content_type: str = "text/html; charset=utf-8"
) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r.headers["Content-Type"] = content_type
    r._content = body
    return r


def parked_host(calls: list, status: int = 200, body: bytes = PARKED_PAGE) -> Callable[..., requests.Response]:
    def parked(*args: object, **kw: object) -> requests.Response:
        calls.append(1)
        return fake_response(status, body)

    return parked


def test_an_unreachable_json_vault_fails_the_push(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = HTTP("json", "https://vault.invalid/rpc", "token", api_mode="json")

    def down(method: str, params: dict) -> dict:
        raise ConnectionError("vault is down")

    monkeypatch.setattr(vault, "request", down)
    vaults = Vaults("SVC")
    vaults.vaults = [vault]
    assert vaults.add_keys({KID: KEY}) == 0


@pytest.mark.parametrize(
    "mode, status, body",
    [
        ("json", 200, PARKED_PAGE),
        ("decrypt_labs", 200, PARKED_PAGE),
        ("query", 200, PARKED_PAGE),
        ("json", 502, PARKED_PAGE),
        ("json", 200, b"[]"),
        ("query", 200, b"null"),
    ],
)
def test_a_vault_that_answers_html_is_skipped_after_one_request(
    mode: str, status: int, body: bytes, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list = []
    vault = HTTP("EXAMPLE", "https://vault.example/", password="tok", api_mode=mode, username="user")
    monkeypatch.setattr(vault.session, "get", parked_host(calls, status, body))
    monkeypatch.setattr(vault.session, "post", parked_host(calls, status, body))
    vaults = Vaults("SVC")
    vaults.vaults = [vault]
    with caplog.at_level(logging.WARNING):
        assert vaults.get_key(KID) == (None, None)
        assert vaults.get_key(KID2) == (None, None)
    assert len(calls) == 1
    assert vault in vaults.unreachable
    messages = [record.getMessage() for record in caplog.records]
    assert any(f"HTTP {status} text/html response is not a JSON object" in m for m in messages)
    assert not any("<html" in m or "DOCTYPE" in m or "\n" in m for m in messages)


def test_a_hostile_content_type_stays_out_of_the_error() -> None:
    r = fake_response(content_type="text/html\r\n INJECTED" + "x" * 1000)
    with pytest.raises(ConnectionError) as error:
        json_body(r)
    assert str(error.value) == "HTTP 200 text/html response is not a JSON object"


def test_an_html_answer_fails_the_json_push_without_a_single_key_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    vault = HTTP("EXAMPLE", "https://vault.example/", password="tok", api_mode="json")
    monkeypatch.setattr(vault.session, "post", parked_host(calls))
    with pytest.raises(ConnectionError):
        vault.add_keys("SVC", {KID: KEY, KID2: KEY})
    assert len(calls) == 1


def no_answer(*args: object, **kw: object) -> requests.Response:
    raise requests.ConnectTimeout("no answer")


def one_json_vault(host: str = "https://vault.example/") -> tuple[HTTP, Vaults]:
    vault = HTTP("EXAMPLE", host, password="tok", api_mode="json")
    vaults = Vaults("SVC")
    vaults.vaults = [vault]
    return vault, vaults


def test_a_json_vault_that_is_down_on_a_single_push_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    vault, vaults = one_json_vault()
    monkeypatch.setattr(vault.session, "post", no_answer)
    assert vaults.add_key(KID, KEY) == 0
    assert vault in vaults.unreachable


def test_a_json_vault_error_reply_to_a_single_push_stays_a_quiet_false(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    vault, vaults = one_json_vault()
    reply = b'{"status_code": 409, "message": "key already exists"}'
    monkeypatch.setattr(vault.session, "post", lambda *a, **kw: fake_response(200, reply, "application/json"))
    with caplog.at_level(logging.WARNING):
        assert vaults.add_key(KID, KEY) == 0
    assert vault not in vaults.unreachable
    assert not caplog.records


def test_a_json_vault_with_a_broken_url_warns_on_a_single_push(caplog: pytest.LogCaptureFixture) -> None:
    _, vaults = one_json_vault("vault.example/rpc")
    with caplog.at_level(logging.WARNING):
        assert vaults.add_key(KID, KEY) == 0
    assert [r.getMessage().split(":")[0] for r in caplog.records] == ["Failed to add key to Vault 'EXAMPLE'"]


def test_a_json_vault_that_is_down_fails_the_service_list(monkeypatch: pytest.MonkeyPatch) -> None:
    vault, _ = one_json_vault()
    monkeypatch.setattr(vault.session, "post", no_answer)
    with pytest.raises(requests.ConnectTimeout):
        list(vault.get_services())


def test_an_api_vault_still_rate_limited_fails_the_key_list_and_kv_copy_skips_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = API("api", "https://api.example/", "tok")
    monkeypatch.setattr(api.session, "get", lambda *a, **kw: fake_response(429, b"{}", "application/json"))
    with pytest.raises(RateLimited):
        list(api.get_keys("SVC"))

    with caplog.at_level(logging.WARNING, logger="kv"):
        assert copy_service_data(SQLite("to", tmp_path / "to.db"), api, "SVC", logging.getLogger("kv")) == 0
    assert [r.getMessage() for r in caplog.records] == [
        "SVC: Could not read from api API (still rate limited after 3 retries), skipped"
    ]


class FakeVault:
    no_push = False

    def __init__(self, ok: bool) -> None:
        self.ok = ok


def fake_server_vaults(pushes: list, vault_list: list[FakeVault]) -> Any:
    class FakeVaults:
        vaults = vault_list

        def __len__(self) -> int:
            return len(vault_list)

        def clear_licensed_flags(self, content_keys: dict) -> list:
            return []

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


def test_one_licence_request_builds_the_server_vaults_once(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list = []
    pushes: list = []

    def build(name: str) -> Any:
        builds.append(name)
        return fake_server_vaults(pushes, [FakeVault(False)])

    monkeypatch.setattr(handlers, "load_server_vaults", build)
    monkeypatch.setattr(handlers, "CACHED_PAIRS", set())
    shared: dict = {}
    handlers.cache_to_vaults({KID.hex: KEY}, "SVC", shared=shared)
    handlers.cache_to_vaults({KID.hex: KEY}, "SVC", shared=shared)
    assert len(builds) == 1
    assert len(pushes) == 2
