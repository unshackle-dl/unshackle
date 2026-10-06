"""Pins the SIGHUP config reload: a valid file swaps the users in, a bad or restart-only file leaves the old one."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from aiohttp import web

from unshackle.commands import serve as serve_cmd
from unshackle.core import config as config_module
from unshackle.core.config import config

pytestmark = pytest.mark.unit

LOG = logging.getLogger("serve")


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[web.Application, Path]:
    """An app serving the key ``old`` with one open CDM session, and a writable config path."""
    wvds = tmp_path / "wvds"
    prds = tmp_path / "prds"
    wvds.mkdir()
    prds.mkdir()
    (wvds / "dev.wvd").write_bytes(b"x")
    monkeypatch.setattr(config.directories, "wvds", wvds)
    monkeypatch.setattr(config.directories, "prds", prds)
    monkeypatch.setattr(config, "serve", {"api_secret": "api", "users": {"old": {"devices": ["dev"]}}})
    path = tmp_path / "unshackle.yaml"
    monkeypatch.setattr(config_module, "config_path", path)

    app = web.Application()
    app["config"] = {"users": {"old": {"devices": ["dev"]}, "api": {"devices": ["dev"]}}, "devices": {}}
    app["cdms"] = {("old", "dev"): object()}
    return app, path


def _write(path: Path, **serve: object) -> None:
    path.write_text(yaml.safe_dump({"serve": {"api_secret": "api", **serve}}), encoding="utf8")


def _reload(app: web.Application) -> bool:
    return serve_cmd.reload_config(app, no_key=False, serve_widevine=True, serve_playready=True)


def test_valid_reload_swaps_the_accepted_keys(running: tuple[web.Application, Path]) -> None:
    app, path = running
    _write(path, users={"new": {"devices": ["dev"]}})

    assert _reload(app) is True
    assert set(app["config"]["users"]) == {"new", "api"}
    assert app["config"]["devices"] == {"dev": path.parent / "wvds" / "dev.wvd"}
    assert config.serve["users"] == {"new": {"devices": ["dev"]}}
    assert ("old", "dev") in app["cdms"]


def test_invalid_file_keeps_the_running_config(running: tuple[web.Application, Path]) -> None:
    app, path = running
    _write(path, users={"new": {"devices": ["dev"], "tier": "gold"}})

    assert _reload(app) is False
    assert set(app["config"]["users"]) == {"old", "api"}
    assert config.serve["users"] == {"old": {"devices": ["dev"]}}


def test_restart_only_change_is_reported_not_applied(
    running: tuple[web.Application, Path], caplog: pytest.LogCaptureFixture
) -> None:
    app, path = running
    _write(path, users={"new": {"devices": ["dev"]}}, remote_only=True)

    with caplog.at_level(logging.WARNING, logger=LOG.name):
        assert _reload(app) is False
    assert "restart" in caplog.text and "remote_only" in caplog.text
    assert set(app["config"]["users"]) == {"old", "api"}
