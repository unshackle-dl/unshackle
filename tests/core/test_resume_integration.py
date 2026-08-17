"""Integration coverage for cross-run segment resume on the segmented DASH path.

Drives the real ``DASH.download_track`` twice against a localhost server with
``continue_downloads`` on: the first run dies on a missing segment (after completing the
other one), the second run finds the sidecar digest unchanged and must reuse the completed
segment file instead of re-fetching it. A third scenario changes the segmentation and
asserts the stale directory is wiped rather than mixed into the new download.
"""

from __future__ import annotations

import importlib
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.manifests import DASH
from unshackle.core.tracks import resume
from unshackle.core.tracks.track import DownloadContext

INIT = bytes(range(100))
PART0 = bytes([0xA0]) * 300
PART1 = bytes([0xB1]) * 200


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        with server.lock:
            server.requests.append(self.path)
        body = server.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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
def resume_enabled(monkeypatch):
    monkeypatch.setattr(config, "continue_downloads", True)
    # fail fast on the deliberately-missing segment instead of the full retry ladder
    # (import via importlib: the downloaders package exports a same-named function)
    monkeypatch.setattr(importlib.import_module("unshackle.core.downloaders.requests"), "MAX_ATTEMPTS", 1)
    DOWNLOAD_CANCELLED.clear()
    yield
    DOWNLOAD_CANCELLED.clear()


def base(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}/"


def mpd(srv, part1: str = "part1.bin") -> str:
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">
  <BaseURL>{base(srv)}</BaseURL>
  <Period id="0">
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      <Representation id="v0" codecs="avc1.640028" bandwidth="1000000" width="1920" height="1080">
        <SegmentList timescale="1">
          <Initialization sourceURL="init.bin"/>
          <SegmentURL media="part0.bin"/>
          <SegmentURL media="{part1}"/>
        </SegmentList>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""


def make_track(text: str, srv):
    return DASH.from_text(text, url=f"{base(srv)}manifest.mpd").to_tracks(language="en").videos[0]


def make_ctx(tmp_path):
    return DownloadContext(
        save_path=tmp_path / "out.mp4",
        save_dir=tmp_path / "v0_segments",
        progress=partial(lambda **kw: None),
        # sequential: part0 must be complete before the deliberate part1 failure aborts run 1
        max_workers=1,
    )


def test_second_run_reuses_completed_segments(server, tmp_path):
    server.routes["/init.bin"] = INIT
    server.routes["/part0.bin"] = PART0
    # part1.bin is missing -> the first run completes part0 then dies on part1

    ctx = make_ctx(tmp_path)
    with pytest.raises(Exception):
        DASH.download_track(make_track(mpd(server), server), ctx)

    assert (ctx.save_dir / "0.mp4").read_bytes() == PART0
    assert resume.sidecar_path(ctx.save_dir).exists()
    part0_fetches_run1 = server.requests.count("/part0.bin")

    DOWNLOAD_CANCELLED.clear()
    server.routes["/part1.bin"] = PART1
    DASH.download_track(make_track(mpd(server), server), ctx)

    assert ctx.save_path.read_bytes() == INIT + PART0 + PART1
    assert server.requests.count("/part0.bin") == part0_fetches_run1
    assert not resume.sidecar_path(ctx.save_dir).exists()
    assert not ctx.save_dir.exists()


def test_changed_segmentation_wipes_stale_segments(server, tmp_path):
    server.routes["/init.bin"] = INIT
    server.routes["/part0.bin"] = PART0

    ctx = make_ctx(tmp_path)
    with pytest.raises(Exception):
        DASH.download_track(make_track(mpd(server), server), ctx)
    assert (ctx.save_dir / "0.mp4").exists()

    DOWNLOAD_CANCELLED.clear()
    # different final segment path -> different digest -> stale part0 must be re-fetched
    server.routes["/part2.bin"] = PART1
    part0_fetches_run1 = server.requests.count("/part0.bin")
    DASH.download_track(make_track(mpd(server, part1="part2.bin"), server), ctx)

    assert ctx.save_path.read_bytes() == INIT + PART0 + PART1
    assert server.requests.count("/part0.bin") == part0_fetches_run1 + 1
