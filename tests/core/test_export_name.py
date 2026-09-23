"""Tests for the ``--export`` file name and the messages a run with ``--export`` logs."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

import mediaexport
import pytest

from unshackle.commands.dl import dl
from unshackle.core.config import config
from unshackle.core.export_name import ExportNamer, export_name, move_export, season_episodes
from unshackle.core.titles import Episode, Movie, Song
from unshackle.core.tracks import Audio, Subtitle, Video

KID = UUID(hex="00000000000000000000000000000001")


class StubService:
    """Stands in for the service class slot on a title; never instantiated."""


class StubDRM:
    def __init__(self) -> None:
        self.content_keys = {KID: "aa" * 16}

    def to_dict(self) -> dict:
        return {"system": "Widevine", "pssh_b64": "AAAA"}


def video(
    id_: str = "v1",
    height: int = 1080,
    width: int = 1920,
    codec: Video.Codec = Video.Codec.AVC,
    range_: Video.Range = Video.Range.SDR,
    descriptor: Video.Descriptor = Video.Descriptor.DASH,
) -> Video:
    return Video(
        id_=id_,
        url=f"https://example.test/{id_}.mp4",
        language="en",
        codec=codec,
        range_=range_,
        width=width,
        height=height,
        descriptor=descriptor,
    )


def audio(
    id_: str = "a1",
    codec: Audio.Codec = Audio.Codec.AAC,
    channels: str = "2.0",
    descriptor: Audio.Descriptor = Audio.Descriptor.DASH,
    joc: int = 0,
) -> Audio:
    return Audio(
        id_=id_,
        url=f"https://example.test/{id_}.mp4",
        language="en",
        codec=codec,
        channels=channels,
        descriptor=descriptor,
        joc=joc,
    )


def episode(season: int, number: int, name: str = "Pilot") -> Episode:
    return Episode(
        id_=f"ep-{season}-{number}", service=StubService, title="Example Show", season=season, number=number, name=name
    )


def movie(name: str = "Example Movie", year: int | None = 2024) -> Movie:
    return Movie(id_="movie-1", service=StubService, name=name, year=year)


def seasons_of(*shape: tuple[int, int]) -> dict[int, set[int]]:
    return season_episodes([episode(s, n) for s, count in shape for n in range(1, count + 1)])


def test_movie_name_has_year_and_no_scope() -> None:
    name = export_name([movie()], [video(), audio(descriptor=Audio.Descriptor.HLS)], "EXAMPLE")
    assert name == "Example.Movie.2024.1080p.h264.sdr.dash.hls.aac2.0-EXAMPLE"


def test_movie_without_year() -> None:
    assert export_name([movie(year=None)], [video()], "EXAMPLE") == "Example.Movie.1080p.h264.sdr.dash-EXAMPLE"


def test_full_season_is_the_season_number() -> None:
    titles = [episode(1, n) for n in range(1, 4)]
    name = export_name(titles, [video(), audio(codec=Audio.Codec.AC3, channels="5.1")], "EXAMPLE", seasons_of((1, 3)))
    assert name == "Example.Show.S01.1080p.h264.sdr.dash.dd5.1-EXAMPLE"


def test_contiguous_subset_is_one_range() -> None:
    titles = [episode(1, n) for n in (5, 6, 7, 8, 9)]
    assert export_name(titles, [], "EXAMPLE", seasons_of((1, 10))) == "Example.Show.S01E05-E09-EXAMPLE"


def test_non_contiguous_subset_lists_each_run() -> None:
    titles = [episode(1, n) for n in (1, 2, 5, 9)]
    assert export_name(titles, [], "EXAMPLE", seasons_of((1, 10))) == "Example.Show.S01E01-E02.S01E05.S01E09-EXAMPLE"


def test_many_runs_are_cut_short() -> None:
    titles = [episode(1, n) for n in (1, 3, 5, 7, 9, 11)]
    name = export_name(titles, [], "EXAMPLE", seasons_of((1, 12)))
    assert name == "Example.Show.S01E01.S01E03.S01E05.+3-EXAMPLE"


def test_unknown_season_list_uses_ranges() -> None:
    titles = [episode(1, n) for n in (1, 2, 3)]
    assert export_name(titles, [], "EXAMPLE") == "Example.Show.S01E01-E03-EXAMPLE"


def test_whole_seasons_in_a_row_collapse() -> None:
    titles = [episode(s, n) for s in (1, 2, 3) for n in (1, 2)]
    assert export_name(titles, [], "EXAMPLE", seasons_of((1, 2), (2, 2), (3, 2))) == "Example.Show.S01-S03-EXAMPLE"


def test_whole_season_and_a_partial_season() -> None:
    titles = [episode(1, 1), episode(1, 2), episode(2, 1)]
    assert export_name(titles, [], "EXAMPLE", seasons_of((1, 2), (2, 4))) == "Example.Show.S01.S02E01-EXAMPLE"


def test_quality_parts_are_deduplicated_and_ordered() -> None:
    tracks = [
        video("v1", 1080, codec=Video.Codec.HEVC, range_=Video.Range.HDR10),
        video("v2", 2160, 3840, codec=Video.Codec.HEVC, range_=Video.Range.SDR, descriptor=Video.Descriptor.ISM),
        video("v3", 1080, codec=Video.Codec.AVC, range_=Video.Range.DV),
        video("v4", 800, 1920, codec=Video.Codec.HEVC, range_=Video.Range.HDR10),
        audio("a1", Audio.Codec.EC3, "5.1", joc=16),
        audio("a2", Audio.Codec.AAC, "2.0"),
        audio("a3", Audio.Codec.EC3, "5.1", joc=16),
        audio("a4", Audio.Codec.EC3, "2.0"),
        Subtitle(id_="s1", url="https://example.test/s1.vtt", language="en", codec=Subtitle.Codec.WebVTT),
    ]
    name = export_name([movie()], tracks, "EXAMPLE")
    assert name == "Example.Movie.2024.2160p.1080p.h264.h265.sdr.hdr10.dv.dash.ism.aac2.0.ddp5.1atmos.ddp2.0-EXAMPLE"


def test_title_is_filename_safe_and_keeps_letters() -> None:
    name = export_name([movie('Café: "Ünïcode"/Kanji 映画?', year=None)], [], "EXAMPLE")
    assert name == "Café.Ünïcode.&.Kanji.映画-EXAMPLE"


def test_output_template_does_not_change_the_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config,
        "output_template",
        {"series": "{title} {season_episode} {quality}", "movies": "{title} ({year}) {quality}"},
    )
    monkeypatch.setattr(config, "unicode_filenames", False)
    titles = [episode(1, n) for n in (1, 2, 3)]
    tracks = [video(descriptor=Video.Descriptor.HLS), audio("a1", Audio.Codec.EC3, "5.1", Audio.Descriptor.HLS, 16)]
    assert export_name(titles, tracks, "EX") == "Example.Show.S01E01-E03.1080p.h264.sdr.hls.ddp5.1atmos-EX"


def test_song_uses_artist_and_album() -> None:
    song = Song(
        id_="track-1", service=StubService, name="Track", artist="Example Artist", album="Example Album", track=1
    )
    assert export_name([song], [audio(codec=Audio.Codec.FLAC)], "EXAMPLE") == (
        "Example.Artist.Example.Album.dash.flac2.0-EXAMPLE"
    )


def test_collision_gets_a_suffix(tmp_path: Path) -> None:
    other = tmp_path / "Example.Movie.2024-EXAMPLE.json"
    other.write_text("{}")
    current = tmp_path / "export_EXAMPLE_1.json"
    current.write_text("mine")

    moved = move_export(current, "Example.Movie.2024-EXAMPLE")

    assert moved.name == "Example.Movie.2024-EXAMPLE-2.json"
    assert other.read_text() == "{}"
    assert moved.read_text() == "mine"
    assert not current.exists()
    # the run's own file keeps its suffix instead of moving on to -3
    assert move_export(moved, "Example.Movie.2024-EXAMPLE") == moved


def make_dl(tmp_path: Path, seasons: dict[int, set[int]] | None = None) -> dl:
    instance = dl.__new__(dl)
    instance.service = "EXAMPLE"
    instance.log = logging.getLogger("download")
    instance.export_namer = ExportNamer("EXAMPLE", seasons or {})
    return instance


def test_export_is_renamed_as_titles_arrive_and_stays_valid(tmp_path: Path) -> None:
    instance = make_dl(tmp_path, seasons_of((1, 2)))
    work = tmp_path / "export_EXAMPLE_1.json"
    first, second = episode(1, 1), episode(1, 2)
    for title in (first, second):
        title.tracks.add(video(f"v{title.number}"))

    instance.write_export(work, first, first.tracks.videos[0], StubDRM())
    partial = tmp_path / "Example.Show.S01E01.1080p.h264.sdr.dash-EXAMPLE.json"
    assert [p.name for p in tmp_path.iterdir()] == [partial.name]
    assert [e.id for e in mediaexport.read(partial).titles] == ["ep-1-1"]

    instance.write_export(work, second, second.tracks.videos[0])
    final = tmp_path / "Example.Show.S01.1080p.h264.sdr.dash-EXAMPLE.json"
    assert [p.name for p in tmp_path.iterdir()] == [final.name]
    assert [e.id for e in mediaexport.read(final).titles] == ["ep-1-1", "ep-1-2"]


def test_export_messages(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    instance = make_dl(tmp_path)
    title = movie()
    title.tracks.add(video())
    title.tracks.add(audio())
    title.tracks.add(audio("a2"))
    work = tmp_path / "export_EXAMPLE_1.json"

    with caplog.at_level(logging.INFO, logger="download"):
        for track in title.tracks:
            instance.write_export(work, title, track, StubDRM() if isinstance(track, Video) else None)
        instance.log_export_title(title)
        instance.log_export_summary()

    lines = [r.getMessage() for r in caplog.records]
    assert "Exporting to Example.Movie.2024.1080p.h264.sdr.dash.aac2.0-EXAMPLE.json" in lines
    assert "Exported 'Example Movie' (2024): 1 video, 2 audio, 0 subtitles, 1 key" in lines
    final = tmp_path / "Example.Movie.2024.1080p.h264.sdr.dash.aac2.0-EXAMPLE.json"
    assert f"Saved the export to {final}: 1 title, 1 key" in lines
    assert any("content keys" in line for line in lines)


def test_episode_message_names_the_episode(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    instance = make_dl(tmp_path)
    title = episode(1, 5, "Example Episode")
    title.tracks.add(video())
    instance.write_export(tmp_path / "export_EXAMPLE_1.json", title, title.tracks.videos[0])

    with caplog.at_level(logging.INFO, logger="download"):
        instance.log_export_title(title)

    assert "Exported S01E05 'Example Episode': 1 video, 0 audio, 0 subtitles, 0 keys" in caplog.messages


def test_summary_when_nothing_was_exported(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    instance = make_dl(tmp_path)
    with caplog.at_level(logging.INFO, logger="download"):
        instance.log_export_summary()
    assert "Nothing was exported" in caplog.messages
