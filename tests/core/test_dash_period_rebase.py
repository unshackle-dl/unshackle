import re
import struct
import threading
from collections.abc import Iterator
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from lxml import etree

from unshackle.core.config import config
from unshackle.core.manifests import DASH, HLS
from unshackle.core.manifests.dash import PeriodRebase, mdhd_timescale, tfdt_fields
from unshackle.core.manifests.ism_init import box, build_sinf, full_box
from unshackle.core.manifests.snapshot import fragments, unpack
from unshackle.core.manifests.trim import trim_overlaps
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


def audio_fragment(
    decode_time: int,
    samples: int,
    duration: int = 1024,
    encrypted: bool = False,
    per_sample_durations: bool = True,
    roll: bool = False,
    extra: bytes = b"",
) -> bytes:
    """A moof of one-byte samples, then an mdat holding each sample's index.

    encrypted adds senc, saiz and saio; roll adds an sbgp that puts the samples in two roll groups; extra goes in
    the traf as it is.
    """
    tfhd_flags, defaults = (0x020000, b"") if per_sample_durations else (0x020008, struct.pack(">I", duration))
    tfhd = full_box(b"tfhd", 0, tfhd_flags, struct.pack(">I", 1) + defaults)
    tfdt = full_box(b"tfdt", 1, 0, struct.pack(">Q", decode_time))
    entry = struct.pack(">II", duration, 1) if per_sample_durations else struct.pack(">I", 1)
    trun_size = 8 + 4 + 8 + len(entry) * samples
    half = samples // 2
    sbgp = full_box(b"sbgp", 0, 0, b"roll" + struct.pack(">IIIII", 2, half, 1, samples - half, 2)) if roll else b""
    head = 8 + 16 + 8 + len(tfhd) + len(tfdt) + trun_size + len(sbgp) + len(extra)
    senc = saiz = saio = b""
    if encrypted:
        ivs = b"".join(struct.pack(">QHHI", n, 1, 0, 1) for n in range(samples))
        senc = full_box(b"senc", 0, 0x2, struct.pack(">I", samples) + ivs)
        saiz = full_box(b"saiz", 0, 0, struct.pack(">BI", 16, samples))
        saio = full_box(b"saio", 0, 0, struct.pack(">II", 1, head + 16))
    moof_size = head + len(senc) + len(saiz) + len(saio)
    flags = 0x301 if per_sample_durations else 0x201
    trun = full_box(b"trun", 0, flags, struct.pack(">II", samples, moof_size + 8) + entry * samples)
    traf = box(b"traf", tfhd + tfdt + trun + sbgp + extra + senc + saiz + saio)
    return box(b"moof", box(b"mfhd", b"\0" * 8) + traf) + box(b"mdat", bytes(range(samples)))


def encrypted_init(timescale: int) -> bytes:
    mdhd = full_box(b"mdhd", 0, 0, struct.pack(">III", 0, 0, timescale) + b"\0" * 8)
    enca = box(b"enca", b"\0" * 28 + build_sinf(b"mp4a", b"\1" * 16, 8))
    stsd = full_box(b"stsd", 0, 0, struct.pack(">I", 1) + enca)
    return box(b"ftyp", b"iso6") + box(b"moov", box(b"trak", box(b"mdia", mdhd + box(b"minf", box(b"stbl", stsd)))))


def box_at(data: bytes, kind: bytes) -> int:
    return data.index(kind) - 4


JOIN = 3 * 1024 + 100


def overlapping(**kwargs: Any) -> bytes:
    """Five samples, then a period join two samples before they end."""
    return audio_fragment(0, 5, **kwargs) + audio_fragment(JOIN, 2, **kwargs)


def test_trim_overlaps_drops_the_audio_past_the_next_period(tmp_path: Path) -> None:
    path = tmp_path / "audio.mp4"
    path.write_bytes(init_segment(48000) + box(b"sidx", b"\0" * 24) + overlapping())

    assert trim_overlaps(path, {JOIN}) == 2

    data = path.read_bytes()
    assert b"sidx" not in data and b"free" in data
    assert tfdts(data) == [0, JOIN]
    mdat = box_at(data, b"mdat")
    assert data[mdat : mdat + 11] == struct.pack(">I", 11) + b"mdat" + bytes(range(3))
    assert trim_overlaps(path, {JOIN}) == 0


