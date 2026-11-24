import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest
import requests
from pyplayready.remote.remotecdm import RemoteCdm as PlayReadyRemoteCdm
from pywidevine.remotecdm import RemoteCdm as WidevineRemoteCdm

from unshackle.core.cdm.unprobed_remote_cdm import UnprobedPlayReadyRemoteCdm, UnprobedWidevineRemoteCdm

HOST = "http://cdm.invalid"


def widevine(**overrides: Any) -> Any:
    args = dict(
        device_type="ANDROID", system_id=4464, security_level=3000, host=HOST, secret="s3cret", device_name="dev"
    )
    return UnprobedWidevineRemoteCdm(**{**args, **overrides})


def playready(**overrides: Any) -> Any:
    args = dict(security_level=3000, host=HOST, secret="s3cret", device_name="dev")
    return UnprobedPlayReadyRemoteCdm(**{**args, **overrides})


class FakeResponse:
    status_code = 200

    def json(self) -> dict:
        device = {"system_id": 4464, "security_level": 3000}
        return {"status": 200, "data": {"device": device, "session_id": "00ff"}}


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Record every GET, so a test sees what the upstream methods send through the session."""
    calls: list[tuple[str, Any]] = []

    def get(self: requests.Session, url: str, **kwargs: Any) -> FakeResponse:
        calls.append((url, self.headers.get("X-Secret-Key")))
        return FakeResponse()

    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr(requests, "head", lambda *a, **k: pytest.fail("the Server header probe ran"))
    return calls


@pytest.mark.parametrize("make, base", [(widevine, WidevineRemoteCdm), (playready, PlayReadyRemoteCdm)])
def test_open_goes_through_upstream_session_without_probe(make: Any, base: type, sent: list) -> None:
    cdm = make()
    assert isinstance(cdm, base)
    assert cdm.open() == b"\x00\xff"
    assert sent == [(f"{HOST}/dev/open", "s3cret")]


@pytest.mark.parametrize("make", [widevine, playready])
@pytest.mark.parametrize("field", ["host", "secret", "device_name"])
def test_missing_connection_field_raises(make: Any, field: str) -> None:
    with pytest.raises(ValueError):
        make(**{field: ""})


def test_playready_rejects_quoted_security_level() -> None:
    with pytest.raises(TypeError):
        playready(security_level="3000")


@pytest.fixture
def redirecting_host() -> Iterator[tuple[int, list[tuple[str, Any]]]]:
    """Redirect `/dev/open` to the `Location` in the `to` query, and answer the target like a serve API."""
    seen: list[tuple[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append((self.path.split("?")[0], self.headers.get("X-Secret-Key")))
            if self.path.startswith("/dev/open"):
                self.send_response(307)
                self.send_header("Location", f"http://{self.headers['X-Redirect-To']}:{port}/moved")
                self.end_headers()
                return
            body = json.dumps(FakeResponse().json()).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield port, seen
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("make", [widevine, playready])
@pytest.mark.parametrize("target, secret", [("127.0.0.1", "s3cret"), ("localhost", None)])
def test_secret_is_dropped_on_a_redirect_to_another_host(
    make: Any, target: str, secret: Any, redirecting_host: tuple[int, list[tuple[str, Any]]]
) -> None:
    port, seen = redirecting_host
    cdm = make(host=f"http://127.0.0.1:{port}")
    cdm._RemoteCdm__session.headers["X-Redirect-To"] = target
    assert cdm.open() == b"\x00\xff"
    assert seen == [("/dev/open", "s3cret"), ("/moved", secret)]
