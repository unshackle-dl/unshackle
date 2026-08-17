"""Hedged segment racers must release their file handles before the batch completes.

A superseded loser that keeps its .!dev handle open makes the manifest merge's
stray sweep raise PermissionError (WinError 32) on Windows. This test forces a
hedge with a trickling first response and checks the invariant at the exact
moment merge would run.
"""

import importlib
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from unshackle.core.constants import DOWNLOAD_CANCELLED

# the downloaders package re-exports the requests() function, shadowing the submodule
dl = importlib.import_module("unshackle.core.downloaders.requests")

SEGMENTS = 10
SLOW_INDEX = 4
FAST_BODY = b"F" * 4096
SLOW_BODY = b"S" * 40960
TRICKLE_CHUNK = 2048
TRICKLE_DELAY = 0.05


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with self.server.count_lock:
            self.server.counts[self.path] = self.server.counts.get(self.path, 0) + 1
            nth = self.server.counts[self.path]
        slow_path = self.path == f"/seg/{SLOW_INDEX}"
        body = SLOW_BODY if slow_path else FAST_BODY
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if slow_path and nth == 1:
            # trickle only the first attempt so the hedge retry wins instantly
            for i in range(0, len(body), TRICKLE_CHUNK):
                try:
                    self.wfile.write(body[i : i + TRICKLE_CHUNK])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(TRICKLE_DELAY)
        else:
            self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def trickle_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.counts = {}
    server.count_lock = threading.Lock()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.mark.unit
def test_hedge_loser_releases_handles_before_merge(tmp_path, trickle_server, monkeypatch):
    monkeypatch.setattr(dl, "HEDGE_MIN_WAIT", 0.3)
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.1)
    DOWNLOAD_CANCELLED.clear()
    threads_before = {t.ident for t in threading.enumerate()}

    port = trickle_server.server_address[1]
    urls = [f"http://127.0.0.1:{port}/seg/{i}" for i in range(SEGMENTS)]
    start = time.monotonic()
    for _event in dl.requests(urls=urls, output_dir=tmp_path, filename="{i:02}.mp4", max_workers=4):
        pass
    wall = time.monotonic() - start

    with trickle_server.count_lock:
        slow_hits = trickle_server.counts[f"/seg/{SLOW_INDEX}"]
    assert slow_hits == 2, "hedge never fired; the race under test was not exercised"

    # the batch must not stall draining the loser's stream (trickle alone takes ~1s)
    assert wall < 5

    for i in range(SEGMENTS):
        expected = SLOW_BODY if i == SLOW_INDEX else FAST_BODY
        assert (tmp_path / f"{i:02}.mp4").read_bytes() == expected

    strays = list(tmp_path.glob("*.!dev"))
    fd_dir = Path("/proc/self/fd")
    if fd_dir.exists():
        open_targets = set()
        for fd in fd_dir.iterdir():
            try:
                open_targets.add(os.readlink(fd))
            except OSError:
                continue
        held = [s.name for s in strays if str(s) in open_targets]
        assert not held, f"racer still holds open handles at completion: {held}"
    for stray in strays:
        stray.unlink()  # raises PermissionError (WinError 32) on Windows if a handle is open

    leaked = [
        t.name
        for t in threading.enumerate()
        if t.ident not in threads_before and t.name.startswith("ThreadPoolExecutor")
    ]
    assert not leaked, f"racer threads still alive at completion: {leaked}"
