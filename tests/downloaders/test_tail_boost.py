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
  os.replace'd into place (no corrupt finalized segment);
- a part that fails for a reason a retry could fix puts its segment back on the normal
  single-worker path, without aborting the sibling segments or the track.

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
            if server.deny_offset_ranges and start > 0:
                # every non-leading window is refused for good (expired token, geo block)
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if server.break_offset_ranges and start > 0:
                # every non-leading window answers 200 with the whole body: part-mode's 206
                # check fails, but 200 is not permanent, so the segment goes back on the
                # normal single-worker path
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
    srv.deny_offset_ranges = False
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def clean_cancel():
    # a failing batch sets the process-global cancel; keep tests independent
    dl.DOWNLOAD_CANCELLED.clear()
    yield
    dl.DOWNLOAD_CANCELLED.clear()


@pytest.fixture(autouse=True)
def boost_constants(monkeypatch):
    monkeypatch.setattr(dl, "TAIL_BOOST_MIN_SEGMENT_SIZE", BOOST_MIN)
    monkeypatch.setattr(dl, "TAIL_BOOST_PART_SIZE", BOOST_PART)


def url(srv, path):
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def seg_urls(srv):
    return [{"url": url(srv, f"/seg{i}.bin")} for i in range(SEG_COUNT)]


def run(srv, tmp_path, urls):
    advances = 0
    for ev in dl.requests(urls, output_dir=tmp_path, filename="seg_{i:04}.bin", max_workers=MAX_WORKERS, adaptive=True):
        a = ev.get("advance")
        if a:
            advances += a
    return sorted(tmp_path.glob("seg_*.bin")), advances


def ranged_counts(srv):
    with srv.lock:
        reqs = list(srv.requests)
    counts: dict[str, int] = {}
    for path, rng in reqs:
        if rng:
            counts[path] = counts.get(path, 0) + 1
    return counts


def boost_engaged(srv):
    # a boosted path sees a 0-0 probe plus >=2 part windows; a normal segment sees no Range
    return any(n > 1 for n in ranged_counts(srv).values())


def test_tail_boost_split_produces_byte_identical_segment(server, tmp_path):
    files, advances = run(server, tmp_path, seg_urls(server))

    assert boost_engaged(server), "tail boost never engaged; test would silently cover the normal path"
    assert len(files) == SEG_COUNT
    assert all(f.read_bytes() == BODY for f in files)
    # part-mode byte advances are swallowed; each segment reports exactly one advance=1
    assert advances == SEG_COUNT
    # a clean run renames every target into place, so no partial/part strays survive
    assert not list(tmp_path.glob("*.!dev"))
    assert not list(tmp_path.glob("*.tp.!dev"))


def test_tail_boost_finalize_last_part_wins(server, tmp_path):
    files, _ = run(server, tmp_path, seg_urls(server))

    assert boost_engaged(server), "tail boost never engaged; finalize path untested"
    assert len(files) == SEG_COUNT
    # last part wins: the pre-truncated .tp.!dev is os.replace'd into save_path, leaving no target
    assert not list(tmp_path.glob("*.tp.!dev"))
    for path in ranged_counts(server):
        i = int(path.removeprefix("/seg").removesuffix(".bin"))
        save_path = tmp_path / f"seg_{i:04}.bin"
        assert save_path.exists()
        assert not save_path.with_name(f"{save_path.name}.tp.!dev").exists()


def test_tail_boost_part_failure_falls_back_to_normal_path(server, tmp_path, monkeypatch):
    server.break_offset_ranges = True  # every part window past the first fails, but not for good
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)

    files, advances = run(server, tmp_path, seg_urls(server))

    assert boost_engaged(server), "tail boost never engaged; the fallback path is untested"
    # a transient part failure returns the segment to the normal path instead of killing the track
    assert len(files) == SEG_COUNT
    assert all(f.read_bytes() == BODY for f in files)
    # one advance per segment: a requeued segment must not be counted twice
    assert advances == SEG_COUNT
    assert not dl.DOWNLOAD_CANCELLED.is_set()
    assert not list(tmp_path.glob("*.!dev"))
    assert not list(tmp_path.glob("*.tp.!dev"))


def test_tail_boost_failure_does_not_abort_sibling_segments(server, tmp_path, monkeypatch):
    server.break_offset_ranges = True
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)

    files, _ = run(server, tmp_path, seg_urls(server))

    assert boost_engaged(server), "tail boost never engaged; the fallback path is untested"
    # the parts of one segment share an abort of their own: setting the batch-wide one would
    # strand every other in-flight worker and end the batch with segments missing
    assert len(files) == SEG_COUNT
    with server.lock:
        reqs = list(server.requests)
    boosted_paths = {path for path, rng in reqs if rng and rng != "bytes=0-0"}
    assert boosted_paths
    # each boosted segment went back on the normal path: a plain unranged GET for it
    plain = {path for path, rng in reqs if rng is None}
    assert boosted_paths <= plain


def test_tail_boost_permanent_part_failure_still_fails_the_batch(server, tmp_path, monkeypatch):
    server.deny_offset_ranges = True  # every part window past the first is a 403
    monkeypatch.setattr(dl, "RETRY_WAIT", 0.01)

    with pytest.raises(Exception):
        run(server, tmp_path, seg_urls(server))

    assert boost_engaged(server), "tail boost never engaged; the permanent-failure rule is untested"
    # a 4xx will not get better on the normal path either, so the boosted tail segments
    # (6, 7) are never finalized and the batch fails instead of paying a second full attempt
    for i in (6, 7):
        assert not (tmp_path / f"seg_{i:04}.bin").exists()
    for f in tmp_path.glob("seg_*.bin"):
        assert f.read_bytes() == BODY
