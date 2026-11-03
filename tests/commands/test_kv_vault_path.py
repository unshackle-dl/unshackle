from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from unshackle.commands import kv as kv_module
from unshackle.vaults.SQLite import SQLite

KID = "0" * 31 + "1"
KEY = "a" * 32


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
