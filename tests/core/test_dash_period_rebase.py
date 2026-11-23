import re
import struct
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import requests
from lxml import etree

from unshackle.core.config import config
from unshackle.core.manifests import DASH, HLS
from unshackle.core.manifests.dash import PeriodRebase, mdhd_timescale, tfdt_fields
from unshackle.core.manifests.ism_init import box, full_box
from unshackle.core.manifests.snapshot import fragments, unpack
from unshackle.core.tracks import Tracks
from unshackle.core.tracks.track import DownloadContext, Track


def fragment(*times: int, version: int = 1) -> bytes:
    """A moof with one traf per decode time, then an empty mdat."""
    trafs = b"".join(
        box(b"traf", full_box(b"tfdt", version, 0, struct.pack(">Q" if version == 1 else ">I", t))) for t in times
    )
    return box(b"moof", trafs) + box(b"mdat", b"")


def init_segment(timescale: int, version: int = 0) -> bytes:
    times = struct.pack(">QQ", 0, 0) if version == 1 else struct.pack(">II", 0, 0)
    mdhd = full_box(b"mdhd", version, 0, times + struct.pack(">I", timescale) + b"\0" * 12)
    return box(b"ftyp", b"iso6") + box(b"moov", box(b"trak", box(b"mdia", mdhd)))


def tfdts(data: bytes) -> list[int]:
    return [value for _, _, value in tfdt_fields(data)]


def merge(
    tmp_path: Path, segments: list[bytes], periods: dict[int, tuple[float, float, float]], timescale: int = 1000
) -> bytes:
    rebase, dst = PeriodRebase(periods, timescale), BytesIO()
    for i, data in enumerate(segments):
        path = tmp_path / f"{i:02}.mp4"
        path.write_bytes(data)
        rebase.append(dst, path)
    return dst.getvalue()


def test_restarting_periods_follow_each_other(tmp_path: Path) -> None:
    # three periods of 10 s, 20 s and 5 s, each packaged with its own timeline from 0
    segments = [fragment(0), fragment(5000), fragment(0), fragment(10000), fragment(0, version=0)]
    merged = merge(tmp_path, segments, {0: (0, 10, 0), 2: (10, 20, 0), 4: (30, 5, 0)})
    assert tfdts(merged) == [0, 5000, 10000, 20000, 30000]


def test_a_period_the_track_lacks_keeps_its_time(tmp_path: Path) -> None:
    # the track has no segments in the 20 s period between the two it holds
    merged = merge(tmp_path, [fragment(0), fragment(0)], {0: (0, 10, 0), 1: (30, 5, 0)})
    assert tfdts(merged) == [0, 30000]


def test_presentation_time_offset_marks_the_period_start(tmp_path: Path) -> None:
    # the second period's first segment starts 500 ms before its presentationTimeOffset
    merged = merge(tmp_path, [fragment(0), fragment(1500)], {0: (0, 10, 0), 1: (10, 10, 2)})
    assert tfdts(merged) == [0, 9500]


def test_restart_below_the_previous_end_is_caught(tmp_path: Path) -> None:
    merged = merge(tmp_path, [fragment(100000), fragment(100500)], {0: (0, 10, 0), 1: (10, 10, 0)})
    assert tfdts(merged) == [100000, 110000]


def test_continuous_timeline_is_unchanged(tmp_path: Path) -> None:
    # absolute timestamps with a filtered ad between the periods keep their gap
    segments = [fragment(0), fragment(5000), fragment(40000), fragment(45000)]
    assert merge(tmp_path, segments, {0: (0, 10, 0), 2: (10, 10, 0)}) == b"".join(segments)


def test_every_traf_is_shifted(tmp_path: Path) -> None:
    merged = merge(tmp_path, [fragment(0), fragment(0, 2000)], {0: (0, 4, 0), 1: (4, 4, 0)})
    assert tfdts(merged) == [0, 4000, 6000]


def test_32_bit_overflow_raises(tmp_path: Path) -> None:
    segments = [fragment(0), fragment(0, version=0)]
    with pytest.raises(ValueError, match="32-bit"):
        merge(tmp_path, segments, {0: (0, 2.0**32 / 1000, 0), 1: (2.0**32 / 1000, 1, 0)})


def test_short_tfdt_is_skipped() -> None:
    truncated = box(b"moof", box(b"traf", box(b"tfdt", b"")))
    assert tfdt_fields(truncated) == []
    assert tfdt_fields(truncated + b"tfdt") == []


@pytest.mark.parametrize("version", [0, 1])
def test_mdhd_timescale(version: int) -> None:
    assert mdhd_timescale(init_segment(15360, version)) == 15360
    assert mdhd_timescale(box(b"ftyp", b"iso6")) is None


