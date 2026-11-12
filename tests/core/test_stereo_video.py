from __future__ import annotations

import io
import itertools
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langcodes import Language

from unshackle.commands.dl import dl, hybrid_plan, pick_video
from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.drm.verify import decrypter_for
from unshackle.core.manifests import DASH, HLS
from unshackle.core.titles.movie import Movie
from unshackle.core.tracks import Audio, Chapter, Chapters, Track, Tracks, Video
from unshackle.core.tracks import tracks as tracks_module

VIDEO_IDS = itertools.count()

MASTER = """#EXTM3U
#EXT-X-VERSION:12
#EXT-X-STREAM-INF:BANDWIDTH=6786883,CODECS="dvh1.20.02",RESOLUTION=960x540,VIDEO-RANGE=PQ,REQ-VIDEO-LAYOUT="CH-MONO,CH-STEREO"
stereo.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,CODECS="hvc1.2.4.L150.90",RESOLUTION=1920x1080,REQ-VIDEO-LAYOUT="CH-STEREO"
mvhevc.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4000000,CODECS="dvh1.08.06",RESOLUTION=1920x1080,VIDEO-RANGE=PQ
flat.m3u8
"""


@pytest.mark.parametrize(
    ("codecs", "layout", "expected"),
    [
        ("dvh1.20.06,ec-3", None, True),
        ("dvhe.20.09", None, True),
        ("hvc1.2.4.L150.90", '"CH-MONO,CH-STEREO"', True),
        ("dvh1.08.06", '"CH-MONO"', False),
        ("dvh1.05.06", None, False),
        ("dvh1.200.06", None, False),
        (None, None, False),
    ],
)
def test_is_stereo(codecs: str | None, layout: str | None, expected: bool) -> None:
    assert Video.is_stereo(codecs, video_layout=layout) is expected


def test_hls_marks_stereo_tracks_from_the_layout_or_the_codec() -> None:
    tracks = HLS.from_text(MASTER, url="https://cdn.example/master.m3u8").to_tracks(Language.get("en"))
    stereo = {str(v.url).rsplit("/", 1)[-1]: v.stereo for v in tracks.videos}
    assert stereo == {"stereo.m3u8": True, "mvhevc.m3u8": True, "flat.m3u8": False}


def test_a_stereo_track_lists_as_3d_and_keeps_the_flag_through_to_dict() -> None:
    video = Video(url="https://x/v.mp4", language="en", codec=Video.Codec.HEVC, range_=Video.Range.DV, stereo=True)
    assert str(video).startswith("VID | [H.265, DV, 3D] |")
    assert Video.from_dict(video.to_dict()).stereo


def stereo_video(tmp_path: Path, stereo: bool = True, **kwargs: Any) -> Video:
    n = next(VIDEO_IDS)
    path = tmp_path / f"v{n}.mp4"
    path.write_bytes(b"\x00" * 16)
    video = Video(url=f"https://x/v{n}.mp4", language="en", codec=Video.Codec.HEVC, stereo=stereo, **kwargs)
    video.path = path
    return video


def test_dash_marks_a_dv_profile_20_representation_stereo() -> None:
    mpd = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
  <Period>
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <SegmentTemplate media="$RepresentationID$/$Number$.m4s" initialization="$RepresentationID$/init.mp4" duration="2" />
      <Representation id="v3d" codecs="dvh1.20.06" bandwidth="9000000" width="1920" height="1080" />
      <Representation id="v2d" codecs="dvh1.08.06" bandwidth="5000000" width="1920" height="1080" />
    </AdaptationSet>
  </Period>
