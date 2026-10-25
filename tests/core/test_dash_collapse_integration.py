"""Integration coverage for the CONSUMING side of the DASH single-URL byte-range collapse.

The pure predicate ``DASH.collapsible_single_url`` is unit-tested in
``test_dash_collapse.py``; this file drives the real ``DASH.download_track`` code path that
acts on it. A minimal MPD is parsed with ``DASH.from_text().to_tracks()`` (so the track's
``data["dash"]`` is built by real parser code, not hand-faked) and downloaded against a
localhost server, asserting that:

- a SegmentList whose ranges collapse fetches the parent resource whole in one direct
  (Range-less) request and writes a byte-correct file at ``save_path``, leaving no
  segment dir or ``.!dev`` artifacts, and
- a SegmentBase track writes the parent byte for byte, when the server sends its total
  length and when it sends an unknown (``*``) one, and
- a track with mixed segment URLs still takes the per-segment download-and-merge path, and
- that same segmented path raises ``DownloadCancelled`` when a sibling track's failure has
  already set ``DOWNLOAD_CANCELLED``, instead of treating the segments the cancel left
  behind as a fault of its own.
"""

from __future__ import annotations

import shutil
import struct
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED, DownloadCancelled
from unshackle.core.manifests import DASH
from unshackle.core.tracks.track import DownloadContext

pytestmark = pytest.mark.skipif(shutil.which("ffprobe") is None, reason="requires ffprobe")

# distinctive parent resource for the collapse case; init is its [0, INIT_LEN) prefix
PARENT = bytes(i % 256 for i in range(10240))
INIT_LEN = 512
# last indexed byte for the clipped case; the parent carries 1024 bytes past it
CLIP_END = len(PARENT) - 1025

# separate resources for the non-collapse (mixed-URL) case
INIT2 = bytes(range(100))
PART0 = bytes([0xA0]) * 300
PART1 = bytes([0xB1]) * 200


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        rng = self.headers.get("Range")
        with server.lock:
            server.requests.append((self.path, rng))
        body = server.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if rng:
            first, _, last = rng.removeprefix("bytes=").partition("-")
            start, end = int(first), int(last) if last else len(body) - 1
            chunk = body[start : end + 1]
            self.send_response(206)
            total = "*" if server.unknown_total else len(body)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            body = chunk
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.lock = threading.Lock()
    srv.requests = []
    srv.routes = {}
    srv.unknown_total = False
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def clear_cancel():
    was_set = DOWNLOAD_CANCELLED.is_set()
    DOWNLOAD_CANCELLED.clear()
    yield
    if was_set:
        DOWNLOAD_CANCELLED.set()
    else:
        DOWNLOAD_CANCELLED.clear()


def base(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}/"


def mixed_mpd(srv) -> str:
    # Two SegmentURLs on distinct paths -> mixed URLs -> predicate False -> segmented path.
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">
  <BaseURL>{base(srv)}</BaseURL>
  <Period id="0">
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      <Representation id="v0" codecs="avc1.640028" bandwidth="1000000" width="1920" height="1080">
        <SegmentList timescale="1">
          <Initialization sourceURL="init.bin"/>
          <SegmentURL media="part0.bin"/>
          <SegmentURL media="part1.bin"/>
        </SegmentList>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""


def sidx_box(sizes: list[int]) -> bytes:
    # version 0 sidx: one MEDIA reference per size, each starting with a SAP
    refs = b"".join(struct.pack(">III", size, 1, 0x90000000) for size in sizes)
    body = struct.pack(">IIIIIHH", 0, 1, 1, 0, 0, 0, len(sizes)) + refs
    return struct.pack(">I", 8 + len(body)) + b"sidx" + body


def media_mpd(srv, segments: str) -> str:
    # one Representation on media.mp4, described by the given segment element
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">
  <BaseURL>{base(srv)}</BaseURL>
  <Period id="0">
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      <Representation id="v0" codecs="avc1.640028" bandwidth="1000000" width="1920" height="1080">
        <BaseURL>media.mp4</BaseURL>
{segments}
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""


def ranged_mpd(srv, ranges: list[tuple[int, int]], init: str = f'range="0-{INIT_LEN - 1}"') -> str:
    # SegmentList of byte ranges on one media.mp4, with the given Initialization attributes
    segment_urls = "\n".join(f'          <SegmentURL mediaRange="{s}-{e}"/>' for s, e in ranges)
    return media_mpd(
        srv,
        f"""        <SegmentList timescale="1">
          <Initialization {init}/>
{segment_urls}
        </SegmentList>""",
    )


