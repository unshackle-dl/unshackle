"""A bounded sub-range that returns no bytes must fail, not spin.

After a cut, the sequential path asks for bounded sub-ranges and treats a whole
sub-range as progress rather than a retry. A response with an empty body of
unknown length (a chunked 206, a 204, a 304) makes no progress, so it must
raise and spend an attempt, or the same request repeats forever.
"""

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
DEADLINE = 30


class _EmptyRangeServer(ThreadingHTTPServer):
    """Cuts the first body, then answers every bounded range with an empty chunked 206."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.ranges: list[str] = []
        super().__init__(("127.0.0.1", 0), _EmptyRangeHandler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}/seg.mp4"

    def handle_error(self, request, client_address):  # the client hanging up is expected here
        pass


class _EmptyRangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        header = self.headers.get("Range")
        self.server.ranges.append(header)
        start, end, bounded = 0, len(BODY) - 1, False
        if header:
            start_s, _, end_s = header.removeprefix("bytes=").partition("-")
            start, end = int(start_s), (int(end_s) if end_s else len(BODY) - 1)
            bounded = bool(end_s)
        if bounded:
            # a valid response: the chunked terminator closes a zero-length body, so requests raises nothing
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(BODY)}")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"0\r\n\r\n")
            return
        body = BODY[start : end + 1]
        self.send_response(206 if header else 200)
        if header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(BODY)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        # the cut teaches the downloader to bound the requests that follow
        self.wfile.write(body[:CUT_AFTER])
        self.close_connection = True


@pytest.fixture
def server(monkeypatch):
    DOWNLOAD_CANCELLED.clear()
    monkeypatch.setattr(downloader, "RETRY_WAIT", 0)
    monkeypatch.setattr(downloader, "REQUEST_CHUNK_SIZE", CHUNK)
    monkeypatch.setattr(downloader, "MIN_REQUEST_CHUNK", 4 * 1024)
    monkeypatch.setattr(downloader, "MIN_RESUME_PROGRESS", 1024)
    server = _EmptyRangeServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()
    DOWNLOAD_CANCELLED.clear()


def test_empty_sub_range_fails_instead_of_looping(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    result: list[BaseException] = []

    def run() -> None:
        try:
            list(download(url=server.url, save_path=save_path, session=rq.Session()))
        except BaseException as exc:
            result.append(exc)

    # a hard deadline: a regression hangs this thread, and the suite must fail, not stall
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=DEADLINE)

    assert not worker.is_alive(), f"download still running after {DEADLINE}s ({len(server.ranges)} requests)"
    assert result and isinstance(result[0], IOError), f"expected an IOError, got {result!r}"
    assert "no bytes" in str(result[0])
    assert len(server.ranges) <= downloader.MAX_ATTEMPTS + 1
    assert not save_path.exists()