</MPD>"""
    videos = DASH.from_text(mpd, url="https://cdn.example/manifest.mpd").to_tracks(language="en").videos
    assert {v.data["dash"]["representation"].get("id"): v.stereo for v in videos} == {"v3d": True, "v2d": False}


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def test_a_track_with_a_second_video_layer_decrypts_with_mp4decrypt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (layered := tmp_path / "3d.mp4").write_bytes(box(b"moov", box(b"lhvC")))
    (flat := tmp_path / "2d.mp4").write_bytes(box(b"moov", box(b"hvcC")))
    monkeypatch.setattr(config, "decryption", "Shaka")
    assert decrypter_for(layered) == "mp4decrypt"
    assert decrypter_for(flat) == "shaka"
    monkeypatch.setattr(config, "decryption", "MP4DECRYPT")
    assert decrypter_for(flat) == "mp4decrypt"


def test_a_3d_title_muxes_with_mp4box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(Tracks, "_mux_mp4", lambda self, **kwargs: calls.append(kwargs["title"]) or (Path("x"), 0, []))
    assert Tracks(stereo_video(tmp_path)).mux("Film")[1] == 0
    assert calls == ["Film"]


@pytest.fixture
def mp4box_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []

    class FakePopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            commands.append([str(c) for c in command])
            Path(command[-1]).write_bytes(b"mp4")
            self.stdout = io.StringIO("")

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(binaries, "MP4Box", Path("/bin/MP4Box"), raising=False)
    monkeypatch.setattr(tracks_module.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(tracks_module, "run_step", lambda command, **kwargs: commands.append([str(c) for c in command]))
    monkeypatch.setattr(config, "sample", None)
    monkeypatch.setattr(config.directories, "temp", tmp_path)
    return commands


def test_a_3d_mp4_mux_keeps_the_sample_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mp4box_commands: list[list[str]]
) -> None:
    monkeypatch.setattr(config, "sample", (120.0, 180.0))
    Tracks(stereo_video(tmp_path))._mux_mp4(title="Film", output_path=tmp_path / "out.mkv")
    assert len(mp4box_commands) == 1
    assert "-splitx" not in mp4box_commands[0]


def test_a_3d_mp4_mux_writes_quicktime_chapters(tmp_path: Path, mp4box_commands: list[list[str]]) -> None:
    for stereo, flag in ((True, "-chapqt"), (False, "-chap")):
        tracks = Tracks(stereo_video(tmp_path, stereo=stereo))
        tracks.chapters = Chapters([Chapter(0.0, "Story")])
        tracks._mux_mp4(title="Film", output_path=tmp_path / f"out{stereo}.mkv")
        assert flag in mp4box_commands[-1] and ({"-chap", "-chapqt"} - {flag}).isdisjoint(mp4box_commands[-1])


def test_a_3d_track_is_never_a_hybrid_ingredient(tmp_path: Path) -> None:
    flat, layered = (stereo_video(tmp_path, stereo=s, range_=Video.Range.DV) for s in (False, True))
    candidates, standalone = Tracks.partition_hybrid_videos([flat, layered], [])
    assert candidates == [flat] and standalone == []


def test_2d_ranks_over_3d_at_one_resolution_and_the_signatures_differ(tmp_path: Path) -> None:
    flat = stereo_video(tmp_path, stereo=False, height=1080, bitrate=5_000_000)
    layered = stereo_video(tmp_path, stereo=True, height=1080, bitrate=9_000_000)
    uhd = stereo_video(tmp_path, stereo=True, height=2160, bitrate=40_000_000, width=3840)
    tracks = Tracks(layered, flat, uhd)
    tracks.sort_videos()
    assert tracks.videos == [uhd, flat, layered]
    assert Tracks.signature(flat) != Tracks.signature(
        stereo_video(tmp_path, stereo=True, height=1080, bitrate=5_000_000)
    )


def test_a_3d_track_gets_no_ffmpeg_vui_rewrite_or_repack(tmp_path: Path) -> None:
    track = stereo_video(tmp_path, range_=Video.Range.HDR10)
    assert track.vui_bsf() is None
    assert track.repackage() is False
    assert track.path.exists()


def test_the_filename_context_marks_a_3d_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "tag_rules", [])
    monkeypatch.setattr(config, "language_tags", {})

    class Svc:
        pass

    def context(stereo: bool) -> str:
        movie = Movie(id_="movie-0001", service=Svc, name="The Film", year=2024, language="en")
        movie.tracks = Tracks(Video(url="https://x/v.mp4", language="en", codec=Video.Codec.HEVC, stereo=stereo))
        video = SimpleNamespace(
            width=3840,
            height=2160,
            format="HEVC",
            scan_type="Progressive",
            hdr_format_commercial="",
            hdr_format="",
            hdr_format_profile="",
            transfer_characteristics="PQ",
            transfer_characteristics_original=None,
            frame_rate="47.952",
        )
        return str(movie.build_base_template_context(SimpleNamespace(video_tracks=[video], audio_tracks=[]))["3d"])

    assert (context(True), context(False)) == ("3D", "")


def test_worst_prefers_the_2d_track_at_one_height(tmp_path: Path) -> None:
    videos = [
        stereo_video(tmp_path, stereo=s, height=2160, bitrate=b)
        for s, b in ((False, 9_000_000), (False, 5_000_000), (True, 40_000_000), (True, 30_000_000))
    ]
    ranked = Tracks(videos)
    ranked.sort_videos()
    assert pick_video(ranked.videos, worst=True) is videos[1]
    assert pick_video(ranked.videos, worst=False) is videos[0]
    assert pick_video([videos[3], videos[2]], worst=True) is videos[2]
    assert pick_video([], worst=True) is None


def test_hybrid_never_pairs_a_3d_dv_track(tmp_path: Path) -> None:
    base = stereo_video(tmp_path, stereo=False, range_=Video.Range.HDR10, height=2160)
    layered = stereo_video(tmp_path, stereo=True, range_=Video.Range.DV, height=2160)
    assert hybrid_plan([base, layered], generate_dv=False) == ([], None)


def test_mp4_names_skip_the_extra_tracks_of_a_3d_video_file(tmp_path: Path, mp4box_commands: list[list[str]]) -> None:
    video = stereo_video(tmp_path)
    video.path.write_bytes(box(b"moov", box(b"trak") + box(b"trak")))
    audio = Audio(url="https://x/a.mp4", language="en", codec=Audio.Codec.EC3, name="English")
    audio.path = tmp_path / "a.mp4"
    audio.path.write_bytes(b"\x00" * 16)
    Tracks([video, audio])._mux_mp4(title="Film", output_path=tmp_path / "out.mkv")
    command = mp4box_commands[0]
    assert command[command.index("-name") + 1] == "3=English"


def test_a_3d_mux_that_loses_its_second_view_warns(
    tmp_path: Path, mp4box_commands: list[list[str]], caplog: pytest.LogCaptureFixture
) -> None:
    Tracks(stereo_video(tmp_path))._mux_mp4(title="Film", output_path=tmp_path / "out.mkv")
    assert "lost its second view" in caplog.text


def test_a_downloaded_file_with_a_second_view_marks_the_video_3d(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = stereo_video(tmp_path, stereo=False)
    monkeypatch.setattr(Track, "download", lambda self, *a, **k: self.path.write_bytes(box(b"moov", box(b"lhvC"))))
    video.download(None, None)
    assert video.stereo


def test_dash_reads_dv_profile_20_from_supplemental_codecs() -> None:
    mpd = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10M">
  <Period id="1"><AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
    <Representation id="v" codecs="hvc1.2.4.L153" supplementalCodecs="dvh1.20.09" bandwidth="1" width="3840" height="2160">
      <BaseURL>v.mp4</BaseURL><SegmentBase indexRange="0-1"/>
    </Representation>
  </AdaptationSet></Period>
</MPD>"""
    assert DASH.from_text(mpd, url="https://cdn.example/m.mpd").to_tracks(language="en").videos[0].stereo


def test_hls_reads_each_variant_layout_even_when_two_share_a_playlist() -> None:
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2,CODECS="hvc1.2.4.L150.90",RESOLUTION=1920x1080,REQ-VIDEO-LAYOUT="CH-STEREO"
shared.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="hvc1.2.4.L150.90",RESOLUTION=1920x1080,AUDIO="a"
shared.m3u8
"""
    tracks = HLS.from_text(master, url="https://cdn.example/master.m3u8").to_tracks(Language.get("en"))
    assert any(v.stereo for v in tracks.videos)


def test_a_sample_is_cut_only_from_a_matroska_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = dl.__new__(dl)
    runner.log = logging.getLogger("download")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("mkvmerge ran on an MP4"))
    runner.write_sample(tmp_path / "Film.mp4", SimpleNamespace(), SimpleNamespace())
    assert not (tmp_path / "Sample").exists()
