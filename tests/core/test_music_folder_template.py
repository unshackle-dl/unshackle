"""Tests for the dedicated album-folder template (`output_template.folder.albums`)."""

from __future__ import annotations

import warnings

import pytest

from unshackle.core.config import Config, config
from unshackle.core.titles.music import Song
from unshackle.core.utilities import sanitize_filename


class DummyService:
    pass


class StubMediaInfo:
    """Minimal MediaInfo stand-in: the template context only reads track lists."""

    video_tracks: list = []
    audio_tracks: list = []


def make_song(**overrides) -> Song:
    kwargs = dict(
        id_="track-0001",
        service=DummyService,
        name="NUEVAYoL",
        artist="Bad Bunny",
        album="DeBI TiRAR MaS FOToS",
        track=1,
        disc=1,
        year=2025,
        album_artist="Bad Bunny",
    )
    kwargs.update(overrides)
    return Song(**kwargs)


@pytest.fixture
def reset_folder_config():
    """Save/restore the global config's template attributes around each test."""
    saved = (config.folder_templates, config.folder_template, config.output_template)
    config.folder_templates = {}
    config.folder_template = ""
    config.output_template = {}
    yield config
    config.folder_templates, config.folder_template, config.output_template = saved


def test_folder_fallback_when_no_templates(reset_folder_config):
    song = make_song()
    result = song.get_filename(StubMediaInfo(), folder=True)
    assert result == sanitize_filename("Bad Bunny - DeBI TiRAR MaS FOToS (2025)", " ")


def test_albums_template_used(reset_folder_config):
    reset_folder_config.folder_templates = {"albums": "{album_artist} - {album} ({year})"}
    result = make_song().get_filename(StubMediaInfo(), folder=True)
    assert result == sanitize_filename("Bad Bunny - DeBI TiRAR MaS FOToS (2025)", " ")
    # Album folder must NOT carry per-track info like the song file name does.
    assert "01" not in result


def test_albums_preferred_over_songs(reset_folder_config):
    reset_folder_config.folder_templates = {"albums": "AA-{album}", "songs": "SS-{album}"}
    result = make_song().get_filename(StubMediaInfo(), folder=True)
    assert result.startswith("AA-")


def test_songs_folder_used_when_no_albums(reset_folder_config):
    # Backward compatibility: the legacy "songs" folder kind still names the album folder.
    reset_folder_config.folder_templates = {"songs": "SS-{album}"}
    result = make_song().get_filename(StubMediaInfo(), folder=True)
    assert result.startswith("SS-")


def test_validation_accepts_music_variables_and_albums_kind():
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning becomes a test failure
        Config(
            output_template={
                "songs": "{track_number}. {title}",
                "folder": {"albums": "{album_artist} - {album} ({year?})"},
            }
        )


def test_validation_warns_on_unknown_folder_kind():
    with pytest.warns(UserWarning, match="Unknown folder template kind"):
        # A non-folder key is required so output-template validation actually runs.
        Config(output_template={"songs": "{track_number}. {title}", "folder": {"bogus": "{album}"}})
