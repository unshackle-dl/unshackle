"""Services import on first use: a command pays only for the service it runs."""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

import click
import pytest
from aiohttp import web

from unshackle.core import binaries, services
from unshackle.core.config import config

pytestmark = pytest.mark.unit

EXAMPLE_SOURCE = """\
import click


class EXAMPLE:
    ALIASES = ("ex",)

    @staticmethod
    def get_binaries():
        return [{"name": "ExampleTool", "desc": "example dependency"}]

    @staticmethod
    @click.command()
    def cli():
        pass
"""


@pytest.fixture
def two_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for tag, body in (("EXAMPLE", EXAMPLE_SOURCE), ("OTHER", "raise ImportError('boom')\n")):
        (tmp_path / tag).mkdir()
        (tmp_path / tag / "__init__.py").write_text(body)
    monkeypatch.setattr(services, "SERVICES", [tmp_path / t / "__init__.py" for t in ("EXAMPLE", "OTHER")])
    monkeypatch.setattr(services, "MODULES", {})
    monkeypatch.setattr(services, "ALIASES", {})
    monkeypatch.setattr(services, "LOAD_ERRORS", [])
    monkeypatch.setattr(services, "SHADOWED", [])
    monkeypatch.setattr(services, "DIRTY_REPOS", [])
    monkeypatch.setattr(services, "SUMMARY_LOGGED", False)
    monkeypatch.setattr(binaries, "_registered_binaries", {})
    monkeypatch.setattr(binaries, "__binaries", dict(getattr(binaries, "__binaries")))
    yield
    for name in ("EXAMPLE", "OTHER"):
        sys.modules.pop(name, None)


def test_a_tag_imports_only_its_own_service(two_services: None) -> None:
    assert services.Services.get_tag("example") == "EXAMPLE"
    assert services.MODULES == {}

    assert services.Services.load("EXAMPLE").__name__ == "EXAMPLE"
    assert list(services.MODULES) == ["EXAMPLE"] and not services.LOAD_ERRORS
    assert [d["name"] for d in binaries.get_registered_dependencies()] == ["ExampleTool"]


def test_an_alias_imports_services_until_one_matches(two_services: None) -> None:
    assert services.Services.get_tag("ex") == "EXAMPLE"
    assert list(services.MODULES) == ["EXAMPLE"]


def test_a_failed_import_fails_only_its_own_tag_and_is_not_retried(two_services: None) -> None:
    with pytest.raises(KeyError):
        services.Services.load("OTHER")
    assert len(services.LOAD_ERRORS) == 1 and services.LOAD_ERRORS[0].startswith("OTHER: ")
    services.ensure_loaded("OTHER")
    assert len(services.LOAD_ERRORS) == 1

    services.check_load_errors("EXAMPLE")
    with pytest.raises(click.ClickException, match="Failed to load 1 service"):
        services.check_load_errors("OTHER")


def test_get_command_runs_a_good_service_beside_a_broken_one(two_services: None) -> None:
    group = services.Services(name="dl")
    ctx = click.Context(group)
    with pytest.raises(click.ClickException, match="OTHER: failed to import"):
        group.get_command(ctx, "OTHER")
    assert group.get_command(ctx, "EXAMPLE").name == "EXAMPLE"


def test_get_tag_does_not_wait_for_a_reload_on_a_failed_tag(two_services: None) -> None:
    services.ensure_loaded("EXAMPLE")
    services.ensure_loaded("OTHER")
    held, release = threading.Event(), threading.Event()

    def reload() -> None:
        with services.RELOAD_LOCK:
            held.set()
            release.wait()

    holder = threading.Thread(target=reload)
    holder.start()
    try:
        assert held.wait(5)
        result: list[str] = []
        lookup = threading.Thread(target=lambda: result.append(services.Services.get_tag("nosuch")))
        lookup.start()
        lookup.join(5)
        assert result == ["nosuch"]
    finally:
        release.set()
        holder.join()


def test_load_all_imports_every_service(two_services: None) -> None:
    services.load_all()
    assert list(services.MODULES) == ["EXAMPLE"]
    assert services.ALIASES == {"EXAMPLE": ("ex",)}
    assert services.failed_tags(services.LOAD_ERRORS) == {"OTHER"}


def test_env_dependencies_list_every_service_binary(two_services: None) -> None:
    from unshackle.commands.env import get_dependencies

    assert "ExampleTool" in {dep["name"] for dep in get_dependencies()}


def test_serve_startup_imports_every_service(two_services: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.commands.serve import _install_service_refresh

    monkeypatch.setattr(config, "serve", {})
    _install_service_refresh(web.Application())
    assert list(services.MODULES) == ["EXAMPLE"]


def test_a_run_of_one_tag_names_its_duplicate(two_services: None, caplog: pytest.LogCaptureFixture) -> None:
    services.SHADOWED[:] = [
        "EXAMPLE: using /a/EXAMPLE, ignoring /b/EXAMPLE",
        "OTHER: using /a/OTHER, ignoring /b/OTHER",
    ]
    with caplog.at_level(logging.INFO, logger="services"):
        services.check_load_errors("EXAMPLE")
    assert "EXAMPLE: 1 duplicate(s) ignored" in caplog.text
    assert "OTHER" not in caplog.text
