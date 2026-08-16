"""Integration coverage for the CONSUMING side of the DASH single-URL byte-range collapse.

The pure predicate ``DASH.collapsible_single_url`` is unit-tested in
``test_dash_collapse.py``; this file drives the real ``DASH.download_track`` code path that
acts on it. A minimal MPD is parsed with ``DASH.from_text().to_tracks()`` (so the track's
``data["dash"]`` is built by real parser code, not hand-faked) and downloaded against a
localhost server, asserting that:

- a SegmentList whose ranges collapse fetches the parent resource whole in one direct
  (Range-less) request and writes a byte-correct file at ``save_path``, leaving no
  segment dir or ``.!dev`` artifacts, and
- a track with mixed segment URLs still takes the per-segment download-and-merge path.
"""

from __future__ import annotations

import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.manifests import DASH
from unshackle.core.tracks.track import DownloadContext

# distinctive parent resource for the collapse case; init is its [0, INIT_LEN) prefix
PARENT = bytes(i % 256 for i in range(10240))
INIT_LEN = 512

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
            start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
            chunk = body[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
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


def collapse_mpd(srv) -> str:
    # SegmentList on one media.mp4: init is bytes [0, INIT_LEN), media ranges cover the rest
    # contiguously -> collapsible_single_url returns the URL and download_track collapses.
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">
  <BaseURL>{base(srv)}</BaseURL>
  <Period id="0">
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      <Representation id="v0" codecs="avc1.640028" bandwidth="1000000" width="1920" height="1080">
        <BaseURL>media.mp4</BaseURL>
        <SegmentList timescale="1">
          <Initialization range="0-{INIT_LEN - 1}"/>
          <SegmentURL mediaRange="{INIT_LEN}-5119"/>
          <SegmentURL mediaRange="5120-{len(PARENT) - 1}"/>
        </SegmentList>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""


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
    track = make_track(collapse_mpd(server), server)
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
