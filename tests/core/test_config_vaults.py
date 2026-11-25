"""load_config_vaults: one vault factory for dl, the server, the health probe and kv."""

from pathlib import Path
from typing import Any

import pytest

from unshackle.core import vaults as vaults_module
from unshackle.core.config import config
from unshackle.core.vaults import load_config_vaults


class FakeVault:
    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def fake_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(vaults_module.MODULES, "API", FakeVault)


def sqlite(tmp_path: Path) -> dict[str, Any]:
    return {"type": "SQLite", "name": "local", "path": tmp_path / "keys.db"}


def test_config_key_vaults_load_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "key_vaults", [sqlite(tmp_path), {"type": "API", "name": "remote", "uri": "u"}])

    vaults, failed = load_config_vaults("EXAMPLE")

    assert [v.name for v in vaults] == ["local", "remote"]
    assert vaults.service == "EXAMPLE"
    assert failed == {}


def test_a_vault_that_fails_is_left_out_and_named_with_its_error() -> None:
    vaults, failed = load_config_vaults("EXAMPLE", [{"type": "Missing", "name": "gone"}, {"type": "API", "name": "ok"}])

    assert [v.name for v in vaults] == ["ok"]
    assert list(failed) == ["gone"]
    assert isinstance(failed["gone"], ValueError)


def test_an_entry_without_a_type_fails_like_any_other() -> None:
    vaults, failed = load_config_vaults("EXAMPLE", [{"name": "untyped"}, {"type": "API", "name": "ok"}])

    assert [v.name for v in vaults] == ["ok"]
    assert list(failed) == ["untyped"]


def test_a_critical_vault_that_fails_raises() -> None:
    with pytest.raises(ValueError):
        load_config_vaults("EXAMPLE", [{"type": "Missing", "name": "gone"}], critical={"missing"})


def test_critical_applies_only_to_the_types_it_accepts(tmp_path: Path) -> None:
    configs = [sqlite(tmp_path), {"type": "Missing", "name": "gone"}]

    vaults, failed = load_config_vaults("EXAMPLE", configs, critical={"sqlite"})

    assert [v.name for v in vaults] == ["local"]
    assert list(failed) == ["gone"]


def test_the_caller_config_is_not_changed(tmp_path: Path) -> None:
    configs = [sqlite(tmp_path)]

    load_config_vaults("EXAMPLE", configs)

    assert configs[0]["type"] == "SQLite"


@pytest.mark.parametrize(
    ("own_token", "global_key", "expected"),
    [(None, "global", "global"), ("own", "global", "own"), (None, "", None)],
)
def test_a_decrypt_labs_vault_takes_the_global_key_only_without_its_own(
    monkeypatch: pytest.MonkeyPatch, own_token: Any, global_key: str, expected: Any
) -> None:
    monkeypatch.setattr(config, "decrypt_labs_api_key", global_key)
    cfg = {"type": "API", "name": "decrypt_labs_vault", **({"token": own_token} if own_token else {})}

    vaults, _ = load_config_vaults("EXAMPLE", [cfg])

    assert vaults.vaults[0].kwargs.get("token") == expected


def test_only_a_decrypt_labs_vault_takes_the_global_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "decrypt_labs_api_key", "global")

    vaults, _ = load_config_vaults("EXAMPLE", [{"type": "API", "name": "other"}])

    assert "token" not in vaults.vaults[0].kwargs


def test_server_vaults_read_the_global_decrypt_labs_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.api import handlers

    monkeypatch.setattr(config, "decrypt_labs_api_key", "global")
    monkeypatch.setattr(config, "key_vaults", [{"type": "API", "name": "decrypt_labs"}, {"type": "Missing"}])

    vaults = handlers.load_server_vaults("EXAMPLE")

    assert [v.kwargs.get("token") for v in vaults] == ["global"]


def test_the_health_probe_reports_the_load_error_without_logging_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from unshackle.core.api import handlers

    class Broken:
        def __init__(self, name: str, **kwargs: Any) -> None:
            raise ConnectionError("cannot reach the vault")

    monkeypatch.setitem(vaults_module.MODULES, "Broken", Broken)
    monkeypatch.setattr(config, "key_vaults", [{"type": "Broken", "name": "down"}])

    with caplog.at_level("ERROR"):
        checks = {c["id"]: c for c in handlers.run_health_checks()}

    assert checks["vault:down"]["status"] == "fail"
    assert checks["vault:down"]["detail"] == "ConnectionError: cannot reach the vault"
    assert not caplog.records


def test_kv_names_the_vault_that_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    import click

    from unshackle.commands.kv import load_vaults

    monkeypatch.setattr(config, "key_vaults", [{"type": "Missing", "name": "gone"}])

    with pytest.raises(click.ClickException, match=r"Failed to load vault \(gone\)"):
        load_vaults(["gone"])
