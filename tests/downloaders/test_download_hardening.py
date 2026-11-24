"""Downloader hardening invariants.

Pins the adaptive start-at-cap wiring, the tail-probe gate for small segments,
and the byte-range-slice protections: a server ignoring an item's Range (200
instead of 206) must fail the attempt rather than corrupt the merge, a retried
slice must be rewritten whole instead of resumed with a clobbered Range, and
media requests must ask for identity encoding.
"""

import importlib
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

dl = importlib.import_module("unshackle.core.downloaders.requests")

PARENT = bytes(range(256)) * 256
SLICE_START = 1024
SLICE_END = 5119


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        with server.lock:
            server.requests.append(dict(self.headers))
            nth = len(server.requests)
        if nth <= server.fail_429_first:
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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
            return
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
    srv.fail_429_first = 0
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


def url(srv, path="/parent.bin"):
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def run(srv, tmp_path, urls, **kwargs):
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
    files = run(server, tmp_path, [{"url": url(server)} for _ in range(8)], max_workers=4, adaptive=True)
    assert len(files) == 8
    assert seen == {"cap": 4, "start": 4}


def test_small_segments_never_probed(server, tmp_path, monkeypatch):
    def no_probe(*args, **kw):
        raise AssertionError("tail boost probed a small-segment batch")

    monkeypatch.setattr(dl, "probe_ranged", no_probe)
    files = run(server, tmp_path, [{"url": url(server)} for _ in range(30)], max_workers=4, adaptive=True)
    assert len(files) == 30
    assert all(f.read_bytes() == PARENT for f in files)


def test_range_item_ignored_by_server_fails_not_corrupts(server, tmp_path, monkeypatch):
    server.honor_range = False
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)
    urls = [{"url": url(server), "headers": {"Range": f"bytes={SLICE_START}-{SLICE_END}"}}]
    with pytest.raises(Exception):
        run(server, tmp_path, urls, max_workers=1)
    assert not list(tmp_path.glob("seg_*.bin"))


def test_range_item_spanning_whole_resource_accepts_200(server, tmp_path):
    server.honor_range = False
    urls = [{"url": url(server), "headers": {"Range": f"bytes=0-{len(PARENT) - 1}"}}]
    files = run(server, tmp_path, urls, max_workers=1)
    assert files[0].read_bytes() == PARENT
    assert len(server.requests) == 1


def test_range_item_retry_rewrites_slice(server, tmp_path, monkeypatch):
    server.truncate_first = True
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)
    urls = [{"url": url(server), "headers": {"Range": f"bytes={SLICE_START}-{SLICE_END}"}}]
    files = run(server, tmp_path, urls, max_workers=1)
    assert files[0].read_bytes() == PARENT[SLICE_START : SLICE_END + 1]
    ranges = [r.get("Range") for r in server.requests]
    assert ranges == [f"bytes={SLICE_START}-{SLICE_END}"] * len(ranges)
    assert len(ranges) >= 2


def test_segment_requests_ask_identity_encoding(server, tmp_path):
    run(server, tmp_path, [{"url": url(server)}], max_workers=1)
    assert all(r.get("Accept-Encoding") == "identity" for r in server.requests)


def exc_with_headers(headers):
    exc = Exception("boom")
    exc.response = SimpleNamespace(headers=headers)
    return exc


def test_retry_sleep_honors_numeric_retry_after():
    assert dl.retry_sleep(exc_with_headers({"Retry-After": "3"}), 1) == 3.0
    assert dl.retry_sleep(exc_with_headers({"Retry-After": "9999"}), 1) == 60.0


def test_retry_sleep_honors_http_date_retry_after():
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10), usegmt=True)
    assert 5 <= dl.retry_sleep(exc_with_headers({"Retry-After": when}), 1) <= 10


def test_retry_sleep_exponential_backoff_with_jitter():
    for attempts, base in ((1, dl.RETRY_WAIT), (2, dl.RETRY_WAIT * 2), (3, dl.RETRY_WAIT * 4)):
        assert base <= dl.retry_sleep(Exception("reset"), attempts) <= base * 1.1
    assert dl.retry_sleep(Exception("reset"), 30) == 60.0


def test_retry_sleep_invalid_retry_after_falls_back():
    v = dl.retry_sleep(exc_with_headers({"Retry-After": "soon"}), 1)
    assert dl.RETRY_WAIT <= v <= dl.RETRY_WAIT * 1.1


def test_retry_sleep_naive_http_date_treated_as_utc():
    when = format_datetime(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=10))
    assert when.endswith("-0000")
    v = dl.retry_sleep(exc_with_headers({"Retry-After": when}), 1)
    assert 5 <= v <= 10


def test_retry_sleep_nan_retry_after_falls_back():
    v = dl.retry_sleep(exc_with_headers({"Retry-After": "nan"}), 1)
    assert dl.RETRY_WAIT <= v <= dl.RETRY_WAIT * 1.1


def test_retry_sleep_honors_retry_after_on_wrapped_cause():
    exc = Exception("max retries exceeded")
    exc.__cause__ = exc_with_headers({"Retry-After": "3"})
    assert dl.retry_sleep(exc, 1) == 3.0


def test_transient_429_with_retry_after_does_not_abort_track(server, tmp_path, monkeypatch):
    dl.DOWNLOAD_CANCELLED.clear()
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)
    server.fail_429_first = 2
    files = run(server, tmp_path, [{"url": url(server, f"/seg{i}.bin")} for i in range(3)], max_workers=1)
    assert len(files) == 3
    assert all(f.read_bytes() == PARENT for f in files)
    assert not dl.DOWNLOAD_CANCELLED.is_set()
    assert len(server.requests) == 5


def test_session_retry_does_not_nest_inside_an_attempt(server, tmp_path, monkeypatch):
    from requests import Session
    from requests.adapters import HTTPAdapter, Retry

    dl.DOWNLOAD_CANCELLED.clear()
    server.fail_429_first = 10**6
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)
    session = Session()
    adapter = HTTPAdapter(
        max_retries=Retry(total=5, backoff_factor=0, status_forcelist=[429, 500, 502, 503, 504]),
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    with pytest.raises(Exception):
        run(server, tmp_path, [{"url": url(server)}], max_workers=1, session=session)

    assert len(server.requests) == dl.MAX_ATTEMPTS
    assert session.get_adapter("http://x/").max_retries.total == 5
