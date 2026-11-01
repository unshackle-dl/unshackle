"""Tests for the HDR tag in the filename context when MediaInfo misses HDR10+ or the HDR format."""

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


def hdr_tag(
    commercial: str, range_: Video.Range, transfer: str = "PQ", hdr_format: str = "SMPTE ST 2086", profile: str = ""
) -> str:
    movie = Movie(id_="movie-0001", service=DummyService, name="The Film", year=2024, language="en")
    movie.tracks = Tracks(Video(url="https://example.test/v.mpd", language="en", codec=Video.Codec.AV1, range_=range_))
    video = types.SimpleNamespace(
        width=1920,
        height=1080,
        format="AV1",
        scan_type="Progressive",
        hdr_format_commercial=commercial,
        hdr_format=hdr_format,
        hdr_format_profile=profile,
        transfer_characteristics=transfer,
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


def test_pq_without_an_hdr_format_gets_the_hdr_tag() -> None:
    assert hdr_tag("", Video.Range.HDR10) == "HDR"


def test_hlg_without_an_hdr_format_keeps_the_hlg_tag() -> None:
    assert hdr_tag("", Video.Range.HLG, transfer="HLG") == "HLG"


def test_sdr_gets_no_hdr_tag() -> None:
    assert hdr_tag("", Video.Range.SDR, transfer="BT.709") == ""


def test_a_profile_8_dv_track_with_an_unnamed_pq_base_gets_dv_hdr() -> None:
    assert hdr_tag("Dolby Vision", Video.Range.DV, hdr_format="Dolby Vision", profile="dvhe.08") == "DV.HDR"


def test_a_profile_5_dv_track_stays_dv() -> None:
    assert hdr_tag("Dolby Vision", Video.Range.DV, hdr_format="Dolby Vision", profile="dvhe.05") == "DV"


def test_a_profile_8_4_dv_track_with_an_hlg_base_stays_dv() -> None:
    assert hdr_tag("Dolby Vision", Video.Range.DV, transfer="HLG", hdr_format="Dolby Vision", profile="dvhe.08") == "DV"


def test_a_dv_track_with_a_named_hdr10_base_keeps_dv_hdr() -> None:
    assert hdr_tag("HDR10", Video.Range.DV, hdr_format="Dolby Vision", profile="dvhe.08") == "DV.HDR"
