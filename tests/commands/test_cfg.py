from __future__ import annotations

import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from unshackle.commands import cfg as cfg_module


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "unshackle.yaml"
    monkeypatch.setattr(cfg_module, "get_config_path", lambda: path)
    return path


def test_a_duplicate_key_logs_its_lines_once_and_names_no_key_or_value(
    config_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_file.write_text(
        "theme: rainbow\ncredentials:\n  EXAMPLE: first-secret\n  OTHER: other\n  EXAMPLE: second-secret\n"
        "serve:\n  users:\n    key-secret: {tier: a}\n    key-secret: {tier: b}\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="config"):
        result = CliRunner().invoke(cfg_module.cfg, ["theme"])
        CliRunner().invoke(cfg_module.cfg, ["theme"])
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    assert caplog.messages == [
        f"{config_file}: duplicate key on lines 3 and 5, lines 8 and 9. Unshackle uses the later one."
    ]
    assert "Error" not in result.output and "lines" not in result.output
    for output in (result.output, caplog.text):
        assert "secret" not in output and "EXAMPLE" not in output


def test_a_duplicate_key_blocks_a_write(config_file: Path) -> None:
    original = "theme: rainbow\ncredentials:\n  EXAMPLE: a\n  EXAMPLE: b\n"
    config_file.write_text(original, encoding="utf-8")
    result = CliRunner().invoke(cfg_module.cfg, ["theme", "plain"])
    assert result.exit_code == 1
    assert config_file.read_text(encoding="utf-8") == original


def test_a_valid_config_still_reads_a_key(config_file: Path) -> None:
    config_file.write_text("theme: rainbow\n", encoding="utf-8")
    result = CliRunner().invoke(cfg_module.cfg, ["theme"])
    assert result.exit_code == 0, result.output
    assert "rainbow" in result.output


def test_a_duplicate_only_ruamel_sees_still_gives_its_line(config_file: Path, caplog: pytest.LogCaptureFixture) -> None:
    config_file.write_text("a: 1\n1: x\n0x1: y\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(cfg_module.cfg, ["a"])
    assert result.exit_code == 1
    assert caplog.messages == [f"{config_file}: duplicate key on line 3. Unshackle uses the later one."]
