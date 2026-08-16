"""Tail-boost success-path invariants.

Pins the tail-boost engage path in ``requests()``: when only a few segments remain
and workers would otherwise idle, each remaining segment is probed for range support
and split into intra-segment byte-range parts written into a pre-truncated ``.tp.!dev``
target, finalized by the last part worker via os.replace. The invariants:

- a boosted segment's final file is byte-identical to the served content, every
  segment reports exactly one advance=1 (part-mode byte advances are swallowed), and
  a clean run leaves no ``.!dev`` / ``.tp.!dev`` strays;
- finalize is last-part-wins: on success save_path exists and the ``.tp.!dev`` target
  is gone;
- a permanently failing part fails the batch and the boosted segment is never
  os.replace'd into place (no corrupt finalized segment).

Determinism: with ``max_workers`` < segment count <= 2*max_workers and adaptive=True,
the leading ``max_workers`` segments submit upfront and the trailing few stay in
``remaining`` (the tail top-up only releases boost-declined indices). The controller
starts at the cap and never grows, so as soon as two leaders finish the spare workers
outnumber the tail and ``tail_boost_engages`` fires. Module timing/size constants are
lowered via monkeypatch; the whole batch runs in-process (threads), so the patched
values are seen by the workers.
"""

import importlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

dl = importlib.import_module("unshackle.core.downloaders.requests")

BODY = bytes((i * 7 + 11) % 256 for i in range(16384))  # 16 KiB, distinctive, range-sliceable

# lowered so a 16 KiB segment clears the min and splits into several 4 KiB parts
BOOST_MIN = 4096
BOOST_PART = 4096

MAX_WORKERS = 6
SEG_COUNT = 8  # > MAX_WORKERS and <= 2*MAX_WORKERS: leaders 0-5 submit, tail 6,7 boost


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        rng = self.headers.get("Range")
        with server.lock:
            server.requests.append((self.path, rng))
        if rng and rng.startswith("bytes="):
            start_s, _, end_s = rng[len("bytes=") :].partition("-")
            start = int(start_s)
            end = int(end_s) if end_s else len(BODY) - 1
            if server.break_offset_ranges and start > 0:
                # misbehave for every non-leading window: send the whole body as 200 so
                # part-mode's 206 check fails permanently for that part
                self.send_response(200)
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY)
                return
            body = BODY[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(BODY)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.lock = threading.Lock()
    srv.requests = []
    srv.break_offset_ranges = False
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clean_cancel():
    # a failing batch sets the process-global cancel; keep tests independent
    dl.DOWNLOAD_CANCELLED.clear()
    yield
    dl.DOWNLOAD_CANCELLED.clear()


@pytest.fixture(autouse=True)
def _boost_constants(monkeypatch):
    monkeypatch.setattr(dl, "TAIL_BOOST_MIN_SEGMENT_SIZE", BOOST_MIN)
    monkeypatch.setattr(dl, "TAIL_BOOST_PART_SIZE", BOOST_PART)


def _url(srv, path):
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def _urls(srv):
    return [{"url": _url(srv, f"/seg{i}.bin")} for i in range(SEG_COUNT)]


def _run(srv, tmp_path, urls):
    advances = 0
    for ev in dl.requests(urls, output_dir=tmp_path, filename="seg_{i:04}.bin", max_workers=MAX_WORKERS, adaptive=True):
        a = ev.get("advance")
        if a:
            advances += a
    return sorted(tmp_path.glob("seg_*.bin")), advances


def _ranged_counts(srv):
    with srv.lock:
        reqs = list(srv.requests)
    counts: dict[str, int] = {}
    for path, rng in reqs:
        if rng:
            counts[path] = counts.get(path, 0) + 1
    return counts


def _boost_engaged(srv):
    # a boosted path sees a 0-0 probe plus >=2 part windows; a normal segment sees no Range
    return any(n > 1 for n in _ranged_counts(srv).values())


def test_tail_boost_split_produces_byte_identical_segment(server, tmp_path):
    files, advances = _run(server, tmp_path, _urls(server))

    assert _boost_engaged(server), "tail boost never engaged; test would silently cover the normal path"
    assert len(files) == SEG_COUNT
    assert all(f.read_bytes() == BODY for f in files)
    # part-mode byte advances are swallowed; each segment reports exactly one advance=1
    assert advances == SEG_COUNT
    # a clean run renames every target into place, so no partial/part strays survive
    assert not list(tmp_path.glob("*.!dev"))
    assert not list(tmp_path.glob("*.tp.!dev"))


def test_tail_boost_finalize_last_part_wins(server, tmp_path):
    files, _ = _run(server, tmp_path, _urls(server))

    assert _boost_engaged(server), "tail boost never engaged; finalize path untested"
    assert len(files) == SEG_COUNT
    # last part wins: the pre-truncated .tp.!dev is os.replace'd into save_path, leaving no target
    assert not list(tmp_path.glob("*.tp.!dev"))
    for path in _ranged_counts(server):
        i = int(path.removeprefix("/seg").removesuffix(".bin"))
        save_path = tmp_path / f"seg_{i:04}.bin"
        assert save_path.exists()
        assert not save_path.with_name(f"{save_path.name}.tp.!dev").exists()


def test_tail_boost_part_failure_fails_batch_but_keeps_siblings_files_clean(server, tmp_path, monkeypatch):
    server.break_offset_ranges = True  # every non-leading part window 200s -> permanent 206 failure
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)

    with pytest.raises(Exception):
        _run(server, tmp_path, _urls(server))

    # the boosted tail segments (6, 7) hit the failing windows: no part run reaches parts_left==0
    # cleanly, so neither is finalized. sibling leaders that did finalize must be uncorrupted.
    for i in (6, 7):
        assert not (tmp_path / f"seg_{i:04}.bin").exists()
    for f in tmp_path.glob("seg_*.bin"):
        assert f.read_bytes() == BODY
