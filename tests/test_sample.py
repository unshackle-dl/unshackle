"""A ``--sample`` run downloads only the segments of its window and cuts the mux to the window."""

from __future__ import annotations

import functools
import http.server
import struct
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional
from unittest.mock import MagicMock

import click
import pytest
from requests import Session

from unshackle.commands.dl import sample_name
from unshackle.core.api.handlers import validate_download_parameters
from unshackle.core.config import config
from unshackle.core.manifests.dash import DASH
from unshackle.core.services import Services
from unshackle.core.titles import Episode, Movie
from unshackle.core.tracks import Audio, Chapter, Subtitle, Tracks, Video
from unshackle.core.tracks.track import parse_sidx
from unshackle.core.utils.click_types import SAMPLE_RANGE
from unshackle.core.utils.xml import load_xml

STARTS = [1000 + i * 4000 for i in range(100)]  # 4 s segments at timescale 1000


def make_video() -> Video:
    return Video(id_="v", url="https://x/v", codec=Video.Codec.AVC, language="en", width=1920, height=1080, fps=24)


def make_audio(url: str = "https://x/a") -> Audio:
    return Audio(id_="a", url=url, codec=Audio.Codec.AAC, language="en", bitrate=128000)


def test_no_window_keeps_every_segment() -> None:
    assert make_audio().sample_slice(STARTS, 1000) == slice(None)


def test_the_window_is_measured_from_the_first_segment() -> None:
    track = make_audio()
    assert track.sample_slice(STARTS, 1000, (62, 120)) == slice(15, 30)
    assert track.sample_start == 60


def test_a_video_keeps_one_segment_after_the_window() -> None:
    """mkvmerge ends the cut only at a video keyframe, so the video must reach past the window."""
    assert make_video().sample_slice(STARTS, 1000, (0, 60)) == slice(0, 16)
    assert make_audio().sample_slice(STARTS, 1000, (0, 60)) == slice(0, 15)


def test_a_subtitle_keeps_one_segment_on_each_side_of_the_window() -> None:
    subtitle = Subtitle(id_="s", url="https://x/s", codec=Subtitle.Codec.WebVTT, language="en")
    video = make_video()
    video.sample_tail = 0
    keep = video.sample_slice(STARTS, 1000, (62, 120))
    assert subtitle.sample_slice(STARTS, 1000, (62, 120)) == slice(keep.start - 1, keep.stop + 1)
    assert subtitle.sample_start == 0, "cue times stay absolute, so the mux does not shift the track"
    assert subtitle.sample_slice(STARTS, 1000, None) == slice(None)


def test_a_subtitle_that_ends_before_the_window_keeps_its_last_segment() -> None:
    subtitle = Subtitle(id_="s", url="https://x/s", codec=Subtitle.Codec.WebVTT, language="en")
    assert subtitle.sample_slice([0, 4000], 1000, (120, 180)) == slice(0, None)


def test_a_window_after_the_end_stops_the_run() -> None:
    with pytest.raises(click.ClickException, match="ends before the sample window"):
        make_audio().sample_slice(STARTS[:10], 1000, (120, 180))


TIMELINE_MPD = """
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT400S">
  <Period id="0" start="PT0S">
    <AdaptationSet mimeType="audio/mp4">
      <SegmentTemplate timescale="1" media="a/$Number$-$Time$.mp4" startNumber="5">
        <SegmentTimeline><S t="1000" d="4" r="99"/></SegmentTimeline>
      </SegmentTemplate>
      <Representation id="a" bandwidth="1"/>
    </AdaptationSet>
  </Period>
</MPD>
"""

DURATION_MPD = """
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT400S">
  <Period id="0" start="PT0S" duration="PT400S">
    <AdaptationSet mimeType="audio/mp4">
      <SegmentTemplate timescale="25" duration="100" media="a/$Number$.mp4" startNumber="5"/>
      <Representation id="a" bandwidth="1"/>
    </AdaptationSet>
  </Period>
</MPD>
"""