def test_download_track_trims_audio_that_overlaps_the_next_period(
    server: ThreadingHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # period 0 is 2 s in the manifest but its audio runs to 2.5 s; period 1 restarts at 0
    monkeypatch.setattr(Track, "get_key_id", lambda self, *args, **kwargs: None)
    for path, data in {
        "/a0/init.mp4": init_segment(1000),
        "/a0/1.m4s": audio_fragment(0, 3, 500),
        "/a0/2.m4s": audio_fragment(1500, 2, 500),
        "/a1/init.mp4": init_segment(1000),
        "/a1/1.m4s": audio_fragment(0, 2, 500),
        "/a1/2.m4s": audio_fragment(1000, 2, 500),
    }.items():
        server.routes[path] = data  # type: ignore[attr-defined]
    periods = "".join(
        f"""<Period id="{p}" duration="PT2S"><AdaptationSet contentType="audio" mimeType="audio/mp4" lang="en">
  <Representation id="a0" codecs="mp4a.40.2" bandwidth="128000" audioSamplingRate="48000">
    <SegmentTemplate timescale="1000" startNumber="1" initialization="a{p}/init.mp4" media="a{p}/$Number$.m4s">
      <SegmentTimeline><S t="0" d="1000" r="1"/></SegmentTimeline>
    </SegmentTemplate>
  </Representation></AdaptationSet></Period>"""
        for p in (0, 1)
    )
    mpd = f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
  <BaseURL>http://127.0.0.1:{server.server_address[1]}/</BaseURL>{periods}</MPD>"""
    audio = DASH.from_text(mpd, "http://127.0.0.1/manifest.mpd").to_tracks(language="en").audio[0]
    ctx = DownloadContext(
        save_path=tmp_path / "out.mp4", save_dir=tmp_path / "a0_segments", progress=partial(lambda **kw: None)
    )

    DASH.download_track(audio, ctx)

    data = (tmp_path / "out.mp4").read_bytes()
    assert tfdts(data) == [0, 1500, 2000, 3000]
    counts = [int.from_bytes(data[m.start() + 8 : m.start() + 12], "big") for m in re.finditer(b"trun", data)]
    assert counts == [3, 1, 2, 2]


def test_trim_overlaps_cuts_the_encryption_boxes_with_the_samples(tmp_path: Path) -> None:
    path = tmp_path / "audio.mp4"
    second = audio_fragment(JOIN, 2, encrypted=True, roll=True)
    path.write_bytes(encrypted_init(48000) + audio_fragment(0, 5, encrypted=True, roll=True) + second)

    assert trim_overlaps(path, {JOIN}) == 2

    data = path.read_bytes()
    moof, senc, saiz, saio, trun, sbgp, mdat = (
        box_at(data, k) for k in (b"moof", b"senc", b"saiz", b"saio", b"trun", b"sbgp", b"mdat")
    )
    assert int.from_bytes(data[senc + 12 : senc + 16], "big") == 3
    assert int.from_bytes(data[senc : senc + 4], "big") == 16 + 3 * 16
    assert int.from_bytes(data[saiz + 13 : saiz + 17], "big") == 3
    assert int.from_bytes(data[saio + 16 : saio + 20], "big") == senc + 16 - moof
    assert struct.unpack_from(">IIIII", data, sbgp + 16) == (2, 2, 1, 1, 2)
    assert int.from_bytes(data[trun + 16 : trun + 20], "big") == mdat + 8 - moof
    assert data[mdat + 8 : mdat + 11] == bytes(range(3))
    assert data.endswith(second)


def test_trim_overlaps_reads_default_durations(tmp_path: Path) -> None:
    path = tmp_path / "audio.mp4"
    path.write_bytes(init_segment(48000) + overlapping(per_sample_durations=False))
    assert trim_overlaps(path, {JOIN}) == 2


def test_trim_overlaps_drops_a_fragment_wholly_past_the_next_one(tmp_path: Path) -> None:
    path = tmp_path / "audio.mp4"
    path.write_bytes(
        init_segment(48000) + audio_fragment(0, 3) + audio_fragment(3 * 1024, 3) + audio_fragment(3 * 1024 + 400, 2)
    )
    assert trim_overlaps(path, {3 * 1024 + 400}) == 3
    data = path.read_bytes()
    assert tfdts(data) == [0, 3 * 1024 + 400]
    assert data.count(b"mdat") == 2


@pytest.mark.parametrize(
    ("fragments", "joins"),
    [
        pytest.param([(0, 3), (3 * 1024, 3)], {3 * 1024}, id="continuous"),
        pytest.param([(0, 5), (JOIN, 2)], {0}, id="overlap-away-from-a-join"),
        pytest.param([(0, 4), (4096, 4), (8192, 4), (100, 4)], {4096}, id="backwards-tfdt"),
    ],
)
def test_trim_overlaps_cuts_only_at_a_join(tmp_path: Path, fragments: list, joins: set) -> None:
    path = tmp_path / "audio.mp4"
    original = init_segment(48000) + b"".join(audio_fragment(t, n) for t, n in fragments)
    path.write_bytes(original)
    assert trim_overlaps(path, joins) == 0
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(struct.pack(">I", 0) + b"mdat" + b"\0" * 8, id="size-0"),
        pytest.param(struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 24) + b"\0" * 8, id="64-bit-size"),
        pytest.param(box(b"mdat", b"\0" * 8)[:-2], id="truncated"),
    ],
)
def test_trim_overlaps_leaves_a_file_it_cannot_read_to_the_end(tmp_path: Path, tail: bytes) -> None:
    path = tmp_path / "audio.mp4"
    original = init_segment(48000) + overlapping() + tail
    path.write_bytes(original)
    assert trim_overlaps(path, {JOIN}) == 0
    assert path.read_bytes() == original


def test_trim_overlaps_bounds_the_sample_count(tmp_path: Path) -> None:
    tfhd = full_box(b"tfhd", 0, 0x020018, struct.pack(">III", 1, 1024, 1))
    tfdt = full_box(b"tfdt", 1, 0, struct.pack(">Q", 0))
    trun = full_box(b"trun", 0, 0x001, struct.pack(">II", 0xFFFFFFFF, 0))
    huge = box(b"moof", box(b"traf", tfhd + tfdt + trun)) + box(b"mdat", b"\0" * 4)
    path = tmp_path / "audio.mp4"
    path.write_bytes(init_segment(48000) + huge + audio_fragment(100, 2))
    assert trim_overlaps(path, {100}) == 0


def test_trim_overlaps_logs_a_fragment_it_cannot_rewrite(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "audio.mp4"
    path.write_bytes(init_segment(48000) + overlapping(extra=box(b"sdtp", b"\0" * 4)))
    with caplog.at_level("DEBUG", logger="DASH"):
        assert trim_overlaps(path, {JOIN}) == 0
    assert "1 overlapping audio fragments" in caplog.text
