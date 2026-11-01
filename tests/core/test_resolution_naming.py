"""Tests for the standard resolution in the filename context, the export name and -q matching."""

from __future__ import annotations

import types
from typing import Optional

import pytest

from unshackle.core.config import config
from unshackle.core.export_name import resolution
from unshackle.core.titles.movie import Movie
from unshackle.core.tracks import Video
from unshackle.core.tracks.tracks import at_resolution
from unshackle.core.utilities import standard_resolution


class DummyService:
    pass


@pytest.fixture(autouse=True)
def clean_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "tag_rules", [])
    monkeypatch.setattr(config, "language_tags", {})
    monkeypatch.setattr(config, "codec_names", {})


def quality(width: Optional[int], height: Optional[int], scan_type: str = "Progressive") -> str:
    movie = Movie(id_="movie-0001", service=DummyService, name="The Film", year=2024, language="en")
    video = types.SimpleNamespace(
        width=width,
        height=height,
        format="AVC",
        scan_type=scan_type,
        hdr_format_commercial=None,
        hdr_format=None,
        transfer_characteristics="BT.709",
        transfer_characteristics_original=None,
        frame_rate="25.000",
    )
    media_info = types.SimpleNamespace(video_tracks=[video], audio_tracks=[])
    return str(movie.build_base_template_context(media_info)["quality"])


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (3840, 2160, 2160),
        (1920, 1080, 1080),
        (1280, 720, 720),
        (2560, 1440, 1440),
        (1920, 1088, 1080),  # padded to a multiple of 16
        (960, 544, 540),
        (640, 368, 360),
        (1916, 1076, 1080),  # a few lines short
        (3836, 2156, 2160),
        (1920, 800, 1080),  # scope crop
        (1920, 1036, 1080),  # flat crop
        (3840, 1600, 2160),
        (1280, 544, 720),
        (4096, 2160, 2160),  # DCI
        (4096, 1716, 2160),
        (2048, 858, 1080),
        (1440, 1080, 1080),  # 4:3 pillarbox
        (960, 720, 720),
        (720, 576, 576),  # anamorphic SD
        (704, 576, 576),
        (720, 480, 480),
        (854, 480, 480),
        (1080, 1920, 1080),  # portrait
        (1080, 1350, 1080),
        (720, 1280, 720),
        (2560, 1080, 1080),  # ultrawide
        (3440, 1440, 1440),
        (1600, 900, 900),  # no standard height near it
    ],
)
def test_standard_resolution(width: int, height: int, expected: int) -> None:
    assert standard_resolution(width, height) == expected


def test_a_picture_with_one_side_uses_it() -> None:
    assert standard_resolution(None, 1088) == 1080


def test_a_picture_with_no_size_has_no_resolution() -> None:
    assert standard_resolution(None, None) is None


def test_the_filename_quality_uses_the_standard_resolution() -> None:
    assert quality(1920, 1088) == "1080p"
    assert quality(1920, 800) == "1080p"


def test_an_interlaced_picture_keeps_the_i_suffix() -> None:
    assert quality(1920, 1088, scan_type="Interlaced") == "1080i"


def test_a_video_track_with_no_size_gives_an_empty_quality() -> None:
    assert quality(None, None) == ""


def video(width: int, height: int) -> Video:
    return Video(url="https://example.test/v.mpd", language="en", codec=Video.Codec.AVC, width=width, height=height)


def test_the_export_name_uses_the_standard_resolution() -> None:
    assert resolution(video(1080, 1920)) == 1080
    assert resolution(video(4096, 2160)) == 2160


@pytest.mark.parametrize(("width", "height"), [(1920, 1080), (1920, 800), (1916, 1076), (1920, 1088)])
def test_q_1080_matches_odd_1080p_encodes(width: int, height: int) -> None:
    assert at_resolution(video(width, height), 1080)


def test_q_1080_does_not_match_720p() -> None:
    assert not at_resolution(video(1280, 720), 1080)
