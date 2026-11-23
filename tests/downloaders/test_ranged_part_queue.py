"""The workers of a ranged single-file download share fixed-size parts from one queue.

A connection that the host holds keeps one part, and the other worker completes the rest.
"""

import importlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from requests import Session

from unshackle.core.constants import DOWNLOAD_CANCELLED

dl = importlib.import_module("unshackle.core.downloaders.requests")

BODY = bytes(range(256)) * 256


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.lock = threading.Lock()
        self.served: list[str] = []
        self.held: str | None = None
        self.release = threading.Event()
        self.others_done_at_release = -1
        super().__init__(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}/big.bin"

    def handle_error(self, request, client_address):
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        server = self.server
        header = self.headers.get("Range")
        start, end = 0, len(BODY) - 1
        if header:
            start_s, _, end_s = header.removeprefix("bytes=").partition("-")
            start, end = int(start_s), (int(end_s) if end_s else len(BODY) - 1)
        body = BODY[start : end + 1]
        with server.lock:
            held = server.held is None
            if held:
                server.held = header
        if held:
            server.release.wait(5)
            with server.lock:
                server.others_done_at_release = len(server.served)
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(BODY)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with server.lock:
            server.served.append(header)
            if not held and len(server.served) == 7:
                server.release.set()


def test_fast_connection_takes_the_parts_a_held_connection_cannot(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "RANGE_PARALLEL_PART_SIZE", 8 * 1024)
    monkeypatch.setattr(dl, "RANGE_PARALLEL_MIN_PART_SIZE", 2 * 1024)
    DOWNLOAD_CANCELLED.clear()
    server = _Server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    save_path = tmp_path / "big.bin"
    try:
        for _ in dl.dispatch_parts(
            url=server.url, save_path=save_path, session=Session(), total_size=len(BODY), max_workers=2
        ):
            pass
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        DOWNLOAD_CANCELLED.clear()

    assert save_path.read_bytes() == BODY
    assert len(set(server.served)) == 8
    assert server.others_done_at_release == 7
