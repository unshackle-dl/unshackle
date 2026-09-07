"""Submission-window invariants for the segmented drain loop.

Segments are not all submitted up front: that convoys on the pool's global lock and starves
the event drain. The loop meters submission through a window it tops up. The window must hold
two segments for every worker, so a worker that finishes one finds the next already staged
instead of idling until the next drain cycle. Fixed and adaptive mode must agree on that
ratio; adaptive sizes it from its live target instead of from ``max_workers``.

The test stalls every response, so no future can complete and the queue settles at exactly
the window. Counting ``pool.submit`` calls makes the queue depth observable: a blocked
server only ever sees ``max_workers`` requests, whatever is staged behind them.
"""

import importlib
import threading
import time
from concurrent.futures.thread import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

dl = importlib.import_module("unshackle.core.downloaders.requests")

BODY = b"segment-body" * 64
MAX_WORKERS = 4
SEG_COUNT = 20  # more than the window, so queue depth is the only bound

pytestmark = pytest.mark.unit


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.release.wait(timeout=10)
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.release = threading.Event()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.release.set()
    srv.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def clean_cancel():
    dl.DOWNLOAD_CANCELLED.clear()
    yield
    dl.DOWNLOAD_CANCELLED.clear()


@pytest.fixture
def submissions(monkeypatch):
    counter: list[int] = []

    class _CountingPool(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            counter.append(1)  # list.append is atomic, so no lock is needed here
            return super().submit(fn, *args, **kwargs)

    monkeypatch.setattr(dl, "ThreadPoolExecutor", _CountingPool)
    return counter


@pytest.mark.parametrize("adaptive", [False, True])
def test_queue_holds_two_segments_per_worker(server, submissions, tmp_path, adaptive):
    urls = [{"url": f"http://127.0.0.1:{server.server_address[1]}/seg{i}.bin"} for i in range(SEG_COUNT)]
    events: list[dict] = []

    def run():
        events.extend(
            dl.requests(
                urls,
                output_dir=tmp_path,
                filename="seg_{i:04}.bin",
                max_workers=MAX_WORKERS,
                adaptive=adaptive,
            )
        )

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        deadline = time.time() + 3.0
        while time.time() < deadline and len(submissions) < 2 * MAX_WORKERS:
            time.sleep(0.02)
        time.sleep(0.2)  # settle, so an over-filled window is caught as well as an under-filled one
        assert len(submissions) == 2 * MAX_WORKERS
    finally:
        server.release.set()
        worker.join(timeout=30)

    assert not worker.is_alive()
    assert sum(event.get("advance", 0) for event in events) == SEG_COUNT
    assert len(sorted(tmp_path.glob("seg_*.bin"))) == SEG_COUNT
