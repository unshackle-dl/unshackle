from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from ruamel.yaml import YAML

from unshackle.core.config import Config, duplicate_key_lines, load_yaml

ALIASES = "base: &b\n  x: 1\n  x: 2\n  self: *b\nother: *b\nlist:\n  - y: 1\n    y: 2\n"


@pytest.mark.parametrize("compose", [yaml.compose, YAML().compose], ids=["pyyaml", "ruamel"])
def test_duplicate_key_lines_follows_aliases_without_looping(compose) -> None:
    assert duplicate_key_lines(compose(ALIASES)) == [(2, 3), (7, 8)]


def test_load_yaml_warns_once_with_lines_and_keeps_the_later_copy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "unshackle.yaml"
    path.write_text("credentials:\n  EXAMPLE: first-secret\n  EXAMPLE: second-secret\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="config"):
        data = load_yaml(path)
        load_yaml(path)
    assert data == {"credentials": {"EXAMPLE": "second-secret"}}
    assert caplog.messages == [f"{path}: duplicate key on lines 2 and 3. Unshackle uses the later one."]
    assert "EXAMPLE" not in caplog.text and "secret" not in caplog.text


def test_config_from_yaml_warns_on_a_duplicate_key(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "unshackle.yaml"
    path.write_text("tag: first\ntag: second\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="config"):
        assert Config.from_yaml(path).tag == "second"
    assert "duplicate key on lines 1 and 2" in caplog.text


@pytest.mark.parametrize(
    "text",
    ["a: 1\nb: [1, 2]\n", "", "a: &a\n  x: 1\nb: &b\n  y: 2\nc:\n  <<: *a\n  <<: *b\n"],
    ids=["valid", "empty", "two-merges"],
)
def test_load_yaml_matches_safe_load_without_a_warning(
    tmp_path: Path, text: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="config"):
        assert load_yaml(path) == yaml.safe_load(text)
    assert not caplog.records