def segment_base_mpd(srv) -> str:
    # the media range comes from the init response, not the manifest
    return media_mpd(
        srv,
        f"""        <SegmentBase>
          <Initialization range="0-{INIT_LEN - 1}"/>
        </SegmentBase>""",
    )


def make_track(mpd: str, srv):
    return DASH.from_text(mpd, url=f"{base(srv)}manifest.mpd").to_tracks(language="en").videos[0]


def make_ctx(tmp_path, name="out.mp4"):
    return DownloadContext(
        save_path=tmp_path / name,
        save_dir=tmp_path / "v0_segments",
        progress=partial(lambda **kw: None),
    )


def test_collapse_downloads_parent_whole_in_one_direct_request(server, tmp_path):
    server.routes["/media.mp4"] = PARENT
    track = make_track(ranged_mpd(server, [(INIT_LEN, 5119), (5120, len(PARENT) - 1)]), server)
    ctx = make_ctx(tmp_path, "collapsed.mp4")

    DASH.download_track(track, ctx)

    # the whole parent lands at save_path, byte-for-byte
    assert ctx.save_path.read_bytes() == PARENT
    assert track.path == ctx.save_path
    # exactly one Range-less GET (the direct whole-resource download); the only other hit is
    # the ranged init probe done while parsing SegmentList, never a per-segment slice fetch.
    no_range = [path for path, rng in server.requests if rng is None]
    assert no_range == ["/media.mp4"]
    # collapse skips the segment dir + merge entirely: no dir, no leftover control files
    assert not ctx.save_dir.exists()
    assert not list(tmp_path.glob("*.!dev"))
    assert not list(tmp_path.glob("**/*.!dev"))


def test_collapse_trims_parent_bytes_past_the_last_segment(server, tmp_path):
    server.routes["/media.mp4"] = PARENT
    # the ranges stop short of the parent's end, so the collapsed file must be trimmed
    track = make_track(ranged_mpd(server, [(INIT_LEN, 5119), (5120, CLIP_END)]), server)
    ctx = make_ctx(tmp_path, "clipped.mp4")

    DASH.download_track(track, ctx)

    # the parent's trailing 1024 bytes are not part of any segment, so they must not survive
    assert ctx.save_path.read_bytes() == PARENT[: CLIP_END + 1]


def test_collapse_keeps_the_sidx_between_init_and_first_segment(server, tmp_path):
    media = [bytes([0xC0]) * 3000, bytes([0xD1]) * 2000]
    head = PARENT[:INIT_LEN] + sidx_box([len(m) for m in media])
    parent = head + b"".join(media)
    server.routes["/media.mp4"] = parent
    ranges = [(len(head), len(head) + 2999), (len(head) + 3000, len(parent) - 1)]
    ctx = make_ctx(tmp_path, "sidx.mp4")

    DASH.download_track(make_track(ranged_mpd(server, ranges), server), ctx)

    assert ctx.save_path.read_bytes() == parent
    assert [path for path, rng in server.requests if rng is None] == ["/media.mp4"]


def test_segment_base_collapse_ends_at_the_last_byte(server, tmp_path):
    server.routes["/media.mp4"] = PARENT
    ctx = make_ctx(tmp_path, "segment_base.mp4")

    DASH.download_track(make_track(segment_base_mpd(server), server), ctx)

    # the media range ends at total - 1, so the truncate after the collapse adds no byte
    assert ctx.save_path.read_bytes() == PARENT


def test_segment_base_unknown_total_fetches_the_rest_open_ended(server, tmp_path):
    server.routes["/media.mp4"] = PARENT
    server.unknown_total = True
    ctx = make_ctx(tmp_path, "unknown_total.mp4")

    DASH.download_track(make_track(segment_base_mpd(server), server), ctx)

    assert ctx.save_path.read_bytes() == PARENT
    assert ("/media.mp4", f"bytes={INIT_LEN}-") in server.requests


def test_leading_gap_that_is_not_a_sidx_takes_segmented_path(server, tmp_path):
    # a filtered period cut from the same file leaves a gap of media, not an index
    gap = struct.pack(">I", 1000) + b"moof" + bytes(992)
    media = bytes([0xC0]) * 3000
    server.routes["/media.mp4"] = PARENT[:INIT_LEN] + gap + media
    start = INIT_LEN + len(gap)
    ctx = make_ctx(tmp_path, "gap.mp4")

    DASH.download_track(make_track(ranged_mpd(server, [(start, start + 2999)]), server), ctx)

    assert ctx.save_path.read_bytes() == PARENT[:INIT_LEN] + media
    assert not [path for path, rng in server.requests if rng is None]


