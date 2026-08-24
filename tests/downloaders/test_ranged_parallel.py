"""Ranged-parallel single-URL download invariants.

Pins the byte-range fan-out that splits one large file across parts:

- a range-capable server above the size threshold is split into several byte
  windows, the parts merge byte-identical, and the ``.!dev`` control marker is
  removed on success;
- a ranged part that receives a 200 (server ignored the Range) fails after
  retries rather than writing a wrong-length body into the pre-truncated file;
- that failure sets only the ranged download's LOCAL abort event, never the
  process-global DOWNLOAD_CANCELLED (which would poison sibling tracks);
- ``probe_ranged`` parses the Content-Range total on a 206 and declines a 200
  or a content-encoded 206;
- the public single-URL path falls back to a sequential download only when the
  server does not really support ranges, and re-raises a 4xx instead of paying
  MAX_ATTEMPTS a second time from byte 0.

Tests 2/3 exercise ``dispatch_parts`` directly: the public path swallows a
range-support failure and falls back, so the raise that the invariant guarantees
is only observable at the dispatch layer.
"""

import importlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from requests import HTTPError, Session

dl = importlib.import_module("unshackle.core.downloaders.requests")

PAYLOAD = bytes(range(256)) * 128  # 32 KiB, distinctive repeating byte pattern


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _RangeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        srv = self.server
        rng = self.headers.get("Range")
        with srv.lock:
            srv.requests.append(rng)
        mode = srv.mode

        if mode == "gzip_probe":
            # a 206 that (wrongly) carries a content-encoding: the probe must decline it
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-0/{len(PAYLOAD)}")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(b"\x00")
            return

        if mode == "deny_parts" and rng != "bytes=0-0":
            # probe passes, every part window is refused for good (expired token, geo block)
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # always_200 ignores Range entirely; probe_only_206 answers the 0-0 probe
        # with a real 206 but hands every wider part window a 200 full body
        serve_full = mode == "always_200" or not rng or (mode == "probe_only_206" and rng != "bytes=0-0")
        if serve_full:
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
            return

        start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
        body = PAYLOAD[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(autouse=True)
def clear_cancelled():
    # ranged failures must never touch the process-global flag; isolate every test from it
    dl.DOWNLOAD_CANCELLED.clear()
    yield
    dl.DOWNLOAD_CANCELLED.clear()


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    srv.lock = threading.Lock()
    srv.requests = []
    srv.mode = "range"  # honor every Range with a proper 206
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


def url(srv, path="/payload.bin"):
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def run(srv, tmp_path, urls, **kwargs):
    for _ in dl.requests(urls, output_dir=tmp_path, filename="seg_{i:04}.bin", **kwargs):
        pass
    return sorted(tmp_path.glob("seg_*.bin"))


def test_ranged_parallel_merges_byte_identical(server, tmp_path, monkeypatch):
    # shrink the thresholds so a 32 KiB payload splits into several parts
    monkeypatch.setattr(dl, "RANGE_PARALLEL_MIN_SIZE", 8 * 1024)
    monkeypatch.setattr(dl, "RANGE_PARALLEL_PART_SIZE", 2 * 1024)
    files = run(server, tmp_path, [{"url": url(server)}], max_workers=4)
    assert len(files) == 1
    assert files[0].read_bytes() == PAYLOAD
    # the split must actually have happened: >1 distinct window beyond the 0-0 probe
    windows = {r for r in server.requests if r and r != "bytes=0-0"}
    assert len(windows) >= 2
    # control marker removed once the parts finalize
    assert not files[0].with_name(files[0].name + ".!dev").exists()


def test_ranged_part_requires_206(server, tmp_path, monkeypatch):
    server.mode = "probe_only_206"
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)
    save_path = tmp_path / "seg_0000.bin"
    gen = dl.dispatch_parts(
        url=url(server),
        save_path=save_path,
        session=Session(),
        total_size=len(PAYLOAD),
        max_workers=4,
    )
    with pytest.raises(IOError, match="expected 206 for ranged part"):
        for _ in gen:
            pass
    # not finalized: the control marker survives (completed=False) and the target
    # is still the pre-truncated zero-fill, never the served bytes
    assert save_path.with_name(save_path.name + ".!dev").exists()
    assert save_path.read_bytes() != PAYLOAD


def test_ranged_part_failure_stays_local(server, tmp_path, monkeypatch):
    server.mode = "probe_only_206"
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)
    dl.DOWNLOAD_CANCELLED.clear()
    save_path = tmp_path / "seg_0000.bin"
    gen = dl.dispatch_parts(
        url=url(server),
        save_path=save_path,
        session=Session(),
        total_size=len(PAYLOAD),
        max_workers=4,
    )
    with pytest.raises(IOError, match="expected 206 for ranged part"):
        for _ in gen:
            pass
    # a failed part rides the local abort event; the global cancel must stay clear
    assert not dl.DOWNLOAD_CANCELLED.is_set()


def test_probe_ranged_parses_content_range(server):
    session = Session()
    server.mode = "range"
    assert dl.probe_ranged(url(server), session) == (len(PAYLOAD), True)
    server.mode = "always_200"
    assert dl.probe_ranged(url(server), session) == (0, False)
    server.mode = "gzip_probe"
    assert dl.probe_ranged(url(server), session) == (0, False)


def test_permanent_part_failure_is_not_retried_sequentially(server, tmp_path, monkeypatch):
    server.mode = "deny_parts"
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)
    monkeypatch.setattr(dl, "RANGE_PARALLEL_MIN_SIZE", 8 * 1024)
    monkeypatch.setattr(dl, "RANGE_PARALLEL_PART_SIZE", 8 * 1024)
    with pytest.raises(HTTPError):
        run(server, tmp_path, [{"url": url(server)}], max_workers=4)
    # a sequential fallback would issue a plain unranged GET; only the probe and the
    # part windows may appear, each part capped at MAX_ATTEMPTS
    assert [r for r in server.requests if r is None] == []
    parts = len({r for r in server.requests if r and r != "bytes=0-0"})
    assert len(server.requests) <= 1 + parts * dl.MAX_ATTEMPTS
    # nothing half-written is left behind for a later run to mistake for a finished file
    assert sorted(tmp_path.glob("seg_*")) == []


def test_range_unsupported_falls_back_to_sequential(server, tmp_path, monkeypatch):
    server.mode = "probe_only_206"
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)
    monkeypatch.setattr(dl, "RANGE_PARALLEL_MIN_SIZE", 8 * 1024)
    monkeypatch.setattr(dl, "RANGE_PARALLEL_PART_SIZE", 8 * 1024)
    files = run(server, tmp_path, [{"url": url(server)}], max_workers=4)
    assert len(files) == 1
    assert files[0].read_bytes() == PAYLOAD
    assert not files[0].with_name(files[0].name + ".!dev").exists()


def test_permanent_status_unwraps_causes():
    assert dl.permanent_status(HTTPError(response=_Resp(403))) == 403
    assert dl.permanent_status(HTTPError(response=_Resp(429))) is None
    assert dl.permanent_status(HTTPError(response=_Resp(503))) is None
    assert dl.permanent_status(IOError("short read")) is None
    # RnetSession wraps the HTTPError one level down as __cause__
    wrapped = RuntimeError("max retries")
    wrapped.__cause__ = HTTPError(response=_Resp(404))
    assert dl.permanent_status(wrapped) == 404
