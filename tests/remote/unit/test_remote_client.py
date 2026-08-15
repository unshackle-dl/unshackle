"""Unit tests for unshackle.core.remote_service.RemoteClient."""

from __future__ import annotations

import json

import pytest
import responses

from unshackle.core.remote_service import RemoteClient

pytestmark = pytest.mark.unit


@pytest.fixture
def client() -> RemoteClient:
    return RemoteClient(server_url="http://srv:8786", api_key="secret-xyz")


def test_session_sets_secret_key_header(client: RemoteClient) -> None:
    s = client.session
    assert s.headers.get("X-Secret-Key") == "secret-xyz"
    assert s.headers["User-Agent"].startswith("unshackle/")


def test_session_omits_secret_key_when_empty() -> None:
    c = RemoteClient(server_url="http://srv:8786", api_key="")
    assert "X-Secret-Key" not in c.session.headers


def test_server_url_trailing_slash_stripped() -> None:
    c = RemoteClient(server_url="http://srv:8786/", api_key="")
    assert c.server_url == "http://srv:8786"


@responses.activate
def test_get_returns_json(client: RemoteClient) -> None:
    responses.add(
        responses.GET,
        "http://srv:8786/api/health",
        json={"status": "ok"},
        status=200,
    )
    assert client.get("/api/health") == {"status": "ok"}


@responses.activate
def test_post_sends_json_body(client: RemoteClient) -> None:
    captured = {}

    def cb(request):
        captured["body"] = json.loads(request.body)
        return (200, {}, json.dumps({"session_id": "abc"}))

    responses.add_callback(
        responses.POST, "http://srv:8786/api/session/create", callback=cb, content_type="application/json"
    )
    result = client.post("/api/session/create", {"service": "ATV"})
    assert result == {"session_id": "abc"}
    assert captured["body"] == {"service": "ATV"}


@responses.activate
def test_delete_returns_json(client: RemoteClient) -> None:
    responses.add(
        responses.DELETE,
        "http://srv:8786/api/session/abc",
        json={"status": "deleted"},
        status=200,
    )
    assert client.delete("/api/session/abc") == {"status": "deleted"}


@responses.activate
def test_4xx_raises_systemexit_with_logged_error(client: RemoteClient, caplog: pytest.LogCaptureFixture) -> None:
    responses.add(
        responses.GET,
        "http://srv:8786/api/session/none",
        json={"error_code": "SESSION_NOT_FOUND", "message": "no such session"},
        status=404,
    )
    with caplog.at_level("ERROR"), pytest.raises(SystemExit):
        client.get("/api/session/none")
    assert "SESSION_NOT_FOUND" in caplog.text
    assert "no such session" in caplog.text


@responses.activate
def test_401_falls_back_to_next_auth_header(client: RemoteClient) -> None:
    seen: list[tuple[str | None, str | None]] = []

    def cb(request):
        seen.append((request.headers.get("X-Secret-Key"), request.headers.get("X-Api-Key")))
        if request.headers.get("X-Api-Key") == "secret-xyz":
            return (200, {}, json.dumps({"status": "ok"}))
        return (401, {}, json.dumps({"message": "Secret Key is Invalid."}))

    responses.add_callback(responses.GET, "http://srv:8786/api/services", callback=cb)

    assert client.get("/api/services") == {"status": "ok"}
    assert seen == [("secret-xyz", None), (None, "secret-xyz")]

    # the winning header is sticky: no second attempt on the next request
    seen.clear()
    assert client.get("/api/services") == {"status": "ok"}
    assert seen == [(None, "secret-xyz")]


@responses.activate
def test_non_401_error_does_not_fall_back(client: RemoteClient, caplog: pytest.LogCaptureFixture) -> None:
    responses.add(
        responses.GET,
        "http://srv:8786/api/services",
        json={"error_code": "FORBIDDEN", "message": "nope"},
        status=403,
    )
    with caplog.at_level("ERROR"), pytest.raises(SystemExit):
        client.get("/api/services")
    assert len(responses.calls) == 1
    assert client.session.headers.get("X-Secret-Key") == "secret-xyz"


@responses.activate
def test_401_on_every_header_exits(client: RemoteClient, caplog: pytest.LogCaptureFixture) -> None:
    responses.add(
        responses.GET,
        "http://srv:8786/api/services",
        json={"error_code": "UNAUTHORIZED", "message": "Secret Key is Invalid."},
        status=401,
    )
    with caplog.at_level("ERROR"), pytest.raises(SystemExit):
        client.get("/api/services")
    assert len(responses.calls) == 2
    assert "Secret Key is Invalid." in caplog.text


@responses.activate
def test_single_configured_auth_header_does_not_retry() -> None:
    c = RemoteClient(server_url="http://srv:8786", api_key="k", auth_headers=["X-Api-Key"])
    responses.add(responses.GET, "http://srv:8786/api/services", json={"message": "nope"}, status=401)
    with pytest.raises(SystemExit):
        c.get("/api/services")
    assert len(responses.calls) == 1
    assert c.session.headers.get("X-Api-Key") == "k"


@responses.activate
def test_connection_error_raises_systemexit(client: RemoteClient) -> None:
    import requests

    responses.add(
        responses.GET,
        "http://srv:8786/api/health",
        body=requests.ConnectionError("boom"),
    )
    with pytest.raises(SystemExit):
        client.get("/api/health")


@responses.activate
def test_timeout_raises_systemexit(client: RemoteClient) -> None:
    import requests

    responses.add(
        responses.GET,
        "http://srv:8786/api/health",
        body=requests.Timeout("slow"),
    )
    with pytest.raises(SystemExit):
        client.get("/api/health")