LIST_MPD = """
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT400S">
  <Period id="0" start="PT0S">
    <AdaptationSet mimeType="audio/mp4">
      <Representation id="a" bandwidth="1">
        <BaseURL>https://example.com/a/</BaseURL>
        <SegmentList timescale="1" duration="4">{urls}</SegmentList>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
""".format(urls="".join(f'<SegmentURL media="{n}.mp4"/>' for n in range(100)))

BASE_MPD = """
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT400S">
  <Period id="0" start="PT0S">
    <AdaptationSet mimeType="audio/mp4">
      <Representation id="a" bandwidth="1"><BaseURL>https://example.com/a.mp4</BaseURL></Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


WINDOW = (62, 120)


def dash_names(mpd: str, window: Optional[tuple[int, int]]) -> tuple[list[str], float]:
    tree = load_xml(mpd.encode())
    period = tree.find("Period")
    aset = period.find("AdaptationSet")
    rep = aset.find("Representation")
    track = make_audio()
    _, segments, _, _, _ = DASH.get_period_segments(
        period, aset, rep, tree, track, "https://example.com/main.mpd", MagicMock(), window=window
    )
    return [url.rsplit("/", 1)[1].split(".")[0] for url, _ in segments], track.sample_start


def test_dash_timeline_keeps_the_numbers_and_times_of_the_window() -> None:
    names, start = dash_names(TIMELINE_MPD, WINDOW)
    assert names == [f"{5 + n}-{1000 + n * 4}" for n in range(15, 30)]
    assert start == 60


def test_dash_fixed_duration_keeps_the_numbers_of_the_window() -> None:
    names, start = dash_names(DURATION_MPD, WINDOW)
    assert names == [str(5 + n) for n in range(15, 30)]
    assert start == 60


def test_dash_segment_list_keeps_the_urls_of_the_window() -> None:
    names, start = dash_names(LIST_MPD, WINDOW)
    assert names == [str(n) for n in range(15, 30)]
    assert start == 60


@pytest.mark.parametrize("mpd", [TIMELINE_MPD, DURATION_MPD, LIST_MPD])
def test_a_caller_that_passes_no_window_gets_every_segment(mpd: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bitrate probe divides by the full duration, so a sample run must not cut its segment list."""
    monkeypatch.setattr(config, "sample", WINDOW)
    assert len(dash_names(mpd, None)[0]) == 100


@pytest.mark.parametrize(("mpd", "timed"), [(TIMELINE_MPD, True), (LIST_MPD, True), (BASE_MPD, False)])
def test_a_track_given_as_one_file_has_no_segment_timing(mpd: str, timed: bool) -> None:
    aset = load_xml(mpd.encode()).find("Period").find("AdaptationSet")
    track = SimpleNamespace(data={"dash": {"adaptation_set": aset, "representation": aset.find("Representation")}})
    assert DASH.has_segment_timing(track) is timed


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *args: None  # type: ignore[attr-defined]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(config.directories, "temp", tmp_path / "temp")
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_an_hls_subtitle_downloads_the_segments_around_the_window(
    tmp_path: Path, served: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(8):
        (tmp_path / f"s{n}.vtt").write_text(
            f"WEBVTT\n\n00:00:{n * 10 + 1:02}.000 --> 00:00:{n * 10 + 2:02}.000\ncue {n}\n"
        )
    (tmp_path / "s.m3u8").write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:10\n"
        + "".join(f"#EXTINF:10.0,\ns{n}.vtt\n" for n in range(8))
        + "#EXT-X-ENDLIST\n"
    )
    monkeypatch.setattr(config, "sample", (32, 38))
    subtitle = Subtitle(
        id_="s", url=f"{served}/s.m3u8", codec=Subtitle.Codec.WebVTT, language="en", descriptor=Subtitle.Descriptor.HLS
    )

    subtitle.download(session=Session(), prepare_drm=lambda *a, **k: None, progress=lambda **k: None)

    text = subtitle.path.read_text()
    assert [n for n in range(8) if f"cue {n}" in text] == [2, 3, 4]
    assert "00:00:31.000 --> 00:00:32.000" in text, "cue times stay absolute"


