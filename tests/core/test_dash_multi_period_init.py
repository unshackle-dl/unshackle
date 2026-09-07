"""A multi-period DASH manifest must fetch and probe the init segment of the first content
period only.

``DASH.download_track`` keeps the init data and the KID of period 0 and discards them for
every later period, so a request and an FFprobe process per extra period gain nothing. The
``SegmentBase`` form is the exception: its ``media_range`` is built from ``len(init_data)``,
so the request stays for every period and only the probe is dropped.
"""

from __future__ import annotations

import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from unshackle.core.constants import DOWNLOAD_LICENCE_ONLY
from unshackle.core.manifests import DASH
from unshackle.core.tracks.track import DownloadContext, Track
from unshackle.core.utils.xml import load_xml

MEDIA = bytes(i % 256 for i in range(4096))
INIT_LEN = 100


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
    srv.routes = {f"/p{i}/init.bin": MEDIA[:INIT_LEN] for i in (0, 1)}
    srv.routes.update({f"/p{i}/1.bin": MEDIA for i in (0, 1)})
    srv.routes.update({f"/p{i}.mp4": MEDIA for i in (0, 1)})
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def probes(monkeypatch):
    """Count KID probes without running FFprobe."""
    calls = []

    def counted(self, init_data=None, *args, **kwargs):
        calls.append(init_data)
        return None

    monkeypatch.setattr(Track, "get_key_id", counted)
    return calls


@pytest.fixture
def licence_only():
    """Return from download_track once the periods are parsed, before any segment download."""
    was_set = DOWNLOAD_LICENCE_ONLY.is_set()
    DOWNLOAD_LICENCE_ONLY.set()
    yield
    if not was_set:
        DOWNLOAD_LICENCE_ONLY.clear()


def base(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}/"


def template_mpd(srv) -> str:
    periods = "\n".join(
        f"""  <Period id="{i}" duration="PT1S">
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      <Representation id="v0" codecs="avc1.640028" bandwidth="1000000" width="1920" height="1080">
        <SegmentTemplate timescale="1" duration="1" startNumber="1"
                         initialization="p{i}/init.bin" media="p{i}/$Number$.bin"/>
      </Representation>
    </AdaptationSet>
  </Period>"""
        for i in (0, 1)
    )
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2S">
  <BaseURL>{base(srv)}</BaseURL>
{periods}
</MPD>"""


def base_mpd(srv) -> str:
    periods = "\n".join(
        f"""  <Period id="{i}" duration="PT1S">
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      <Representation id="v0" codecs="avc1.640028" bandwidth="1000000" width="1920" height="1080">
        <BaseURL>p{i}.mp4</BaseURL>
        <SegmentBase>
          <Initialization range="0-{INIT_LEN - 1}"/>
        </SegmentBase>
      </Representation>
    </AdaptationSet>
  </Period>"""
        for i in (0, 1)
    )
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2S">
  <BaseURL>{base(srv)}</BaseURL>
{periods}
</MPD>"""


def make_track(mpd: str, srv):
    return DASH.from_text(mpd, url=f"{base(srv)}manifest.mpd").to_tracks(language="en").videos[0]


def make_ctx(tmp_path):
    return DownloadContext(
        save_path=tmp_path / "out.mp4",
        save_dir=tmp_path / "v0_segments",
        progress=partial(lambda **kw: None),
    )


def test_segment_template_init_is_fetched_once_for_two_periods(server, probes, licence_only, tmp_path):
    track = make_track(template_mpd(server), server)

    DASH.download_track(track, make_ctx(tmp_path))

    # both periods contributed a segment, so period 1 was parsed, not skipped
    assert len(track.data["dash"]["segment_durations"]) == 2
    assert [path for path, _ in server.requests] == ["/p0/init.bin"]
    assert len(probes) == 1


def test_segment_base_keeps_each_request_but_probes_once(server, probes, licence_only, tmp_path):
    track = make_track(base_mpd(server), server)

    DASH.download_track(track, make_ctx(tmp_path))

    # media_range needs the init bytes of every period, so every period keeps its request
    assert [path for path, _ in server.requests] == ["/p0.mp4", "/p1.mp4"]
    assert len(probes) == 1


def test_segment_base_range_survives_a_skipped_probe(server, probes):
    tree = load_xml(base_mpd(server).encode())
    period = tree.findall("Period")[1]
    aset = period.find("AdaptationSet")
    rep = aset.find("Representation")

    _, segments, _, _, track_kid = DASH.get_period_segments(
        period,
        aset,
        rep,
        tree,
        Track(url=base(server), language="en"),
        f"{base(server)}manifest.mpd",
        requests.Session(),
        probe_kid=False,
    )

    assert segments == [(f"{base(server)}p1.mp4", f"{INIT_LEN}-{len(MEDIA)}")]
    assert track_kid is None
    assert not probes
