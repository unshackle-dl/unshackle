"""Cancel/abort hardening for the downloader.

Pins two teardown-safety invariants:

- ``dispatch_parts`` must NOT finalize when the process-global ``DOWNLOAD_CANCELLED``
  fires mid-download: part workers return silently keeping partials, so every future
  completes without error, and finalizing would strip the ``.!dev`` control file and
  pass off a hole-filled pre-truncated file as complete on the next run.
- A worker parked in the retry backoff wait (which can reach MAX_BACKOFF) must wake and
  exit as soon as the batch abort is set, instead of sleeping out the full delay and
  stalling shutdown or the merge.
"""

import importlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from requests import Session

dl = importlib.import_module("unshackle.core.downloaders.requests")

pytestmark = pytest.mark.unit

BODY = b"x" * 4096


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        if server.always_status:
            self.send_response(server.always_status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rng = self.headers.get("Range")
        if rng:
            start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
            body = BODY[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(BODY)}")
        else:
            body = BODY
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if server.gate is not None:
            self.wfile.write(body[:64])
            self.wfile.flush()
            server.started.set()
            server.gate.wait(timeout=10)
            self.wfile.write(body[64:])
        else:
            self.wfile.write(body)

    def log_message(self, *args):  # keep the test output quiet
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # cancelled readers close mid-write
        pass


@pytest.fixture
def server():
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.always_status = 0
    srv.gate = None
    srv.started = threading.Event()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    if srv.gate is not None:
        srv.gate.set()
    srv.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clear_cancel():
    dl.DOWNLOAD_CANCELLED.clear()
    yield
    dl.DOWNLOAD_CANCELLED.clear()


def _url(server: _Server) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}/seg.bin"


def test_dispatch_parts_does_not_finalize_on_global_cancel(server, tmp_path):
    server.gate = threading.Event()
    save_path = tmp_path / "big.bin"

    def _cancel_when_started() -> None:
        assert server.started.wait(timeout=10)
        dl.DOWNLOAD_CANCELLED.set()
        server.gate.set()

    canceller = threading.Thread(target=_cancel_when_started)
    canceller.start()
    events = list(
        dl.dispatch_parts(
            url=_url(server),
            save_path=save_path,
            session=Session(),
            total_size=len(BODY),
            max_workers=2,
        )
    )
    canceller.join(timeout=10)

    assert not any("file_downloaded" in ev for ev in events)
    # the control file must survive so the next run restarts instead of trusting the file
    assert save_path.with_name(f"{save_path.name}.!dev").exists()


def test_backoff_wait_exits_promptly_on_batch_abort(server, tmp_path, monkeypatch):
    server.always_status = 503
    monkeypatch.setattr(dl, "retry_sleep", lambda exc, attempts: 30.0)
    abort = threading.Event()

    def _consume() -> None:
        for _ in dl.download(
            url=_url(server),
            save_path=tmp_path / "seg.bin",
            session=Session(),
            segmented=True,
            abort=abort,
        ):
            pass

    worker = threading.Thread(target=_consume)
    start = time.monotonic()
    worker.start()
    time.sleep(0.5)  # let the first attempt fail and enter the backoff wait
    abort.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    # generous bound: far below the 30s nap, so the wait was interrupted, not slept out
    assert time.monotonic() - start < 10