def test_subtitle_trim_keeps_the_cues_of_the_window_from_zero(tmp_path: Path) -> None:
    subtitle = Subtitle(id_="s", url="https://x/s", codec=Subtitle.Codec.SubRip, language="en")
    subtitle.path = tmp_path / "s.srt"
    subtitle.path.write_text(
        "1\n00:00:05,000 --> 00:00:06,000\nbefore\n\n"
        "2\n00:02:10,000 --> 00:02:12,000\ninside\n\n"
        "3\n00:10:00,000 --> 00:10:01,000\nafter\n"
    )

    assert subtitle.trim(120, 180) is True

    text = subtitle.path.read_text()
    assert "inside" in text and "before" not in text and "after" not in text
    assert "00:00:10,000 --> 00:00:12,000" in text
    assert subtitle.sample_start == 120


def test_subtitle_trim_leaves_an_unreadable_file_whole(tmp_path: Path) -> None:
    subtitle = Subtitle(id_="s", url="https://x/s", codec=Subtitle.Codec.fTTML, language="en")
    subtitle.path = tmp_path / "s.stpp"
    subtitle.path.write_bytes(b"\x00\x01not a subtitle")

    assert subtitle.trim(120, 180) is False
    assert subtitle.path.read_bytes() == b"\x00\x01not a subtitle"


def mux_command(tmp_path: Path, mux_args: Callable[..., list[str]]) -> list[str]:
    video, audio = make_video(), make_audio()
    trimmed = Subtitle(id_="s1", url="https://x/s1", codec=Subtitle.Codec.SubRip, language="en")
    whole = Subtitle(id_="s2", url="https://x/s2", codec=Subtitle.Codec.SubRip, language="fr")
    for track, name in ((video, "v.mp4"), (audio, "a.mp4"), (trimmed, "s1.srt"), (whole, "s2.srt")):
        track.path = tmp_path / name
        track.path.write_bytes(b"\x00" * 16)
    video.sample_start, audio.sample_start, trimmed.sample_start = 120.0, 119.5, 121.0
    tracks = Tracks([video, audio, trimmed, whole])
    tracks.chapters.add(Chapter("00:02:05.000", "Scene"))

    return mux_args(tracks, progress=lambda **k: None, output_path=tmp_path / "out.mkv")


def test_the_mux_shifts_each_track_and_cuts_to_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mux_args: Callable[..., list[str]]
) -> None:
    monkeypatch.setattr(config, "sample", (121, 181))
    command = mux_command(tmp_path, mux_args)

    def sync_before(name: str) -> str:
        file_at = command.index(str(tmp_path / name))
        options = command[:file_at]
        sync_at = len(options) - 1 - options[::-1].index("--sync")
        assert all(not arg.endswith((".mp4", ".srt")) for arg in command[sync_at:file_at]), (
            "the --sync is another file's"
        )
        return command[sync_at + 1]

    assert command[command.index("--split") + 1] == "parts:1.500s-61.500s"
    assert sync_before("v.mp4") == "-1:500"
    assert "--sync" not in command[: command.index(str(tmp_path / "a.mp4"))][-12:], "the earliest track is not moved"
    assert sync_before("s1.srt") == "-1:1500"
    assert sync_before("s2.srt") == "-1:-119500"
    assert command[command.index("--chapter-sync") + 1] == "-119500"
    assert command.index("--chapter-sync") < command.index("--chapters")


def test_a_full_download_adds_no_sample_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mux_args: Callable[..., list[str]]
) -> None:
    monkeypatch.setattr(config, "sample", None)
    command = mux_command(tmp_path, mux_args)
    assert not {"--split", "--sync", "--chapter-sync"} & set(command)


def test_sample_range_reads_a_window() -> None:
    assert SAMPLE_RANGE.convert("60-120", None, None) == (60, 120)
    assert SAMPLE_RANGE.convert(True, None, None) == (120, 180)
    assert SAMPLE_RANGE.convert(False, None, None) is None
    for bad in ("120-60", "60-60", "abc", "-5-10", "60"):
        with pytest.raises(click.BadParameter):
            SAMPLE_RANGE.convert(bad, None, None)


