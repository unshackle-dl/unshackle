from __future__ import annotations

import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from unshackle.commands import kv as kv_module
from unshackle.core.vaults import MODULES
from unshackle.vaults.SQLite import SQLite

KID = "0" * 31 + "1"
KEY = "a" * 32
QUERY_VAULT = {"type": "HTTP", "name": "shared", "host": "https://vault.example/", "password": "tok", "username": "u"}


def refuse_example(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Make a query-mode HTTP vault refuse every push for the EXAMPLE service and accept the rest."""
    pushed: list[dict] = []

    def answer(self: object, params: dict) -> dict:
        if params["service"] == "example":
            return {"status_code": 403, "message": "service not allowed"}
        pushed.append(params)
        return {"status_code": 200, "inserted": True}

    monkeypatch.setattr(MODULES["HTTP"], "_query", answer)
    return pushed


def test_kv_copy_moves_a_plain_tag_table_from_a_file_into_the_vault_tag_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "key_store.db"
    SQLite("seed", source).add_key("TESTSVC", KID, KEY)
    dest = tmp_path / "local.db"
    monkeypatch.setattr(kv_module.config, "key_vaults", [{"type": "SQLite", "name": "local", "path": str(dest)}])
    monkeypatch.setattr(kv_module.Services, "get_vault_tag", lambda name: "Shared" if name == "TESTSVC" else name)

    result = CliRunner().invoke(kv_module.kv, ["copy", "local", str(source), "-s", "TESTSVC"])

    assert result.exit_code == 0, result.output
    check = SQLite("check", dest)
    assert check.get_key(KID, "Shared") == KEY
    assert check.get_key(KID, "TESTSVC") is None


def test_kv_copy_rejects_a_name_that_is_neither_configured_nor_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kv_module.config, "key_vaults", [])

    result = CliRunner().invoke(kv_module.kv, ["copy", "local", "nowhere.db"])

    assert result.exit_code != 0
    assert "not defined in the config" in result.output


def test_kv_copy_warns_and_skips_a_service_the_vault_refuses_and_copies_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "key_store.db"
    for service in ("EXAMPLE", "OTHER"):
        SQLite("seed", source).add_key(service, KID, KEY)
    monkeypatch.setattr(kv_module.config, "key_vaults", [QUERY_VAULT])
    pushed = refuse_example(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="kv"):
        result = CliRunner().invoke(kv_module.kv, ["copy", "shared", str(source)])

    assert result.exit_code == 0, result.output
    assert [p["service"] for p in pushed] == ["other"]
    assert [r.getMessage() for r in caplog.records] == [
        "EXAMPLE: Could not write to shared HTTP (API returned an error: 403 - service not allowed), skipped"
    ]


def test_kv_add_warns_and_skips_a_vault_that_refuses_and_adds_to_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    keys = tmp_path / "keys.txt"
    keys.write_text(f"{KID}:{KEY}\n", encoding="utf8")
    dest = tmp_path / "local.db"
    monkeypatch.setattr(
        kv_module.config, "key_vaults", [QUERY_VAULT, {"type": "SQLite", "name": "local", "path": str(dest)}]
    )
    monkeypatch.setattr(kv_module.Services, "get_vault_tag", lambda name: name)
    refuse_example(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="kv"):
        result = CliRunner().invoke(kv_module.kv, ["add", str(keys), "EXAMPLE", "shared", "local"])

    assert result.exit_code == 0, result.output
    assert SQLite("check", dest).get_key(KID, "EXAMPLE") == KEY
    assert [r.getMessage() for r in caplog.records] == [
        "Could not write to shared HTTP (API returned an error: 403 - service not allowed), skipped"
    ]


def test_kv_add_skips_a_null_key_line_and_adds_the_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keys = tmp_path / "keys.txt"
    null_kid = "0" * 31 + "2"
    keys.write_text(f"{KID}:{KEY}\n{null_kid}:{'0' * 32}\n", encoding="utf8")
    dest = tmp_path / "local.db"
    monkeypatch.setattr(kv_module.config, "key_vaults", [{"type": "SQLite", "name": "local", "path": str(dest)}])
    monkeypatch.setattr(kv_module.Services, "get_vault_tag", lambda name: name)

    result = CliRunner().invoke(kv_module.kv, ["add", str(keys), "TESTSVC", "local"])

    assert result.exit_code == 0, result.output
    check = SQLite("check", dest)
    assert check.get_key(KID, "TESTSVC") == KEY
    assert check.get_key(null_kid, "TESTSVC") is None
