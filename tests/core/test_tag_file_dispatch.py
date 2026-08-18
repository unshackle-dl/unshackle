"""Which tagger ``tag_file`` picks per title type.

A Song is an audio file, so it must reach mutagen and never mkvpropedit. The bug this
pins ran ``mkvpropedit`` against a ``.flac``. Movies and Episodes keep the Matroska path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unshackle.core.config import config
from unshackle.core.music.tagger import MusicMetadataResult
from unshackle.core.titles.episode import Episode
from unshackle.core.titles.movie import Movie
from unshackle.core.titles.music import Song
from unshackle.core.utils import tags as tags_module

pytestmark = pytest.mark.unit

SESSION = object()  # sentinel: tag_file must hand this straight to the music tagger


class DummyService:
    """Stand-in service class; a Title only requires a type, never an instance."""


class Calls:
    """Records which tagger ran, and with what."""

    def __init__(self) -> None:
        self.music: list[tuple[Path, Any, Any]] = []
        self.mkv: list[tuple[Path, dict[str, str]]] = []


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    recorded = Calls()

    def fake_write_music_metadata(path: Path, song: Any, *, session: Any = None, source_md5: str = "") -> Any:
        recorded.music.append((path, song, session))
        return MusicMetadataResult(written=True)

    def fake_apply_tags(path: Path, tags: dict[str, str]) -> None:
        recorded.mkv.append((path, tags))

    monkeypatch.setattr(tags_module, "write_music_metadata", fake_write_music_metadata)
    monkeypatch.setattr(tags_module, "apply_tags", fake_apply_tags)
    monkeypatch.setattr(config, "tag_imdb_tmdb", False)  # keep provider lookups out of a unit test
    return recorded


def make_song(**overrides: Any) -> Song:
    kwargs: dict[str, Any] = dict(
        id_="track-0001",
        service=DummyService,
        name="Example Track",
        artist="Example Artist",
        album="Example Album, Live & Remastered",
        track=4,
        year=1983,
    )
    kwargs.update(overrides)
    return Song(**kwargs)


def test_a_song_is_tagged_by_the_music_tagger(calls: Calls) -> None:
    path = Path("04. Example Track.flac")
    song = make_song()

    tags_module.tag_file(path, song, session=SESSION)

    assert calls.music == [(path, song, SESSION)], "the music tagger needs the session for cover art"
    assert calls.mkv == [], "mkvpropedit must never touch an audio container"


def test_a_skipped_song_warns_with_the_reason(
    calls: Calls, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        tags_module,
        "write_music_metadata",
        lambda path, song, **kwargs: MusicMetadataResult(skipped=True, reason="install mutagen to write FLAC tags"),
    )
    with caplog.at_level("WARNING", logger="TAGS"):
        tags_module.tag_file(Path("04. Example Track.flac"), make_song(), session=SESSION)
    assert "install mutagen to write FLAC tags" in caplog.text


def test_a_tagger_fault_warns_and_never_raises(
    calls: Calls, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The file already moved when tag_file runs, so a corrupt file must not abort the run."""

    def broken_tagger(path: Path, song: Any, **kwargs: Any) -> Any:
        raise OSError("not a FLAC stream")

    monkeypatch.setattr(tags_module, "write_music_metadata", broken_tagger)
    with caplog.at_level("WARNING", logger="TAGS"):
        tags_module.tag_file(Path("04. Example Track.flac"), make_song(), session=SESSION)
    assert "not a FLAC stream" in caplog.text


@pytest.mark.parametrize(
    "title",
    [
        Movie(id_="movie-0001", service=DummyService, name="Some Movie", year=2024),
        Episode(id_="episode-0001", service=DummyService, title="Some Show", season=1, number=2, name="Pilot"),
    ],
    ids=["movie", "episode"],
)
def test_a_movie_or_episode_still_takes_the_mkv_path(calls: Calls, title: Any) -> None:
    path = Path("Some Title.mkv")

    tags_module.tag_file(path, title)

    assert calls.music == [], "write_music_metadata must not see a Matroska file"
    assert [call[0] for call in calls.mkv] == [path]