def test_sidx_indexing_more_than_the_listed_segments_takes_segmented_path(server, tmp_path):
    # the file carries a post-roll after the listed segments, and the sidx indexes it too
    media, post_roll = bytes([0xC0]) * 3000, bytes([0xEE]) * 500
    head = PARENT[:INIT_LEN] + sidx_box([len(media), len(post_roll)])
    server.routes["/media.mp4"] = head + media + post_roll
    ctx = make_ctx(tmp_path, "post-roll.mp4")

    DASH.download_track(make_track(ranged_mpd(server, [(len(head), len(head) + 2999)]), server), ctx)

    assert ctx.save_path.read_bytes() == PARENT[:INIT_LEN] + media


def test_init_from_another_file_takes_segmented_path(server, tmp_path):
    # the media ranges start where an init of the same length would end, but the init is elsewhere
    init = bytes([0x11]) * INIT_LEN
    server.routes["/init.bin"] = init
    server.routes["/media.mp4"] = PARENT
    ranges = [(INIT_LEN, 5119), (5120, len(PARENT) - 1)]
    ctx = make_ctx(tmp_path, "init-elsewhere.mp4")

    DASH.download_track(make_track(ranged_mpd(server, ranges, init='sourceURL="init.bin"'), server), ctx)

    assert ctx.save_path.read_bytes() == init + PARENT[INIT_LEN:]
    assert not [path for path, rng in server.requests if rng is None and path == "/media.mp4"]


def test_non_collapse_takes_segmented_merge_path(server, tmp_path):
    server.routes["/init.bin"] = INIT2
    server.routes["/part0.bin"] = PART0
    server.routes["/part1.bin"] = PART1
    track = make_track(mixed_mpd(server), server)
    ctx = make_ctx(tmp_path, "merged.mp4")

    DASH.download_track(track, ctx)

    # merged output is init followed by both segments, in order
    assert ctx.save_path.read_bytes() == INIT2 + PART0 + PART1
    # both distinct segments were fetched -> genuinely the per-segment path, not a collapse
    fetched = {path for path, _ in server.requests}
    assert {"/part0.bin", "/part1.bin"} <= fetched
    # segment files are consumed and the segment dir is cleaned up after merge
    assert not list(ctx.save_dir.glob("*")) if ctx.save_dir.exists() else True


def test_cancelled_sibling_does_not_report_missing_segments(server, tmp_path):
    server.routes["/init.bin"] = INIT2
    server.routes["/part0.bin"] = PART0
    server.routes["/part1.bin"] = PART1
    track = make_track(mixed_mpd(server), server)
    ctx = make_ctx(tmp_path, "cancelled.mp4")

    DOWNLOAD_CANCELLED.set()
    with pytest.raises(DownloadCancelled):
        DASH.download_track(track, ctx)

    # the workers returned early, so there is nothing to merge; a FileNotFoundError about the
    # missing segments here would mask the sibling failure that set the flag
    assert track.path is None
    assert not ctx.save_path.exists()


def test_rolling_merge_appends_in_order_and_leaves_no_segments(server, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "merge_segments", True)
    server.routes["/init.bin"] = INIT2
    server.routes["/part0.bin"] = PART0
    server.routes["/part1.bin"] = PART1
    track = make_track(mixed_mpd(server), server)
    ctx = make_ctx(tmp_path, "rolling.mp4")

    DASH.download_track(track, ctx)

    assert ctx.save_path.read_bytes() == INIT2 + PART0 + PART1
    assert not ctx.save_dir.exists() or not list(ctx.save_dir.glob("*"))


def test_rolling_merge_cancel_leaves_no_partial_output(server, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "merge_segments", True)
    server.routes["/init.bin"] = INIT2
    server.routes["/part0.bin"] = PART0
    server.routes["/part1.bin"] = PART1
    track = make_track(mixed_mpd(server), server)
    ctx = make_ctx(tmp_path, "rolling-cancelled.mp4")

    DOWNLOAD_CANCELLED.set()
    with pytest.raises(DownloadCancelled):
        DASH.download_track(track, ctx)

    assert track.path is None
    assert not ctx.save_path.exists()
