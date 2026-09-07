"""``probe_ranged`` cost and fidelity.

The probe asks for one byte to learn the complete length and whether the host honours
ranges. It runs once for every single-URL download and once per tail-boost candidate,
so what it costs and what it sends both matter:

- it must leave the connection reusable. A streamed response whose body is never read
  cannot go back in the pool. Every probe would then tear down a socket, and the next
  request would pay a fresh handshake.
- it must ask for an unencoded body like ``download()`` does. A CDN that answers a
  probe with a Content-Encoding makes the probe decline, and the ranged-parallel path
  is silently lost for a file that supports it.
- it must carry the URL item's own request options. Without them a host that needs an
  auth header answers the probe with a 4xx, and the download falls back to one
  connection even though the ranges were there.

The drain is bounded by the declared Content-Length, not by the status code. A host
that ignores Range may answer 200 with the whole resource, and one that answers 206
with the whole resource is just as possible. Reading either would download the file
only to decide not to use it.
"""

import importlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from requests import Session

from unshackle.core.constants import DOWNLOAD_CANCELLED

# the package re-exports the `requests` function, shadowing the module of the same name
dl = importlib.import_module("unshackle.core.downloaders.requests")

PAYLOAD = bytes((i * 11 + 3) % 256 for i in range(32 * 1024))
BIG = bytes(8 * 1024 * 1024)  # larger than any socket buffer, so a full drain is visible
PROBES = 6
AUTH = "let-me-in"


class _ProbeServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list = []  # (Range, Accept-Encoding, X-Auth)
        self.connections = 0
        self.mode = "range"
        self.written = 0
        self.broke = False
        super().__init__(("127.0.0.1", 0), _ProbeHandler)

    def handle_error(self, request, client_address):  # the client hanging up is expected here
        pass


class _ProbeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def do_GET(self):
        srv = self.server
        rng = self.headers.get("Range")
        with srv.lock:
            srv.requests.append((rng, self.headers.get("Accept-Encoding"), self.headers.get("X-Auth")))

        if srv.mode == "needs_auth" and self.headers.get("X-Auth") != AUTH:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if srv.mode == "ignores_range":
            self.send_response(200)
            self.send_header("Content-Length", str(len(BIG)))
            self.end_headers()
            self._write_slowly(BIG)
            return

        if srv.mode == "fat_206":
            # a 206 status on a whole-resource body: the status alone cannot bound the drain
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-{len(BIG) - 1}/{len(BIG)}")
            self.send_header("Content-Length", str(len(BIG)))
            self.end_headers()
            self._write_slowly(BIG)
            return

        if not rng:
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
            return

        start_s, _, end_s = rng.removeprefix("bytes=").partition("-")
        start = int(start_s)
        end = int(end_s) if end_s else len(PAYLOAD) - 1
        body = PAYLOAD[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
        self.send_header("Content-Length", str(len(body)))
        if srv.mode == "encode_unless_identity" and self.headers.get("Accept-Encoding") != "identity":
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)

    def _write_slowly(self, body):
        # chunked writes so the count records how far the client actually read
        srv = self.server
        try:
            for at in range(0, len(body), 64 * 1024):
                self.wfile.write(body[at : at + 64 * 1024])
                with srv.lock:
                    srv.written += len(body[at : at + 64 * 1024])
        except OSError:
            with srv.lock:
                srv.broke = True
            self.close_connection = True


@pytest.fixture
def server(monkeypatch):
    DOWNLOAD_CANCELLED.clear()
    monkeypatch.setattr(dl, "RETRY_WAIT", 0)
    srv = _ProbeServer()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)
    srv.server_close()
    DOWNLOAD_CANCELLED.clear()


def url(srv, path="/payload.bin"):
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def test_probe_reuses_the_pooled_connection(server):
    session = Session()

    for _ in range(PROBES):
        assert dl.probe_ranged(url(server), session) == (len(PAYLOAD), True)

    assert server.connections == 1


def test_probe_does_not_drain_a_200_body(server):
    server.mode = "ignores_range"
    session = Session()

    assert dl.probe_ranged(url(server), session) == (0, False)

    for _ in range(50):
        with server.lock:
            if server.broke or server.written >= len(BIG):
                break
        time.sleep(0.1)
    with server.lock:
        assert server.written < len(BIG)


def test_probe_asks_for_identity_encoding(server):
    server.mode = "encode_unless_identity"
    session = Session()

    assert dl.probe_ranged(url(server), session) == (len(PAYLOAD), True)
    assert server.requests[-1][1] == "identity"

    # a caller that names its own encoding still wins
    assert dl.probe_ranged(url(server), session, headers={"Accept-Encoding": "gzip"}) == (0, False)
    assert server.requests[-1][1] == "gzip"


def test_probe_uses_the_url_items_headers(server, tmp_path, monkeypatch):
    server.mode = "needs_auth"
    monkeypatch.setattr(dl, "RANGE_PARALLEL_MIN_SIZE", 8 * 1024)
    item = {"url": url(server), "headers": {"X-Auth": AUTH}}

    for _ in dl.requests([item], output_dir=tmp_path, filename="seg_{i:04}.bin", max_workers=4):
        pass

    save_path = tmp_path / "seg_0000.bin"
    assert save_path.read_bytes() == PAYLOAD
    probes = [r for r in server.requests if r[0] == "bytes=0-0"]
    assert probes and all(r[2] == AUTH for r in probes)
    # the probe was answered, so the ranged path ran instead of one sequential connection
    assert [r for r in server.requests if r[0] and r[0] != "bytes=0-0"]


def test_probe_does_not_drain_a_fat_206_body(server):
    """A 206 carrying the whole resource must not be read to decide whether to use ranges."""
    server.mode = "fat_206"
    session = Session()

    assert dl.probe_ranged(url(server), session) == (len(BIG), True)

    for _ in range(50):
        with server.lock:
            if server.broke or server.written >= len(BIG):
                break
        time.sleep(0.1)
    with server.lock:
        assert server.written < len(BIG)
