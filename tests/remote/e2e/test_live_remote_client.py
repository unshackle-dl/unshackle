"""E2E: drive the full RemoteClient HTTP surface against the running serve."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live]


def test_remote_client_get_health(remote_client) -> None:
    body = remote_client.get("/api/health")
    assert body["status"] == "ok"
    assert "version" in body


def test_remote_client_get_services(remote_client) -> None:
    body = remote_client.get("/api/services")
    assert "services" in body
    assert isinstance(body["services"], list)


def test_remote_client_get_404_raises_systemexit(remote_client) -> None:
    with pytest.raises(SystemExit):
        remote_client.get("/api/session/this-does-not-exist")


def test_remote_client_delete_404_raises_systemexit(remote_client) -> None:
    with pytest.raises(SystemExit):
        remote_client.delete("/api/session/this-does-not-exist")
