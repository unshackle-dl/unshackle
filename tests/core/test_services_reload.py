"""Pins hot-reload of services: re-import on change, drop on removal, and staged swaps for busy services."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from unshackle.core import services
from unshackle.core.config import config

pytestmark = pytest.mark.unit


def write_service(root: Path, version: int) -> None:
    pkg = root / "FOO"
    pkg.mkdir(exist_ok=True)
    init = pkg / "__init__.py"
    init.write_text(f"class FOO:\n    VERSION = {version}\n    ALIASES = ('foo{version}',)\n")
    # the bytecode cache keys on mtime and size; same-second rewrites of equal length would reuse the stale .pyc
    os.utime(init, (time.time() + version, time.time() + version))


@pytest.fixture
def service_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "svc"
    root.mkdir()
    write_service(root, 1)
    monkeypatch.setattr(config.directories, "services", [str(root)])
    monkeypatch.setattr(services, "SERVICES", [])
    monkeypatch.setattr(services, "MODULES", {})
    monkeypatch.setattr(services, "ALIASES", {})
    monkeypatch.setattr(services, "LOAD_ERRORS", [])
    monkeypatch.setattr(services, "PENDING", set())
    assert services.reload_services(["FOO"]) == []
    assert services.MODULES["FOO"].VERSION == 1
    yield root
    services.reload_services(["FOO"])


def test_reload_picks_up_new_code_and_removal(service_dir: Path):
    write_service(service_dir, 2)
    assert services.reload_services(["FOO"]) == []
    assert services.MODULES["FOO"].VERSION == 2
    assert services.ALIASES["FOO"] == ("foo2",)
    assert services.Services.get_tag("foo2") == "FOO"

    shutil.rmtree(service_dir / "FOO")
    assert services.reload_services(["FOO"]) == []
    assert "FOO" not in services.MODULES
    assert "FOO" not in services.Services.get_tags()


def test_busy_service_is_staged_then_applied(service_dir: Path, monkeypatch: pytest.MonkeyPatch):
    write_service(service_dir, 2)
    monkeypatch.setattr(services, "repo_specs", lambda: ["example/repo"])
    monkeypatch.setattr(services, "refresh_repo", lambda spec: (service_dir, ["~FOO"]))

    repos = services.refresh_and_reload(busy={"FOO"})
    assert repos == [
        {"spec": "example/repo", "updated": True, "changes": ["~FOO"], "deferred": ["FOO"], "load_errors": []}
    ]
    assert services.MODULES["FOO"].VERSION == 1
    assert "FOO" in services.PENDING

    assert services.apply_pending(busy={"FOO"}) == []
    assert services.MODULES["FOO"].VERSION == 1

    assert services.apply_pending(busy=set()) == ["FOO"]
    assert services.MODULES["FOO"].VERSION == 2
    assert services.PENDING == set()


def write_broken_service(root: Path, version: int) -> None:
    init = root / "FOO" / "__init__.py"
    init.write_text(f"raise ImportError('boom {version}')\n")
    os.utime(init, (time.time() + version, time.time() + version))


def test_apply_pending_never_reports_a_failed_reimport_as_applied(
    service_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed re-import keeps the old module serving, so the tag is not applied."""
    monkeypatch.setattr(services, "PENDING_SINCE", {})
    monkeypatch.setattr(services, "LOADED_COMMITS", {"FOO": "old-head"})
    monkeypatch.setattr(services, "head", lambda dest: "new-head")
    (service_dir / ".git").mkdir()

    write_broken_service(service_dir, 2)
    monkeypatch.setattr(services, "repo_specs", lambda: ["example/repo"])
    monkeypatch.setattr(services, "refresh_repo", lambda spec: (service_dir, ["~FOO"]))
    services.refresh_and_reload(busy={"FOO"})
    assert services.PENDING == {"FOO"}

    assert services.apply_pending(busy=set()) == []
    assert services.MODULES["FOO"].VERSION == 1
    assert services.LOAD_ERRORS and services.LOAD_ERRORS[0].startswith("FOO: ")
    # The commit must still name the code that is running, not the one that failed to import.
    assert services.LOADED_COMMITS["FOO"] == "old-head"

    write_service(service_dir, 3)
    services.PENDING.add("FOO")
    assert services.apply_pending(busy=set()) == ["FOO"]
    assert services.LOADED_COMMITS["FOO"] == "new-head"
