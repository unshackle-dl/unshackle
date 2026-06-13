"""E2E: basic reachability against a running `unshackle serve`."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live]


def test_health_endpoint(http_session, server_url: str) -> None:
    r = http_session.get(f"{server_url}/api/health", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "update_check" in body


def test_services_endpoint(http_session, server_url: str) -> None:
    r = http_session.get(f"{server_url}/api/services", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "services" in body
    assert isinstance(body["services"], list)


def test_health_does_not_require_secret_key(server_url: str) -> None:
    """Health bypasses auth even when the server runs with a key."""
    import requests

    r = requests.get(f"{server_url}/api/health", timeout=10)
    assert r.status_code == 200


def test_unknown_route_returns_404(http_session, server_url: str) -> None:
    r = http_session.get(f"{server_url}/api/this-does-not-exist", timeout=10)
    assert r.status_code in (404, 405)