@pytest.mark.parametrize(
    ("periods", "expected"),
    [
        ('<Period duration="PT4S"/><Period start="PT4S"/><Period start="PT10S"/>', [4.0, 6.0, 20.0]),
        ('<Period/><Period start="PT10S"/>', [10.0, 20.0]),
        ('<Period duration="PT10S"/><Period/>', [10.0, 20.0]),
        ('<Period start="PT5S"/><Period start="PT2S"/>', [None, 28.0]),
        ("<Period/><Period/>", [None, None]),
    ],
)
def test_period_durations(periods: str, expected: list) -> None:
    mpd = etree.fromstring(f'<MPD mediaPresentationDuration="PT30S">{periods}</MPD>')
    assert DASH.period_durations(mpd) == expected


def test_presentation_time_offset_reads_the_merged_template() -> None:
    aset = etree.fromstring(
        '<AdaptationSet><SegmentTemplate timescale="1000" presentationTimeOffset="2500"/></AdaptationSet>'
    )
    assert DASH.presentation_time_offset(aset, etree.SubElement(aset, "Representation")) == 2.5
    assert DASH.presentation_time_offset(etree.Element("AdaptationSet"), etree.Element("Representation")) == 0.0


def test_hls_discontinuity_starts() -> None:
    segments = [SimpleNamespace(discontinuity=d, duration=t) for d, t in ((True, 4), (False, 4), (True, 3), (False, 1))]
    assert HLS.discontinuity_starts(segments) == [0.0, 8.0, 12.0]


