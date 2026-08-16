"""Regression tests: no locked file handles or leaked worker threads after a batch ends.

Covers the WinError 32 family: a racer leaked past a no-wait shutdown must never hold
(or reopen) its .!dev handle once the batch is over, even after the process-global
cancel flag is cleared for the next track. On Windows the unlink assertions fail
outright if a handle is still open; on POSIX the thread accounting catches the
same leak.
"""

from __future__ import annotations

import http.server
import importlib
import socketserver
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.downloaders.requests import requests as download_batch

dl_mod = importlib.import_module("unshackle.core.downloaders.requests")

SEG_SIZE = 256 * 1024
FAST = {"READ_TIMEOUT": 1, "RETRY_WAIT": 0.2, "MAX_ATTEMPTS": 2, "HEDGE_MIN_WAIT": 0.3}


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        self.body = (b"unshackle-test-" * (SEG_SIZE // 15 + 1))[:SEG_SIZE]
        self.stall_release = threading.Event()
        super().__init__(("127.0.0.1", 0), _Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:  # expected fault noise
        pass


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: Any) -> None:
        pass

    def do_GET(self) -> None:
        server: _Server = self.server  # type: ignore[assignment]
        body = server.body
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.path.startswith("/stall/"):  # half the body, then hold: never completes
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            server.stall_release.wait(30)
        elif self.path.startswith("/slow/"):  # trickle so a cancel can land mid-body
            for off in range(0, len(body), 16 * 1024):
                self.wfile.write(body[off : off + 16 * 1024])
                self.wfile.flush()
                time.sleep(0.05)
        else:
            self.wfile.write(body)


@pytest.fixture()
def server() -> Iterator[_Server]:
    srv = _Server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.stall_release.set()
    srv.shutdown()


@pytest.fixture(autouse=True)
def fast_timeouts() -> Iterator[None]:
    saved = {k: getattr(dl_mod, k) for k in FAST}
    for k, v in FAST.items():
        setattr(dl_mod, k, v)
    DOWNLOAD_CANCELLED.clear()
    yield
    for k, v in saved.items():
        setattr(dl_mod, k, v)
    DOWNLOAD_CANCELLED.clear()


def pool_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name.startswith("ThreadPoolExecutor"))


def assert_drained_and_unlocked(out_dir: Path, baseline: int) -> None:
    """Workers must exit within one read timeout; then every stray must be deletable."""
    deadline = time.monotonic() + FAST["READ_TIMEOUT"] + FAST["RETRY_WAIT"] + 3
    while time.monotonic() < deadline and pool_threads() > baseline:
        time.sleep(0.05)
    assert pool_threads() <= baseline, "worker threads leaked past the drain deadline"
    for stray in out_dir.glob("*.!dev"):
        stray.unlink()  # PermissionError here on Windows = a handle is still held
    # a resurrected worker would reopen/recreate its .!dev within one retry cycle
    time.sleep(FAST["RETRY_WAIT"] + 0.3)
    assert not list(out_dir.glob("*.!dev")), "a leaked worker recreated its temp file after cleanup"


def test_failed_batch_frees_handles_even_after_flag_clear(server: _Server, tmp_path: Path) -> None:
    """Stall fault fails the batch; clearing DOWNLOAD_CANCELLED (as dl.py does for the
    next track) must not resurrect the leaked racer's retry loop."""
    baseline = pool_threads()
    host, port = str(server.server_address[0]), int(server.server_address[1])
    urls = [f"http://{host}:{port}/stall/0"] + [f"http://{host}:{port}/ok/{i}" for i in range(1, 4)]

    with pytest.raises(Exception):
        for _ in download_batch(urls=urls, output_dir=tmp_path, filename="seg_{i}.bin", max_workers=4):
            pass

    assert DOWNLOAD_CANCELLED.is_set()
    DOWNLOAD_CANCELLED.clear()  # what dl.py does after a failed track
    assert_drained_and_unlocked(tmp_path, baseline)


def test_external_cancel_frees_handles(server: _Server, tmp_path: Path) -> None:
    """DOWNLOAD_CANCELLED set mid-stream (sibling-track failure / SIGINT path): the batch
    must wind down without leaving locked handles or live workers."""
    baseline = pool_threads()
    host, port = str(server.server_address[0]), int(server.server_address[1])
    urls = [f"http://{host}:{port}/slow/{i}" for i in range(4)]

    for event in download_batch(urls=urls, output_dir=tmp_path, filename="seg_{i}.bin", max_workers=4):
        if "advance" in event or "written" in event:
            DOWNLOAD_CANCELLED.set()

    DOWNLOAD_CANCELLED.clear()
    assert_drained_and_unlocked(tmp_path, baseline)
