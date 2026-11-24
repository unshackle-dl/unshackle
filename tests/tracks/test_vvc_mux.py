from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pymediainfo import MediaInfo

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.drm.verify import read_top_level_box
from unshackle.core.titles import Movie
from unshackle.core.tracks import Tracks, Video
from unshackle.core.tracks import tracks as tracks_module


def vvc_mp4(path: Path) -> None:
    """Encode a few frames of H.266 into an MP4, or skip when FFmpeg has no VVC encoder."""
    if not binaries.FFMPEG:
        pytest.skip("needs ffmpeg")
    encode = subprocess.run(
        [binaries.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=s=128x72:r=24:d=0.25"]
        + ["-c:v", "libvvenc", "-preset", "faster", str(path)],
        capture_output=True,
    )
    if encode.returncode:
        pytest.skip("ffmpeg has no libvvenc")


def vvc_tracks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tracks:
    monkeypatch.setattr(config.directories, "temp", tmp_path / "temp", raising=False)
    (tmp_path / "temp").mkdir()
    video = Video(url="https://x.example/v.m3u8", language="en", codec=Video.Codec.VVC, id_="v")
    video.path = tmp_path / "v.mp4"
    vvc_mp4(video.path)
    tracks = Tracks()
    tracks.videos = [video]
    return tracks


def video_codecs(path: Path) -> list[str]:
    return [t["properties"]["codec_id"] for t in tracks_module.mkvmerge_tracks(path) if t["type"] == "video"]


def test_an_h266_track_is_muxed_as_vvc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not binaries.MKVToolNix:
        pytest.skip("needs mkvmerge")
    tracks = vvc_tracks(tmp_path, monkeypatch)
    out, code, _ = tracks.mux("t", delete=False, audio_expected=False, output_path=tmp_path / "out.mkv")
    assert code == 0 and video_codecs(out) == ["V_MPEGI/ISO/VVC"]
    assert tracks.videos[0].path == tmp_path / "v.mp4" and tracks.videos[0].path.exists()
    assert not list((tmp_path / "temp").iterdir())
    movie = Movie(id_="movie-0001", service=type("EXAMPLE", (), {}), name="Some Movie", year=2024)
    assert ".H.266" in movie.get_filename(MediaInfo.parse(out))


def test_the_mp4box_route_gets_the_original_h266_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not getattr(binaries, "MP4Box", None):
        pytest.skip("needs MP4Box")
    tracks = vvc_tracks(tmp_path, monkeypatch)
    tracks.videos[0].stereo = True
    out, code, _ = tracks.mux("t", delete=False, audio_expected=False, output_path=tmp_path / "out.mkv")
    moov = read_top_level_box(out, b"moov") or b""
    assert code == 0 and out.suffix == ".mp4" and b"vvc1" in moov and b"FFID" not in moov


def test_a_remux_with_the_wrong_codec_id_stops_the_mux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracks = vvc_tracks(tmp_path, monkeypatch)
    old_ffmpeg = [{"type": "video", "properties": {"codec_id": "V_MS/VFW/FOURCC"}}]
    monkeypatch.setattr(tracks_module, "mkvmerge_tracks", lambda path: old_ffmpeg)
    with pytest.raises(RuntimeError, match="FFmpeg wrote H.266 as V_MS/VFW/FOURCC"):
        tracks_module.remux_vvc(tracks.videos[0].path)
    assert not list((tmp_path / "temp").iterdir())
