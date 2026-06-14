"""Unit tests for the shared music rendering helpers in unshackle.core.music.display.

Rendering is visual, so these assert structure/type/no-crash rather than exact
ANSI. No network: the artwork soft-fail path is exercised with a session whose
get raises, and (when Pillow is absent) via the missing-dependency guard.
"""

from __future__ import annotations

from typing import Any, Optional

from rich.console import Console, RenderableType
from rich.panel import Panel

from unshackle.core.music.display import (MusicHeaderInfo, TrackRow, format_track_detail_quality, render_album_header,
                                          render_artwork_preview, render_track_panel)
from unshackle.core.titles.music import Song


class DummyService:
    """Stand-in service class; Song only requires a type, never an instance."""


def make_song(*, track: int = 1, name: str = "Song", album: str = "Album") -> Song:
    return Song(
        id_=f"{album}-{track}",
        service=DummyService,
        name=name,
        artist="Artist",
        album=album,
        track=track,
        disc=1,
        year=2020,
    )


def render_to_text(renderable: RenderableType) -> str:
    """Render a rich renderable to plain text so we can assert on its content."""
    console = Console(width=120, record=True, force_terminal=False)
    console.print(renderable)
    return console.export_text()


def test_trackrow_constructs() -> None:
    song = make_song(name="Track A")
    row = TrackRow(
        song=song,
        quality_label="FLAC 16-bit/44.1kHz",
        layout="Stereo",
        duration_str="3:21",
        hires=True,
        cd=True,
        note="",
    )
    assert row.song is song
    assert row.quality_label == "FLAC 16-bit/44.1kHz"
    assert row.layout == "Stereo"
    assert row.hires is True
    assert row.cd is True
    assert row.note == ""


def test_trackrow_defaults() -> None:
    row = TrackRow(song=make_song(), quality_label="MP3 320", layout="Stereo", duration_str="2:00")
    assert row.hires is False
    assert row.cd is False
    assert row.note == ""


def test_musicheaderinfo_constructs() -> None:
    info = MusicHeaderInfo(
        artist="Some Artist",
        album="Some Album",
        year="2021",
        track_count=12,
        quality_label="FLAC 24-bit/96kHz",
        duration_str="42:10",
    )
    assert info.artist == "Some Artist"
    assert info.album == "Some Album"
    assert info.year == "2021"
    assert info.track_count == 12
    assert info.quality_label == "FLAC 24-bit/96kHz"
    assert info.duration_str == "42:10"
    assert info.artist_label == "Artist"


def test_format_track_detail_quality_brackets_codec() -> None:
    assert format_track_detail_quality("FLAC 16-bit/44.1kHz") == "[FLAC] | 16-bit/44.1kHz"
    assert format_track_detail_quality("ogg 320kbps") == "[OGG] | 320kbps"


def test_format_track_detail_quality_passthrough() -> None:
    assert format_track_detail_quality("Lossless") == "Lossless"
    assert format_track_detail_quality("") == ""


def test_render_track_panel_returns_panel() -> None:
    rows = [
        TrackRow(song=make_song(track=1, name="First"), quality_label="FLAC 16-bit/44.1kHz", layout="Stereo", duration_str="3:00", cd=True),
        TrackRow(song=make_song(track=2, name="Second"), quality_label="OGG 320", layout="Stereo", duration_str="4:00", hires=True),
    ]
    panel = render_track_panel(rows, total=len(rows))
    assert isinstance(panel, Panel)

    text = render_to_text(panel)
    assert "First" in text
    assert "Second" in text
    assert "2 Tracks" in text
    assert "CD" in text
    assert "Hi-Res" in text


def test_render_track_panel_singular_label() -> None:
    rows = [TrackRow(song=make_song(name="Only"), quality_label="FLAC 16-bit/44.1kHz", layout="Stereo", duration_str="3:00")]
    text = render_to_text(render_track_panel(rows, total=1))
    # The count node reads "1 Track" (singular); the panel title is always
    # "Available Tracks", so we only check the count node phrasing here.
    assert "1 Track" in text
    assert "1 Tracks" not in text


def test_render_track_panel_note_marks_unavailable() -> None:
    rows = [
        TrackRow(
            song=make_song(name="Gone"),
            quality_label="FLAC",
            layout="Stereo",
            duration_str="3:00",
            cd=True,
            hires=True,
            note="(unavailable in region)",
        )
    ]
    text = render_to_text(render_track_panel(rows, total=1))
    assert "unavailable in region" in text
    # When a note is present, the badges are suppressed.
    assert "CD" not in text
    assert "Hi-Res" not in text


def test_render_album_header_without_artwork() -> None:
    info = MusicHeaderInfo(
        artist="The Artist",
        album="Greatest Hits",
        year="1999",
        track_count=10,
        quality_label="FLAC 16-bit/44.1kHz",
        duration_str="55:00",
    )
    header = render_album_header(info)
    assert header is not None
    text = render_to_text(header)
    assert "The Artist" in text
    assert "Greatest Hits" in text
    assert "1999" in text
    assert "10" in text


def test_render_album_header_with_artwork() -> None:
    info = MusicHeaderInfo(
        artist="Owner Name",
        album="A Playlist",
        year="2024",
        track_count=3,
        quality_label="OGG 320",
        artist_label="Owner",
    )
    artwork = render_album_header(MusicHeaderInfo("x", "y", "1", 1, "q"))  # any renderable
    header = render_album_header(info, artwork=artwork)
    assert header is not None
    text = render_to_text(header)
    assert "Owner Name" in text
    assert "A Playlist" in text
    assert "Owner" in text


class RaisingSession:
    """Session stub whose get() always raises, to drive the soft-fail path."""

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("network disabled in tests")


def test_render_artwork_preview_empty_url_returns_none() -> None:
    assert render_artwork_preview(RaisingSession(), "") is None


def test_render_artwork_preview_soft_fails_without_network() -> None:
    # Either Pillow is absent (guard returns None) or the session.get raises
    # (caught and returns None). Both paths must yield None, never raise.
    result: Optional[RenderableType] = render_artwork_preview(RaisingSession(), "https://example.invalid/cover.jpg")
    assert result is None
