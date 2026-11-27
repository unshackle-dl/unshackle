"""An import falls back to the export region for a proxy only when the region is a region code."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from unshackle.core import import_service

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(("region", "proxies"), [("ca", {"all": "http://ca.proxy:8080"}), ("localhost:8080", {})])
def test_the_export_region_becomes_a_proxy_only_when_it_is_a_region(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, region: str, proxies: dict
) -> None:
    monkeypatch.setattr(import_service, "resolve_proxy_arg", lambda query: f"http://{query}.proxy:8080")
    ctx = SimpleNamespace(parent=SimpleNamespace(params={}))
    with caplog.at_level(logging.WARNING):
        session = import_service.ImportService.build_session(ctx, region)  # type: ignore[arg-type]
    assert session.proxies == proxies
    assert ("continuing without one" in caplog.text) is not proxies