def test_remote_fragment_writes_each_period_duration() -> None:
    mpd = DASH.from_text(
        """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT30S">
  <Period id="a" start="PT0S"><AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
    <Representation id="v" codecs="avc1.640028" bandwidth="1" width="1" height="1"><BaseURL>a.mp4</BaseURL></Representation>
  </AdaptationSet></Period>
  <Period id="ad" start="PT10S"><AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
    <Representation id="v" codecs="avc1.640028" bandwidth="1" width="1" height="1"><BaseURL>ad.mp4</BaseURL></Representation>
  </AdaptationSet></Period>
  <Period id="b" start="PT20S"><AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
    <Representation id="v" codecs="avc1.640028" bandwidth="1" width="1" height="1"><BaseURL>b.mp4</BaseURL></Representation>
  </AdaptationSet></Period>
</MPD>""",
        "https://example.test/manifest.mpd",
    )
    tracks = mpd.to_tracks(language="en", period_filter=lambda period: period.get("id") == "ad")
    own = Tracks()
    own.add(tracks.videos[0])
    # a fragment is built for a track whose AdaptationSet is not in its manifest tree
    own.videos[0].data["dash"]["adaptation_set"] = etree.fromstring(
        etree.tostring(own.videos[0].data["dash"]["adaptation_set"])
    )
    [entry] = fragments(own, 1, {})
    periods = unpack(entry["data"], [1 << 20]).findall("Period")
    assert [p.get("duration") for p in periods] == ["PT10S", "PT10S"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = self.server.routes.get(self.path.split("?")[0])  # type: ignore[attr-defined]
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def server() -> Iterator[ThreadingHTTPServer]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.routes = {}  # type: ignore[attr-defined]
    for p in (0, 1):
        srv.routes[f"/p{p}/init.mp4"] = init_segment(1000)  # type: ignore[attr-defined]
        for n, t in ((1, 0), (2, 1000)):
            srv.routes[f"/p{p}/{n}.m4s"] = fragment(t)  # type: ignore[attr-defined]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


def restarting_mpd(port: int) -> str:
    periods = "".join(
        f"""<Period id="{p}" duration="PT2S"><AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
  <Representation id="v0" codecs="avc1.640028" bandwidth="1000000" width="1920" height="1080">
    <SegmentTemplate timescale="1000" startNumber="1" initialization="p{p}/init.mp4" media="p{p}/$Number$.m4s">
      <SegmentTimeline><S t="0" d="1000" r="1"/></SegmentTimeline>
    </SegmentTemplate>
  </Representation></AdaptationSet></Period>"""
        for p in (0, 1)
    )
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
  <BaseURL>http://127.0.0.1:{port}/</BaseURL>{periods}</MPD>"""


@pytest.mark.parametrize("rolling", [True, False], ids=["rolling-merge", "post-merge"])
def test_download_track_rebases_restarting_periods(
    server: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rolling: bool
) -> None:
    monkeypatch.setattr(Track, "get_key_id", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(config, "merge_segments", rolling)
    track = DASH.from_text(restarting_mpd(server.server_address[1]), "http://127.0.0.1/manifest.mpd").to_tracks(
        language="en"
    )
    video = track.videos[0]
    ctx = DownloadContext(
        save_path=tmp_path / "out.mp4", save_dir=tmp_path / "v0_segments", progress=partial(lambda **kw: None)
    )

    DASH.download_track(video, ctx)

    assert tfdts((tmp_path / "out.mp4").read_bytes()) == [0, 1000, 2000, 3000]


def test_a_track_missing_the_first_period_is_placed_at_its_start(tmp_path: Path) -> None:
    merged = merge(tmp_path, [fragment(0), fragment(5000), fragment(0)], {0: (10, 10, 0), 2: (20, 5, 0)})
    assert tfdts(merged) == [10000, 15000, 20000]


def test_a_restart_after_a_continuous_gap_follows_the_previous_period(tmp_path: Path) -> None:
    segments = [fragment(0), fragment(5000), fragment(40000), fragment(45000), fragment(0)]
    merged = merge(tmp_path, segments, {0: (0, 10, 0), 2: (10, 10, 0), 4: (20, 5, 0)})
    assert tfdts(merged) == [0, 5000, 40000, 45000, 50000]


def test_a_period_back_on_the_first_timeline_leaves_the_restart_shift(tmp_path: Path) -> None:
    # an unfiltered 30 s ad restarts at 0 between two parts of one continuous timeline
    merged = merge(
        tmp_path, [fragment(0), fragment(0), fragment(600000)], {0: (0, 600, 0), 1: (600, 30, 0), 2: (630, 600, 600)}
    )
    assert tfdts(merged) == [0, 600000, 630000]


def test_mdhd_too_short_has_no_timescale() -> None:
    assert mdhd_timescale(box(b"moov", box(b"mdhd", b""))) is None
    assert mdhd_timescale(box(b"moov", box(b"mdhd", b"\0" * 4)) + box(b"free", b"\0" * 16)) is None


def test_number_template_counts_segments_to_the_next_period_start() -> None:
    mpd = DASH.from_text(
        """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT16S">
  <Period id="a" start="PT0S"/>
  <Period id="b" start="PT10S"><AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
    <Representation id="v" codecs="avc1.640028" bandwidth="1" width="1" height="1">
      <SegmentTemplate timescale="1000" duration="2000" startNumber="1" media="$Number$.m4s"/>
    </Representation></AdaptationSet></Period>
</MPD>""",
        "https://example.test/manifest.mpd",
    )
    period = mpd.manifest.findall("Period")[1]
    aset = period.find("AdaptationSet")
    _, segments, *_ = DASH.get_period_segments(
        period,
        aset,
        aset.find("Representation"),
        mpd.manifest,
        Track(url="https://example.test/", language="en"),
        "https://example.test/manifest.mpd",
        requests.Session(),
        probe_kid=False,
    )
    assert len(segments) == 3


def webvtt_mpd(port: int) -> str:
    periods = "".join(
        f"""<Period id="{p}" duration="PT2S"><AdaptationSet contentType="text" mimeType="text/vtt" lang="en">
  <Representation id="s0" bandwidth="1">
    <SegmentTemplate timescale="1000" startNumber="1" media="p{p}/$Number$.vtt">
      <SegmentTimeline><S t="0" d="1000" r="1"/></SegmentTimeline>
    </SegmentTemplate>
  </Representation></AdaptationSet></Period>"""
        for p in (0, 1)
    )
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
  <BaseURL>http://127.0.0.1:{port}/</BaseURL>{periods}</MPD>"""


def test_download_track_rebases_webvtt_periods(server: ThreadingHTTPServer, tmp_path: Path) -> None:
    for p in (0, 1):
        for n, cue in ((1, "00:00:00.100"), (2, "00:00:01.100")):
            server.routes[f"/p{p}/{n}.vtt"] = f"WEBVTT\n\n{cue} --> {cue[:-3]}900\nline\n\n".encode()  # type: ignore[attr-defined]
    track = (
        DASH.from_text(webvtt_mpd(server.server_address[1]), "http://127.0.0.1/manifest.mpd")
        .to_tracks(language="en")
        .subtitles[0]
    )
    ctx = DownloadContext(
        save_path=tmp_path / "out.vtt", save_dir=tmp_path / "s0_segments", progress=partial(lambda **kw: None)
    )

    DASH.download_track(track, ctx)

    assert re.findall(r"(\S+) -->", (tmp_path / "out.vtt").read_text()) == [
        "00:00:00.100",
        "00:00:01.100",
        "00:00:02.100",
        "00:00:03.100",
    ]
