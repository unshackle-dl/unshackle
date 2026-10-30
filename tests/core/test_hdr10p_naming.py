"""Tests for the HDR tag in the filename context when MediaInfo misses HDR10+."""

from __future__ import annotations

import types

import pytest

from unshackle.core.config import config
from unshackle.core.titles.movie import Movie
from unshackle.core.tracks import Tracks, Video


class DummyService:
    pass


@pytest.fixture(autouse=True)
def clean_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "tag_rules", [])
    monkeypatch.setattr(config, "language_tags", {})
    monkeypatch.setattr(config, "codec_names", {})


def hdr_tag(commercial: str, range_: Video.Range) -> str:
    movie = Movie(id_="movie-0001", service=DummyService, name="The Film", year=2024, language="en")
    movie.tracks = Tracks(Video(url="https://example.test/v.mpd", language="en", codec=Video.Codec.AV1, range_=range_))
    video = types.SimpleNamespace(
        width=1920,
        height=1080,
        format="AV1",
        scan_type="Progressive",
        other_display_aspect_ratio=["16:9"],
        hdr_format_commercial=commercial,
        hdr_format="SMPTE ST 2086",
        transfer_characteristics="PQ",
        transfer_characteristics_original=None,
        frame_rate="23.976",
    )
    media_info = types.SimpleNamespace(video_tracks=[video], audio_tracks=[])
    return str(movie.build_base_template_context(media_info)["hdr"])


def test_an_hdr10p_track_upgrades_a_plain_hdr10_tag() -> None:
    assert hdr_tag("HDR10", Video.Range.HDR10P) == "HDR10P"


def test_an_hdr10_track_keeps_the_hdr_tag() -> None:
    assert hdr_tag("HDR10", Video.Range.HDR10) == "HDR"


def test_mediainfo_hdr10p_is_unchanged() -> None:
    assert hdr_tag("HDR10 / HDR10+", Video.Range.HDR10) == "HDR10P"
