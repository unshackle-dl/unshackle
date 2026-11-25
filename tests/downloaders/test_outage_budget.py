"""A failure with no HTTP response gets the longer outage budget, and a full disk fails on the first attempt.

A failure that carries an HTTP response keeps the normal MAX_ATTEMPTS budget.
"""

import errno
import importlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests as rq
import rnet

from tests.downloaders.test_content_encoding import _FakeRnetResponse
from tests.downloaders.test_rnet_session_paths import SESSIONS
from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.session import MaxRetriesError
from unshackle.core.utilities import get_free_port

dl = importlib.import_module("unshackle.core.downloaders.requests")

BODY = b"\0" * 4096


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, status):
        self.status = status
        self.requests = 0
        super().__init__(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}/a"

    def handle_error(self, request, client_address):
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.requests += 1
        body = BODY if self.server.status == 200 else b""
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    DOWNLOAD_CANCELLED.clear()
    monkeypatch.setattr(dl, "RETRY_WAIT", 0)
    yield
    DOWNLOAD_CANCELLED.clear()


@pytest.fixture
def serve():
    servers: list[_Server] = []

    def start(status):
        server = _Server(status)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("make_session", SESSIONS)
def test_no_response_failure_gets_the_outage_budget(tmp_path, make_session):
    port = get_free_port()
    retried: list[Exception] = []

    with pytest.raises(Exception) as raised:
        list(
            dl.download(
                url=f"http://127.0.0.1:{port}/a",
                save_path=tmp_path / "a.bin",
                session=make_session(),
                on_retry=retried.append,
            )
        )

    assert len(retried) == dl.OUTAGE_ATTEMPTS - 1
    assert any(f"gave up after {dl.OUTAGE_ATTEMPTS} failed attempts" in note for note in raised.value.__notes__)


class _FullDisk:
    def __init__(self, *args, **kwargs):
        self.file = open(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.file.close()

    def write(self, data):
        raise OSError(errno.ENOSPC, "No space left on device")


def test_disk_full_is_not_retried(tmp_path, monkeypatch, serve):
    server = serve(200)
    monkeypatch.setattr(dl, "open", _FullDisk, raising=False)
    with pytest.raises(OSError) as raised:
        list(dl.download(url=server.url, save_path=tmp_path / "a.bin", session=rq.Session()))

    assert raised.value.errno == errno.ENOSPC
    assert server.requests == 1
    assert dl.retries_exhausted(raised.value)


def test_status_failure_keeps_the_normal_budget(tmp_path, serve):
    server = serve(503)
    with pytest.raises(rq.HTTPError):
        list(dl.download(url=server.url, save_path=tmp_path / "a.bin", session=rq.Session()))

    assert server.requests == dl.MAX_ATTEMPTS


@pytest.mark.parametrize("make_session", SESSIONS)
def test_malformed_url_keeps_the_normal_budget(tmp_path, make_session):
    retried: list[Exception] = []

    with pytest.raises(Exception) as raised:
        list(
            dl.download(
                url="htp://127.0.0.1/a",
                save_path=tmp_path / "a.bin",
                session=make_session(),
                on_retry=retried.append,
            )
        )

    assert len(retried) == dl.MAX_ATTEMPTS - 1
    assert any(f"gave up after {dl.MAX_ATTEMPTS} failed attempts" in note for note in raised.value.__notes__)


class _StatusError(Exception):
    def __init__(self, response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


class _RecoveringSession:
    """Refuses six connections, answers one 503, then serves the body."""

    def __init__(self):
        self.calls = 0

    def get(self, url, stream=True, **kwargs):
        self.calls += 1
        if self.calls <= 6:
            raise MaxRetriesError("GET 127.0.0.1 failed", cause=rnet.ConnectionError("connection refused"))
        if self.calls == 7:
            raise _StatusError(_FakeRnetResponse(503, {}, b""))
        return _FakeRnetResponse(200, {}, b"body-bytes")


def test_outage_does_not_spend_the_status_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "is_rnet_session", lambda s: isinstance(s, _RecoveringSession))
    monkeypatch.setattr(dl, "retry_sleep", lambda exc, attempts: 0)
    session = _RecoveringSession()
    save_path = tmp_path / "a.bin"

    list(dl.download(url="http://127.0.0.1:1/a", save_path=save_path, session=session))

    assert save_path.read_bytes() == b"body-bytes"
    assert session.calls == 8
