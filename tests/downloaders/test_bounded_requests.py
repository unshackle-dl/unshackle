import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests as rq

from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.downloaders.requests import download

# the package re-exports the `requests` function, shadowing the module of the same name
downloader = sys.modules[download.__module__]

BODY = bytes(range(256)) * 256  # 64 KiB
CUT_AFTER = 10 * 1024
CHUNK = 16 * 1024


class _CutServer(ThreadingHTTPServer):
    """Serves BODY but ends any body longer than CUT_AFTER early, like a per-request-limited CDN."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.ranges: list[str] = []
        self.cuts = 0
        self.connections = 0
        super().__init__(("127.0.0.1", 0), _CutHandler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}/seg.mp4"

    def handle_error(self, request, client_address):  # the client hanging up is expected here
        pass


class _CutHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.server.connections += 1

    def do_GET(self):
        header = self.headers.get("Range")
        self.server.ranges.append(header)
        start, end = 0, len(BODY) - 1
        if header:
            start_s, _, end_s = header.removeprefix("bytes=").partition("-")
            start, end = int(start_s), (int(end_s) if end_s else len(BODY) - 1)
        body = BODY[start : end + 1]
        self.send_response(206 if header else 200)
        if header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(BODY)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if len(body) > CUT_AFTER:
            self.server.cuts += 1
            self.wfile.write(body[:CUT_AFTER])
            self.close_connection = True
            return
        self.wfile.write(body)


@pytest.fixture
def server(monkeypatch):
    DOWNLOAD_CANCELLED.clear()
    monkeypatch.setattr(downloader, "RETRY_WAIT", 0)
    monkeypatch.setattr(downloader, "REQUEST_CHUNK_SIZE", CHUNK)
    monkeypatch.setattr(downloader, "MIN_REQUEST_CHUNK", 4 * 1024)
    monkeypatch.setattr(downloader, "MIN_RESUME_PROGRESS", 1024)
    server = _CutServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()
    DOWNLOAD_CANCELLED.clear()


def test_a_cut_body_bounds_the_following_requests(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    list(download(url=server.url, save_path=save_path, session=rq.Session()))

    assert save_path.read_bytes() == BODY
    # the open-ended first request is cut, the chunk-sized one after it is cut too, and the
    # halved chunk fits under the host's limit, so every later request completes whole
    assert server.cuts == 2
    spans = [r.removeprefix("bytes=").split("-") for r in server.ranges if r and r.split("-")[-1]]
    assert spans and all(int(end) - int(start) + 1 <= CHUNK for start, end in spans)
    # the bounded requests are cheap because they share one pooled connection
    assert server.connections < len(server.ranges)