@pytest.mark.parametrize("sample", [True, "60-120", [60, 120]])
def test_the_api_accepts_a_sample_window(sample: object) -> None:
    assert validate_download_parameters({"sample": sample}) is None


@pytest.mark.parametrize("sample", ["abc", "120-60", [1, 2, 3], ["a", "b"], 5, {"start": 1}])
def test_the_api_refuses_a_bad_sample_window(sample: object) -> None:
    assert "sample" in (validate_download_parameters({"sample": sample}) or "")


def test_the_api_refuses_sample_with_include_sample() -> None:
    assert "include_sample" in (validate_download_parameters({"sample": True, "include_sample": True}) or "")


def test_sample_name_uses_the_separator_of_the_template(monkeypatch: pytest.MonkeyPatch) -> None:
    separators = {"movies": ".", "series": " "}
    monkeypatch.setattr(config, "get_template_separator", lambda kind="movies": separators[kind])
    movie = Movie.__new__(Movie)
    episode = Episode.__new__(Episode)
    assert sample_name("Some.Movie.2026.1080p-TAG", movie) == "Some.Movie.2026.1080p-TAG.SAMPLE"
    assert sample_name("Some Show S01E01 1080p-TAG", episode) == "Some Show S01E01 1080p-TAG SAMPLE"


SIDX_MPD = """
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT400S">
  <Period id="0" start="PT0S">
    <AdaptationSet mimeType="audio/mp4">
      <Representation id="a" bandwidth="1">
        <BaseURL>https://example.com/a.mp4</BaseURL>
        <SegmentBase indexRange="800-{last}"><Initialization range="0-799"/></SegmentBase>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


def sidx_box(count: int = 100, size: int = 5000, duration: int = 4000, timescale: int = 1000) -> bytes:
    body = struct.pack(">IIIII", 0, 1, timescale, 0, 0) + struct.pack(">HH", 0, count)
    body += b"".join(struct.pack(">III", size, duration, 0x90000000) for _ in range(count))
    return struct.pack(">I", 8 + len(body)) + b"sidx" + body


def test_parse_sidx_gives_the_time_and_byte_range_of_each_subsegment() -> None:
    box = sidx_box(count=3)
    end = 800 + len(box)
    assert parse_sidx(box, 800) == (
        1000,
        [(0, end, end + 4999), (4000, end + 5000, end + 9999), (8000, end + 10000, end + 14999)],
    )


def test_parse_sidx_refuses_what_it_cannot_cut() -> None:
    index_of_indexes = bytearray(sidx_box(count=2))
    index_of_indexes[32] |= 0x80
    assert parse_sidx(bytes(index_of_indexes), 0) == (0, [])
    assert parse_sidx(b"\x00\x00\x00\x20moof" + b"\x00" * 24, 0) == (0, [])
    assert parse_sidx(sidx_box(count=3)[:-4], 0) == (0, [])


def test_parse_sidx_is_right_when_tracks_parse_at_the_same_time() -> None:
    boxes = {758: sidx_box(count=758), 1408: sidx_box(count=1408, size=500)}
    wrong: list[int] = []

    def parse(count: int) -> None:
        for _ in range(100):
            if len(parse_sidx(boxes[count], 0)[1]) != count:
                wrong.append(count)

    threads = [threading.Thread(target=parse, args=(count,)) for count in (758, 1408, 758, 1408)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not wrong
    assert not any(thread.is_alive() for thread in threads)


def test_parse_sidx_refuses_a_box_that_counts_more_references_than_it_holds() -> None:
    box = bytearray(sidx_box(count=3))
    box[30:32] = (5).to_bytes(2, "big")
    assert parse_sidx(bytes(box) + bytes(100), 0) == (0, [])


def test_a_single_file_dash_track_is_cut_by_its_segment_index(monkeypatch: pytest.MonkeyPatch) -> None:
    box = sidx_box()
    requested: list[str] = []

    def get(url: str, headers: dict[str, str] | None = None) -> MagicMock:
        byte_range = (headers or {}).get("Range", "")
        requested.append(byte_range)
        content = box if byte_range == f"bytes=800-{800 + len(box) - 1}" else b"\x00" * 800
        return MagicMock(content=content, headers={"Content-Range": "bytes 0-799/900000"})

    tree = load_xml(SIDX_MPD.format(last=800 + len(box) - 1).encode())
    period = tree.find("Period")
    aset = period.find("AdaptationSet")
    rep = aset.find("Representation")
    track = make_audio()
    track.data["dash"] = {"adaptation_set": aset, "representation": rep}
    session = MagicMock(get=get)
    first_media_byte = 800 + len(box)

    _, segments, timescale, starts, _ = DASH.get_period_segments(
        period, aset, rep, tree, track, "https://example.com/main.mpd", session, probe_kid=False, window=WINDOW
    )

    assert DASH.has_segment_timing(track) is True
    assert timescale == 1000 and starts == [n * 4000 for n in range(15, 30)]
    assert segments[0] == (
        "https://example.com/a.mp4",
        f"{first_media_byte + 15 * 5000}-{first_media_byte + 16 * 5000 - 1}",
    )
    assert len(segments) == 15 and track.sample_start == 60

    requested.clear()
    _, whole, _, _, _ = DASH.get_period_segments(
        period, aset, rep, tree, track, "https://example.com/main.mpd", session, probe_kid=False
    )
    assert len(whole) == 1 and not any("800-" in r for r in requested), "a full download does not read the index"


@pytest.mark.parametrize(
    ("args", "window"),
    [
        (["--sample", "TAG", "-t", "x", "id"], (120, 180)),
        (["--sample", "60-120", "TAG", "id"], (60, 120)),
        (["TAG", "id"], None),
    ],
)
def test_a_bare_sample_option_does_not_take_the_service_tag(args: list[str], window: object) -> None:
    seen: dict[str, object] = {}

    class Group(Services):
        def list_commands(self, ctx: click.Context) -> list[str]:
            return ["TAG"]

        def get_command(self, ctx: click.Context, name: str) -> click.Command:
            return click.Command(
                name, params=[click.Option(["-t"]), click.Argument(["title"])], callback=lambda **k: None
            )

    @click.group(cls=Group)
    @click.option("--sample", type=SAMPLE_RANGE, default=None)
    def group(sample: object) -> None:
        seen["sample"] = sample

    group(args, standalone_mode=False)
    assert seen["sample"] == window


class RangeSession:
    """Serves byte ranges of one in-memory file and records each range it was asked for."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.ranges: list[str] = []

    def get(self, url: str, headers: dict[str, str]) -> SimpleNamespace:
        self.ranges.append(headers["Range"])
        first, last = (int(n) for n in headers["Range"].removeprefix("bytes=").split("-"))
        return SimpleNamespace(ok=True, content=self.data[first : last + 1])


