"""Multiprocess segment-download invariants.

Pins the spawned-child fan-out path in ``requests()``/``download_multiprocess``:

- Strided assignment plus the ``index_offset``/``index_stride`` filename mapping puts every
  segment's bytes in the correctly named file (a broken stride mapping misfiles content).
- The parent normalizes child progress to segment granularity: it drops child ``advance``
  and ``total``/speed events and emits exactly one ``advance=1`` per ``file_downloaded``, so
  the advances summed by a caller equal the segment count.
- A child that dies (its worker sends ``__mp_error__``) surfaces as a ``RuntimeError`` from
  the generator without hanging.
- A process-global ``DOWNLOAD_CANCELLED`` set mid-batch terminates the spawned children (which
  carry their own fresh flag and can't see it) and the teardown join reaps them promptly.

Children are spawned and re-import the module, so monkeypatched module attributes do NOT reach
them; these tests drive real child processes against a localhost server on 127.0.0.1 (reachable
from the children) and avoid any child-side patching. Segments are a few hundred bytes so the
real spawns stay fast.
"""

import importlib
import multiprocessing
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

dl = importlib.import_module("unshackle.core.downloaders.requests")

SEG_REPEAT = 32  # 8-digit index * 32 = 256-byte body, distinct per index


def seg_body(index: int) -> bytes:
    """Segment body that encodes its own index, so misfiled striding is detectable."""
    return (f"{index:08d}" * SEG_REPEAT).encode()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        delay = getattr(server, "delay", 0.0)
        if delay:
            time.sleep(delay)
        try:
            index = int(self.path.rsplit("/", 1)[1].split(".")[0])
        except ValueError:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = seg_body(index)
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output quiet
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # a cancelled child closes its socket mid-write
        pass


@pytest.fixture
def server():
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.delay = 0.0
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def clear_cancel():
    # spawned children carry their own flag, but the parent loop reads this process's flag;
    # keep it clean so cancel state never leaks between tests
    dl.DOWNLOAD_CANCELLED.clear()
    yield
    dl.DOWNLOAD_CANCELLED.clear()


def url(srv, path):
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def test_multiprocess_stride_downloads_all_segments_correctly(server, tmp_path):
    # >= MP_MIN_SEGMENTS with processes=2 engages the fan-out via the public entry point
    n = dl.MP_MIN_SEGMENTS
    urls = [{"url": url(server, f"/seg/{i}.bin")} for i in range(n)]

    total_advance = 0
    for event in dl.requests(urls, output_dir=tmp_path, filename="seg_{i:04}.bin", max_workers=4, processes=2):
        total_advance += event.get("advance", 0)

    files = sorted(tmp_path.glob("seg_*.bin"))
    assert len(files) == n
    for f in files:
        index = int(f.stem.split("_")[1])
        assert f.read_bytes() == seg_body(index), f"segment {index} holds another index's bytes (stride mapping bug)"
    # parent drops child advances and emits one advance=1 per file_downloaded
    assert total_advance == n


def test_multiprocess_child_crash_raises(server, tmp_path):
    # download_multiprocess has no MP_MIN_SEGMENTS gate, so drive it directly with 2 urls.
    # a dict url without "url" raises KeyError while requests() builds the save path (before
    # any retry loop), so the child sends __mp_error__ fast; the sibling downloads normally.
    good = {"url": url(server, "/seg/0.bin")}
    bad = {"nourl": True}
    with pytest.raises(RuntimeError):
        for _ in dl.download_multiprocess(
            urls=[good, bad],
            output_dir=tmp_path,
            filename="seg_{i:04}.bin",
            headers=None,
            cookies=None,
            proxy=None,
            max_workers=2,
            adaptive=False,
            spec={"kind": "none"},
            processes=2,
            debug_logger=None,
        ):
            pass


def test_multiprocess_cancel_terminates_children(server, tmp_path):
    server.delay = 0.5  # slow segments so cancel lands well before the batch could finish
    n = dl.MP_MIN_SEGMENTS
    urls = [{"url": url(server, f"/seg/{i}.bin")} for i in range(n)]

    baseline = len(multiprocessing.active_children())
    start = time.time()
    progressed = 0
    for event in dl.requests(urls, output_dir=tmp_path, filename="seg_{i:04}.bin", max_workers=4, processes=2):
        if event.get("advance") or event.get("written"):
            progressed += 1
            if progressed >= 2:
                dl.DOWNLOAD_CANCELLED.set()  # sibling-track cancel: parent must tear the children down
    elapsed = time.time() - start

    # cancel stopped the work: nowhere near all segments finished, and no hang
    assert len(list(tmp_path.glob("seg_*.bin"))) < n
    assert elapsed < 15
    # the finally-block join reaps the spawned children promptly
    deadline = time.time() + 5
    while time.time() < deadline and len(multiprocessing.active_children()) > baseline:
        time.sleep(0.05)
    assert len(multiprocessing.active_children()) <= baseline


def test_speed_limit_forces_single_process(server, tmp_path, monkeypatch):
    """A set speed limit must keep the batch in-process: spawned children re-import the
    module and would run with no TokenBucket, silently ignoring the configured cap."""

    def no_mp(**kwargs):
        raise AssertionError("multiprocess fan-out engaged despite a speed limit")

    monkeypatch.setattr(dl, "download_multiprocess", no_mp)
    n = dl.MP_MIN_SEGMENTS
    urls = [{"url": url(server, f"/seg/{i}.bin")} for i in range(n)]
    dl.set_speed_limit(1_000_000_000)  # far above the tiny batch, so the test is not slowed
    try:
        for _ in dl.requests(urls, output_dir=tmp_path, filename="seg_{i:04}.bin", max_workers=4, processes=2):
            pass
    finally:
        dl.set_speed_limit(None)

    files = sorted(tmp_path.glob("seg_*.bin"))
    assert len(files) == n
    for f in files:
        assert f.read_bytes() == seg_body(int(f.stem.split("_")[1]))
