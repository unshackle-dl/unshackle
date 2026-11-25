"""The placement of a title's output files: folder, name, and one rule for a name taken twice."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pymediainfo import MediaInfo

from tests.lang_selection.test_kind_only_selection import FakeService, run, stub_hybrid
from unshackle.core.config import config
from unshackle.core.placement import Placement
from unshackle.core.titles import Movie
from unshackle.core.titles.episode import Episode
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video

AAC_MP4 = """<?xml version="1.0" encoding="UTF-8"?>
<Mediainfo><File>
<track type="General"><Format>MPEG-4</Format></track>
<track type="Audio"><Format>AAC</Format><Channel_s_>2</Channel_s_></track>
</File></Mediainfo>"""


@pytest.fixture(autouse=True)
def templates() -> Iterator[None]:
    """Pin the naming templates, because the suite shares the live global config."""
    saved = (config.output_template, config.folder_templates, config.folder_template)
    config.output_template = {"movies": "{title}.{year}", "series": "{title}.{season_episode}"}
    config.folder_templates = {}
    config.folder_template = ""
    yield
    config.output_template, config.folder_templates, config.folder_template = saved


def movie() -> Movie:
    return Movie(id_="movie1", service=type("EXAMPLE", (), {}), name="Some Movie", year=2026)


def placement(root: Path, title: object = None, *, sample: bool = False, no_folder: bool = False) -> Placement:
    return Placement(title or movie(), root=root, no_folder=no_folder, no_source=False, sample=sample)


def audio(path: Path, track_id: str, *, descriptive: bool = False) -> Audio:
    track = Audio(
        id_=track_id,
        url=f"https://x.example/{track_id}.mp4",
        language="en",
        codec=Audio.Codec.AAC,
        descriptive=descriptive,
    )
    track.path = path
    return track


def test_two_same_language_aac_tracks_get_two_paths_without_mux(tmp_path: Path) -> None:
    place = placement(tmp_path / "out")
    folder = place.folder(MediaInfo(AAC_MP4))
    main = audio(tmp_path / "Audio_a1.mp4", "a1")
    described = audio(tmp_path / "Audio_a2.mp4", "a2", descriptive=True)
    paths = [place.track_file(folder, t, MediaInfo(AAC_MP4)) for t in (main, described)]
    assert [p.name for p in paths] == ["Some.Movie.2026.en.AAC.m4a", "Some.Movie.2026.en.AAC.2.m4a"]


def test_two_kept_tracks_beside_one_output_get_two_paths(tmp_path: Path) -> None:
    place = placement(tmp_path)
    output = place.claim(tmp_path, "Some.Movie.2026", ".mkv")
    kept = [audio(tmp_path / f"Audio_{n}.mp4", n) for n in ("a1", "a2")]
    names = [place.track_file(tmp_path, t, MediaInfo(AAC_MP4), stem=output.stem).name for t in kept]
    assert names == ["Some.Movie.2026.en.AAC.m4a", "Some.Movie.2026.en.AAC.2.m4a"]


def test_names_that_do_not_collide_keep_their_form(tmp_path: Path) -> None:
    place = placement(tmp_path)
    video = Video(id_="v", url="https://x.example/v", codec=Video.Codec.HEVC, language="en")
    video.path = tmp_path / "v.mp4"
    subtitle = Subtitle(id_="s", url="https://x.example/s", codec=Subtitle.Codec.WebVTT, language="en", sdh=True)
    subtitle.path = tmp_path / "s.vtt"
    tracks = [video, audio(tmp_path / "a.mp4", "a"), subtitle]
    names = [place.track_file(tmp_path, t, MediaInfo(AAC_MP4)).name for t in tracks]
    assert names == ["Some.Movie.2026.HEVC.mp4", "Some.Movie.2026.en.AAC.m4a", "Some.Movie.2026.en.sdh.vtt"]


def test_a_split_audio_mux_adds_the_codec_to_a_name_on_disk(tmp_path: Path) -> None:
    place = placement(tmp_path)
    first = place.claim(tmp_path, "Some.Movie.2026", ".mkv", Audio.Codec.AAC)
    first.touch()
    second = place.claim(tmp_path, "Some.Movie.2026", ".mkv", Audio.Codec.EC3)
    assert [first.name, second.name] == ["Some.Movie.2026.mkv", "Some.Movie.2026.EC3.mkv"]


def test_a_path_taken_twice_gets_a_counter(tmp_path: Path) -> None:
    place = placement(tmp_path)
    names = [place.claim(tmp_path, "Some.Movie.2026", ".mkv").name for _ in range(3)]
    assert names == ["Some.Movie.2026.mkv", "Some.Movie.2026.2.mkv", "Some.Movie.2026.3.mkv"]


def test_a_file_from_an_earlier_run_is_replaced_when_no_codec_tells_them_apart(tmp_path: Path) -> None:
    (tmp_path / "Some.Movie.2026.mkv").touch()
    assert placement(tmp_path).claim(tmp_path, "Some.Movie.2026", ".mkv").name == "Some.Movie.2026.mkv"


def test_folder_and_sample(tmp_path: Path) -> None:
    episode = Episode(
        id_="episode1", service=type("EXAMPLE", (), {}), title="Some Show", season=1, number=2, name="Pilot", year=2026
    )
    info = MediaInfo(AAC_MP4)
    assert placement(tmp_path).folder(info) == tmp_path
    assert placement(tmp_path, sample=True).folder(info) == tmp_path / "Sample"
    assert placement(tmp_path, episode, no_folder=True).folder(info) == tmp_path
    assert placement(tmp_path, episode).folder(None) == tmp_path
    assert placement(tmp_path, episode).folder(info).parent == tmp_path
    assert placement(tmp_path, sample=True).name(info) == "Some.Movie.2026.SAMPLE"


class DownloadedAudio(FakeService):
    """Each audio track comes with a file, as if the download had run."""

    def __init__(self, folder: Path) -> None:
        super().__init__({1: {"ad": True}})
        self.folder = folder

    def get_tracks(self, title: Episode) -> Tracks:
        tracks = super().get_tracks(title)
        for track in tracks.audio:
            track.path = self.folder / f"Audio_{track.id}.mp4"
            track.path.write_bytes(track.id.encode())
        return tracks


def test_a_no_mux_run_keeps_both_same_language_aac_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    out = tmp_path / "out"
    monkeypatch.setattr(config.directories, "downloads", out)
    stub_hybrid(monkeypatch)
    flags: dict[str, Any] = {"no_mux": True, "audio_description": True, "no_folder": True}
    assert run(monkeypatch, caplog, {}, service=DownloadedAudio(tmp_path), to_end=True, **flags) is None
    assert sorted(p.read_bytes() for p in out.iterdir()) == [b"a", b"ad"]
