"""Pins the serve refresh loop: staged reloads apply after the pull, not only on the next tick."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp import web

from unshackle.commands.serve import _install_service_refresh
from unshackle.core import services
from unshackle.core.api import download_manager
from unshackle.core.config import config

pytestmark = pytest.mark.unit


async def test_refresh_tick_applies_tags_staged_during_the_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tag whose last session ended while the pull ran is staged although idle; the same tick swaps it in."""
    pulled = asyncio.Event()
    seen: list[set[str]] = []

    def fake_refresh(busy: set[str]) -> list[dict[str, Any]]:
        # The pull read the busy set before the session ended, so the tag lands in PENDING while idle.
        services.PENDING.add("EXAMPLE")
        pulled.set()
        return []

    def fake_apply(busy: set[str]) -> list[str]:
        seen.append(set(services.PENDING))
        ready = sorted(services.PENDING - busy)
        services.PENDING.difference_update(ready)
        return ready

    monkeypatch.setattr(config, "serve", {"services_refresh_interval": 1})
    monkeypatch.setattr(services, "PENDING", set())
    monkeypatch.setattr(services, "repo_specs", lambda: ["x"])
    monkeypatch.setattr(services, "log_load_issues", lambda: None)
    monkeypatch.setattr(services, "record_loaded_commits", lambda: None)
    monkeypatch.setattr(services, "refresh_and_reload", fake_refresh)
    monkeypatch.setattr(services, "apply_pending", fake_apply)
    monkeypatch.setattr(download_manager, "busy_services", set)
    monkeypatch.setattr(download_manager, "publish_service_event", lambda *a, **k: None)
    monkeypatch.setattr(download_manager, "_reload_task", None)

    app = web.Application()
    _install_service_refresh(app)
    await app.on_startup[-1](app)
    try:
        await asyncio.wait_for(pulled.wait(), timeout=3)
        await asyncio.sleep(0.2)
    finally:
        await app.on_cleanup[-1](app)

    assert {"EXAMPLE"} in seen
    assert services.PENDING == set()
