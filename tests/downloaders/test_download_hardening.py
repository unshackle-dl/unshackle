"""Downloader hardening invariants.

Pins the adaptive start-at-cap wiring, the tail-probe gate for small segments,
and the byte-range-slice protections: a server ignoring an item's Range (200
instead of 206) must fail the attempt rather than corrupt the merge, a retried
slice must be rewritten whole instead of resumed with a clobbered Range, and
media requests must ask for identity encoding.
"""

import importlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

dl = importlib.import_module("unshackle.core.downloaders.requests")

PARENT = bytes(range(256)) * 256  # 64KiB parent resource, distinctive bytes
SLICE_START = 1024
SLICE_END = 5119  # inclusive; 4KiB slice


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        with server.lock:
            server.requests.append(dict(self.headers))
            nth = len(server.requests)
        rng = self.headers.get("Range")
        if server.honor_range and rng:
            start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
            body = PARENT[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(PARENT)}")
        else:
            body = PARENT
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if server.truncate_first and nth == 1:
            self.wfile.write(body[: len(body) // 2])
            return  # connection closes mid-body -> client sees a short read
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.lock = threading.Lock()
    srv.requests = []
    srv.honor_range = True
    srv.truncate_first = False
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


def _url(srv, path="/parent.bin"):
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def _run(srv, tmp_path, urls, **kwargs):
    for _ in dl.requests(urls, output_dir=tmp_path, filename="seg_{i:04}.bin", **kwargs):
        pass
    return sorted(tmp_path.glob("seg_*.bin"))


def test_adaptive_controller_starts_at_cap(server, tmp_path, monkeypatch):
    seen = {}
    real = dl.AdaptiveWorkerController

    def recorder(*args, **kw):
        ctl = real(*args, **kw)
        seen["cap"], seen["start"] = ctl.cap, ctl.target
        return ctl

    monkeypatch.setattr(dl, "AdaptiveWorkerController", recorder)
    files = _run(server, tmp_path, [{"url": _url(server)} for _ in range(8)], max_workers=4, adaptive=True)
    assert len(files) == 8
    assert seen == {"cap": 4, "start": 4}


def test_small_segments_never_probed(server, tmp_path, monkeypatch):
    def no_probe(*args, **kw):
        raise AssertionError("tail boost probed a small-segment batch")

    monkeypatch.setattr(dl, "_probe_ranged", no_probe)
    files = _run(server, tmp_path, [{"url": _url(server)} for _ in range(30)], max_workers=4, adaptive=True)
    assert len(files) == 30
    assert all(f.read_bytes() == PARENT for f in files)


def test_range_item_ignored_by_server_fails_not_corrupts(server, tmp_path, monkeypatch):
    server.honor_range = False
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)
    urls = [{"url": _url(server), "headers": {"Range": f"bytes={SLICE_START}-{SLICE_END}"}}]
    with pytest.raises(Exception):
        _run(server, tmp_path, urls, max_workers=1)
    # nothing finalized: a 200 full-parent body must never become the segment
    assert not list(tmp_path.glob("seg_*.bin"))


def test_range_item_retry_rewrites_slice(server, tmp_path, monkeypatch):
    server.truncate_first = True
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)
    urls = [{"url": _url(server), "headers": {"Range": f"bytes={SLICE_START}-{SLICE_END}"}}]
    files = _run(server, tmp_path, urls, max_workers=1)
    assert files[0].read_bytes() == PARENT[SLICE_START : SLICE_END + 1]
    # the retry must re-send the item's own Range, never a resume Range from the partial
    ranges = [r.get("Range") for r in server.requests]
    assert ranges == [f"bytes={SLICE_START}-{SLICE_END}"] * len(ranges)
    assert len(ranges) >= 2


def test_segment_requests_ask_identity_encoding(server, tmp_path):
    _run(server, tmp_path, [{"url": _url(server)}], max_workers=1)
    assert all(r.get("Accept-Encoding") == "identity" for r in server.requests)
