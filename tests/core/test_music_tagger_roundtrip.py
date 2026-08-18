"""Round-trip ``write_music_metadata`` against real audio files.

The dispatch test pins which tagger runs; this pins what it writes. Silent audio is
generated with ffmpeg, tagged from a fully populated Song, and read back with mutagen.
Cover art goes through the same path with a fake session, so no network is touched.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis

from unshackle.core.music.tagger import write_music_metadata
from unshackle.core.titles.music import Song

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="requires ffmpeg")

PNG_STUB = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16  # magic only; Pillow dimension probing soft-fails


class DummyService:
    """Stand-in service class; a Title only requires a type, never an instance."""


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.headers = {"Content-Type": "image/png"}

    def raise_for_status(self) -> None:
        pass

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        pass


class FakeSession:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.requested: list[str] = []

    def get(self, url: str, timeout: int = 0) -> FakeResponse:
        self.requested.append(url)
        return FakeResponse(self.content)


@pytest.fixture
def flac_path(tmp_path: Path) -> Path:
    path = tmp_path / "04. Example Track.flac"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.2", str(path)],
        check=True,
    )
    return path


def make_song() -> Song:
    return Song(
        id_="track-0001",
        service=DummyService,
        name="Example Track",
        artist="Example Artist",
        album="Example Album, Live & Remastered",
        track=4,
        year=1983,
        album_artist="Example Artist",
        total_tracks=8,
        genre="Synth-pop",
        isrc="ZZABC2400001",
        artwork_url="https://cdn.example/cover.png",
    )


def test_flac_tags_read_back(flac_path: Path) -> None:
    result = write_music_metadata(flac_path, make_song())

    assert result.written
    assert not result.skipped
    assert not result.artwork_embedded, "no session was given, so no cover may be embedded"

    audio = FLAC(flac_path)
    assert audio["TITLE"] == ["Example Track"]
    assert audio["ARTIST"] == ["Example Artist"]
    assert audio["ALBUM"] == ["Example Album, Live & Remastered"]
    assert audio["ALBUMARTIST"] == ["Example Artist"]
    assert audio["TRACKNUMBER"] == ["4/8"]
    assert audio["TRACKTOTAL"] == ["8"]
    assert audio["DATE"] == ["1983"]
    assert audio["GENRE"] == ["Synth-pop"]
    assert audio["ISRC"] == ["ZZABC2400001"]


def test_lyrics_land_in_the_container_field_players_read(tmp_path: Path) -> None:
    """Each container has one lyrics field. A generic custom tag is not read as lyrics.

    Both the MP3 and MP4 writers fall back to writing an unmapped key as a custom tag
    (TXXX / freeform atom), so a lyrics field that is not mapped explicitly is written
    but never displayed.
    """
    lyrics = "Line one\nLine two"
    song = Song(
        id_="track-0001",
        service=DummyService,
        name="Example Track",
        artist="Example Artist",
        album="Example Album",
        track=1,
        lyrics=lyrics,
    )

    for suffix, encoder in ((".flac", "flac"), (".ogg", "libvorbis"), (".mp3", "libmp3lame"), (".m4a", "aac")):
        path = tmp_path / f"track{suffix}"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=mono",
                "-t",
                "0.2",
                "-c:a",
                encoder,
                str(path),
            ],
            check=True,
        )
        assert write_music_metadata(path, song).written

        if suffix == ".flac":
            assert FLAC(path)["LYRICS"] == [lyrics]
        elif suffix == ".ogg":
            assert OggVorbis(path)["LYRICS"] == [lyrics]
        elif suffix == ".mp3":
            tags = MP3(path).tags
            assert tags.getall("USLT")[0].text == lyrics
            assert not tags.getall("TXXX:LYRICS")
        else:
            audio = MP4(path)
            assert audio["\xa9lyr"] == [lyrics]
            assert not [key for key in audio if "LYRICS" in key]


def test_a_song_without_lyrics_writes_no_lyrics_field(flac_path: Path) -> None:
    write_music_metadata(flac_path, make_song())

    assert "LYRICS" not in FLAC(flac_path)


def test_cover_art_is_fetched_with_the_session_and_embedded(flac_path: Path) -> None:
    session = FakeSession(PNG_STUB)

    result = write_music_metadata(flac_path, make_song(), session=session)

    assert result.artwork_embedded
    assert session.requested == ["https://cdn.example/cover.png"]
    audio = FLAC(flac_path)
    assert len(audio.pictures) == 1
    assert audio.pictures[0].mime == "image/png"
    assert audio.pictures[0].data == PNG_STUB


def test_a_nested_metadata_dict_reaches_the_tags(flac_path: Path) -> None:
    """The renderer reads a nested "metadata" sub-dict, so the tagger must read it too.

    A service that nests its metadata used to lose every tag sourced from song.data,
    silently and with no log line.
    """
    song = Song(
        id_="track-0001",
        service=DummyService,
        name="Example Track",
        artist="Example Artist",
        album="Example Album",
        track=1,
        data={"metadata": {"composer": "Example Composer", "release_date": "2020-01-02"}},
    )

    write_music_metadata(flac_path, song)

    audio = FLAC(flac_path)
    assert audio["COMPOSER"] == ["Example Composer"]
    assert audio["RELEASEDATE"] == ["2020-01-02"]
    assert audio["DATE"] == ["2020-01-02"], "a full release date must beat the bare year"