def one_file(moov_size: int, index: bytes) -> bytes:
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom" + b"\x00" * 4
    moov = struct.pack(">I4s", moov_size, b"moov") + b"\x00" * (moov_size - 8)
    return ftyp + moov + index + b"\x00" * 600_000


@pytest.mark.parametrize("moov_size", [700, 200_000])
def test_a_one_file_track_is_cut_by_the_index_in_the_file(moov_size: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """A large moov puts the index after the first probe, so the probe must read on."""
    monkeypatch.setattr(config, "sample", WINDOW)
    box = sidx_box()
    session = RangeSession(one_file(moov_size, box))
    audio = make_audio("https://x/a.mp4")

    init, byte_range = audio.sample_byte_range(session)

    media = 16 + moov_size + len(box)
    assert init == session.data[: 16 + moov_size], "the init data stops before the index"
    assert byte_range == f"{media + 15 * 5000}-{media + 30 * 5000 - 1}"
    assert audio.sample_start == 60
    assert all(r.startswith("bytes=") for r in session.ranges), "every request is a byte range"


def test_a_one_file_track_with_no_index_cannot_be_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "sample", WINDOW)
    mdat = struct.pack(">I4s", 600_000, b"mdat") + b"\x00" * (600_000 - 8)
    audio = make_audio("https://x/a.mp4")
    assert audio.sample_byte_range(RangeSession(one_file(700, b"")[:716] + mdat)) is None
