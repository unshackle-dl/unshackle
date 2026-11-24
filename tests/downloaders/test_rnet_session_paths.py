"""Downloader transport tests that send real requests through both session types.

Each case runs against a loopback server with a plain requests.Session and with a real RnetSession, so an rnet
upgrade that changes how a cut body, a read timeout or a Retry-After header surfaces fails here.
"""

import importlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests as rq

from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.session import RnetSession

dl = importlib.import_module("unshackle.core.downloaders.requests")

BODY = bytes(range(256)) * 256
CUT_AFTER = 10 * 1024
CHUNK = 16 * 1024
SESSIONS = [pytest.param(rq.Session, id="requests"), pytest.param(RnetSession, id="rnet")]


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.hits: list[tuple[str, str | None]] = []
        self.release = threading.Event()
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"

    def handle_error(self, request, client_address):
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        header = self.headers.get("Range")
        with self.server.lock:
            first = not any(path == self.path for path, _ in self.server.hits)
            self.server.hits.append((self.path, header))
        start, end = 0, len(BODY) - 1
        if header:
            start_s, _, end_s = header.removeprefix("bytes=").partition("-")
            start, end = int(start_s), (int(end_s) if end_s else len(BODY) - 1)
        body = BODY[start : end + 1]

        if self.path == "/busy" and first:
            self.send_response(503)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(206 if header else 200)
        if header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(BODY)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if self.path == "/cut" and len(body) > CUT_AFTER:
            self.wfile.write(body[:CUT_AFTER])
            self.close_connection = True
            return
        if self.path == "/stall" and first:
            self.wfile.write(body[:1000])
            self.wfile.flush()
            self.server.release.wait(10)
            self.close_connection = True
            return
        self.wfile.write(body)


@pytest.fixture
def server(monkeypatch):
    DOWNLOAD_CANCELLED.clear()
    monkeypatch.setattr(dl, "RETRY_WAIT", 0)
    monkeypatch.setattr(dl, "REQUEST_CHUNK_SIZE", CHUNK)
    monkeypatch.setattr(dl, "MIN_REQUEST_CHUNK", 4 * 1024)
    monkeypatch.setattr(dl, "MIN_RESUME_PROGRESS", 1024)
    server = _Server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.release.set()
    server.shutdown()
    server.server_close()
    DOWNLOAD_CANCELLED.clear()


@pytest.mark.parametrize("make_session", SESSIONS)
def test_plain_body_arrives_whole(tmp_path, server, make_session):
    save_path = tmp_path / "0.mp4"
    list(dl.download(url=f"{server.url}/plain", save_path=save_path, session=make_session()))

    assert save_path.read_bytes() == BODY
    assert len(server.hits) == 1


@pytest.mark.parametrize("make_session", SESSIONS)
def test_cut_body_resumes_with_range(tmp_path, server, make_session):
    save_path = tmp_path / "0.mp4"
    list(dl.download(url=f"{server.url}/cut", save_path=save_path, session=make_session()))

    assert save_path.read_bytes() == BODY
    assert server.hits[0][1] is None
    assert len(server.hits) > 1
    assert all(r is not None and r.startswith("bytes=") for _, r in server.hits[1:])


@pytest.mark.parametrize("make_session", SESSIONS)
def test_retry_after_header_sets_the_wait(tmp_path, server, make_session, monkeypatch):
    real = dl.retry_sleep
    waits: list[float] = []

    def spy(exc, attempts):
        waits.append(real(exc, attempts))
        return 0

    monkeypatch.setattr(dl, "retry_sleep", spy)
    save_path = tmp_path / "0.mp4"
    list(dl.download(url=f"{server.url}/busy", save_path=save_path, session=make_session()))

    assert save_path.read_bytes() == BODY
    assert len(server.hits) == 2
    assert waits == [1.0]


@pytest.mark.parametrize("make_session", SESSIONS)
def test_stalled_body_times_out_and_retries(tmp_path, server, make_session, monkeypatch):
    monkeypatch.setattr(dl, "READ_TIMEOUT", 1)
    monkeypatch.setattr("unshackle.core.config.config.network", {})
    save_path = tmp_path / "0.mp4"
    list(dl.download(url=f"{server.url}/stall", save_path=save_path, session=make_session()))

    assert save_path.read_bytes() == BODY
    assert len(server.hits) == 2


def test_complete_file_is_reported_without_a_request(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    save_path.write_bytes(b"x" * 10)
    events = list(dl.download(url=f"{server.url}/plain", save_path=save_path, session=rq.Session()))

    assert server.hits == []
    assert save_path.read_bytes() == b"x" * 10
    assert {"file_downloaded": save_path, "written": 10} in events


def test_legacy_layout_restarts_clean(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    save_path.write_bytes(b"partial")
    marker = save_path.with_name(save_path.name + ".!dev")
    marker.write_bytes(b"")
    list(dl.download(url=f"{server.url}/plain", save_path=save_path, session=rq.Session()))

    assert save_path.read_bytes() == BODY
    assert not marker.exists()
    assert server.hits[0][1] is None
