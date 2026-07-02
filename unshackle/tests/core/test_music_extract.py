"""Unit tests for the shared music helpers in unshackle.core.music.extract."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from unshackle.core.music.extract import (build_music_from_songs, classify_release_kind, dedupe_track_options,
                                          duration_seconds, first_number, first_text, format_duration, format_names,
                                          year_from_value)
from unshackle.core.music.models import MusicTrackOption
from unshackle.core.titles.music import Music, Song


class DummyService:
    """Stand-in service class; Song only requires a type, never an instance."""


def make_song(
    *,
    track: int = 1,
    disc: int = 1,
    year: int = 2020,
    album: str = "Album",
    artist: str = "Artist",
    name: str = "Song",
    album_artist: Optional[str] = None,
    total_tracks: Optional[int] = None,
    total_discs: Optional[int] = None,
    artwork_url: Optional[str] = None,
    data: Optional[Any] = None,
) -> Song:
    return Song(
        id_=f"{album}-{disc}-{track}",
        service=DummyService,
        name=name,
        artist=artist,
        album=album,
        track=track,
        disc=disc,
        year=year,
        album_artist=album_artist,
        total_tracks=total_tracks,
        total_discs=total_discs,
        artwork_url=artwork_url,
        data=data,
    )


@pytest.mark.parametrize(
    ("values", "default", "expected"),
    [
        ((None, "", "  hello "), "", "hello"),
        (("", None), "fallback", "fallback"),
        (({"name": "Track"},), "", "Track"),
        (({"title": "T"},), "", "T"),
        (({"description": "D"},), "", "D"),
        ((["a", "", "b"],), "", "a, b"),
        ((123,), "", "123"),
        ((), "def", "def"),
    ],
)
def test_first_text(values: tuple[Any, ...], default: str, expected: str) -> None:
    assert first_text(*values, default=default) == expected


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((None, "", "12"), 12.0),
        (("3.5",), 3.5),
        (("abc", 7), 7.0),
        ((None, ""), None),
        ((), None),
    ],
)
def test_first_number(values: tuple[Any, ...], expected: Optional[float]) -> None:
    assert first_number(*values) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2021-05-04", 2021),
        ("released 1999 remaster", 1999),
        (2018, 2018),
        ("no year here", 1900),
        (None, 1900),
        ("", 1900),
    ],
)
def test_year_from_value(value: Any, expected: int) -> None:
    assert year_from_value(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (250, 250.0),
        (250000, 250.0),  # treated as milliseconds
        ("180", 180.0),
        (None, None),
        ("nope", None),
    ],
)
def test_duration_seconds(value: Any, expected: Optional[float]) -> None:
    assert duration_seconds(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0:00"),
        (5, "0:05"),
        (65, "1:05"),
        (599, "9:59"),
        (3600, "1:00:00"),
        (3725, "1:02:05"),
        (-5, "0:00"),
        (None, ""),
        ("bad", ""),
    ],
)
def test_format_duration(value: Any, expected: str) -> None:
    assert format_duration(value) == expected


@pytest.mark.parametrize(
    ("value", "sep", "expected"),
    [
        (["Alice", "Bob"], ", ", "Alice, Bob"),
        (["Alice", "Alice"], ", ", "Alice"),  # de-dupes
        ([{"profile": {"name": "DJ"}}], ", ", "DJ"),
        ({"items": [{"name": "X"}, {"name": "Y"}]}, " & ", "X & Y"),
        ("Solo", ", ", "Solo"),
    ],
)
def test_format_names(value: Any, sep: str, expected: str) -> None:
    assert format_names(value, sep=sep) == expected


@pytest.mark.parametrize(
    ("raw_kind", "count", "expected"),
    [
        ("single", 1, "single"),
        ("single", 3, "ep"),  # multi-track "single" -> EP
        ("EP", None, "ep"),
        ("Extended Play", None, "ep"),
        ("ep-single", 1, "single"),
        ("ep-single", 4, "ep"),
        ("album", None, "album"),
        ("compilation", None, "compilation"),
        ("Live Recording", None, "live"),
        ("download", None, "download"),
        ("playlist", None, "playlist"),
        ("other", None, "other"),
        ("totally-unknown", None, "album"),
        ("", None, "album"),
    ],
)
def test_classify_release_kind(raw_kind: str, count: Optional[float], expected: str) -> None:
    assert classify_release_kind(raw_kind, count) == expected


def test_dedupe_track_options() -> None:
    a = MusicTrackOption(codec="flac", bit_depth=16, sample_rate=44100, bitrate=None, quality_label="L", explicit=False)
    a_dup = MusicTrackOption(
        codec="FLAC", bit_depth=16, sample_rate=44100, bitrate=None, quality_label="L", explicit=False
    )  # codec case-insensitive duplicate of `a`
    b = MusicTrackOption(codec="flac", bit_depth=24, sample_rate=96000, bitrate=None, quality_label="H", explicit=False)
    c = MusicTrackOption(codec="flac", bit_depth=16, sample_rate=44100, bitrate=None, quality_label="L", explicit=True)

    result = dedupe_track_options([a, a_dup, b, c])
    assert result == [a, b, c]


def test_build_music_from_songs() -> None:
    songs = [
        make_song(
            track=1,
            disc=1,
            year=2019,
            album="Greatest",
            artist="Band",
            album_artist="The Band",
            total_tracks=2,
            total_discs=1,
            artwork_url="http://art/1.jpg",
            data={"duration": 200},
        ),
        make_song(
            track=2,
            disc=1,
            year=2019,
            album="Greatest",
            artist="Band",
            total_tracks=2,
            total_discs=1,
            data={"duration": 100},
        ),
    ]

    music = build_music_from_songs(songs, kind="album")

    assert isinstance(music, Music)
    assert music.kind == "album"
    assert music.title == "Greatest"
    assert music.artist == "The Band"  # album_artist preferred over artist
    assert music.year == 2019
    assert music.total_tracks == 2
    assert music.total_discs == 1
    assert music.total_duration == 300
    assert music.artwork_url == "http://art/1.jpg"


def test_build_music_from_songs_overrides() -> None:
    songs = [make_song(data={"duration": 60})]
    music = build_music_from_songs(
        songs,
        kind="playlist",
        title="My Mix",
        artist="Various",
        owner="george",
        description="best of",
    )
    assert music.title == "My Mix"
    assert music.artist == "Various"
    assert music.owner == "george"
    assert music.description == "best of"


def test_build_music_from_songs_empty_raises() -> None:
    with pytest.raises(ValueError, match="nothing here"):
        build_music_from_songs([], kind="album", empty_error="nothing here")
