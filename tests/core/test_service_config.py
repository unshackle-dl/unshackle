"""The service config that a Service starts with: its config.yaml, with the user's ``services.<TAG>`` block over it."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import click
import pytest

from tests.core.test_launch_context import cli_dl, cli_search
from unshackle.core import launch
from unshackle.core.config import config

pytestmark = pytest.mark.unit


@pytest.fixture
def service_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from unshackle.core.services import Services

    monkeypatch.setattr(Services, "get_path", staticmethod(lambda tag: tmp_path))
    return tmp_path


def test_an_empty_config_yaml_takes_the_user_block(monkeypatch: pytest.MonkeyPatch, service_dir: Path) -> None:
    (service_dir / config.filenames.config).write_text("")
    monkeypatch.setattr(config, "services", {"EXAMPLE": {"region": "us"}})
    assert launch.load_service_config("EXAMPLE") == {"region": "us"}


def test_a_user_block_matches_the_tag_in_any_case(monkeypatch: pytest.MonkeyPatch, service_dir: Path) -> None:
    (service_dir / config.filenames.config).write_text("region: gb\nendpoints:\n  api: a\n")
    monkeypatch.setattr(config, "services", {"example": {"region": "us"}})
    assert launch.load_service_config("EXAMPLE") == {"region": "us", "endpoints": {"api": "a"}}


def test_a_remote_service_gets_the_user_block_only(monkeypatch: pytest.MonkeyPatch, service_dir: Path) -> None:
    (service_dir / config.filenames.config).write_text("region: gb\n")
    monkeypatch.setattr(config, "services", {"EXAMPLE": {"profile": "a"}})
    assert launch.load_service_config("EXAMPLE", local=False) == {"profile": "a"}


def test_each_caller_gets_its_own_copy(monkeypatch: pytest.MonkeyPatch, service_dir: Path) -> None:
    (service_dir / config.filenames.config).write_text("endpoints:\n  api: a\n")
    monkeypatch.setattr(config, "services", {"EXAMPLE": {"hosts": ["h1"]}})
    first = launch.load_service_config("EXAMPLE")
    first["endpoints"]["api"] = "changed"
    first["hosts"].append("h2")
    assert launch.load_service_config("EXAMPLE") == {"endpoints": {"api": "a"}, "hosts": ["h1"]}
    assert config.services == {"EXAMPLE": {"hosts": ["h1"]}}


@pytest.mark.parametrize("build", [cli_dl, cli_search], ids=["dl", "search"])
def test_the_cli_starts_a_service_with_an_empty_config_yaml_and_a_user_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, build: Callable[[pytest.MonkeyPatch, Path], click.Context]
) -> None:
    (tmp_path / config.filenames.config).write_text("")
    monkeypatch.setattr(config, "services", {"example": {"region": "us"}})
    ctx = build(monkeypatch, tmp_path)
    assert ctx.obj.config == {"region": "us"}
