"""Tests for SubtitleCodecChoice — notably the ``original`` keep-source sentinel that
services set via the ``sub_format`` override (must not be rejected as an invalid codec)."""

from __future__ import annotations

import pytest

from unshackle.core.tracks.subtitle import Subtitle
from unshackle.core.utils.click_types import QUALITY_LIST, SLOW_DELAY_RANGE, SubtitleCodecChoice

choice = SubtitleCodecChoice(Subtitle.Codec)


@pytest.mark.parametrize("value", ["original", "ORIGINAL", "Original"])
def test_original_is_kept_as_sentinel(value):
    assert choice.convert(value) == "original"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("srt", Subtitle.Codec.SubRip),
        ("ass", Subtitle.Codec.SubStationAlphav4),
        ("vtt", Subtitle.Codec.WebVTT),
        ("WVTT", Subtitle.Codec.fVTT),
    ],
)
def test_codecs_still_map(value, expected):
    assert choice.convert(value) == expected


def test_empty_is_none():
    assert choice.convert(None) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (1080, [1080]),
        ([720, 1080], [1080, 720]),
        ("1080p", [1080]),
        ("720,1080", [1080, 720]),
    ],
)
def test_quality_list_accepts_yaml_native_values(value, expected):
    assert QUALITY_LIST.convert(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, (60, 120)),
        (False, None),
        ("20-40", (20, 40)),
        ((25, 30), (25, 30)),
    ],
)
def test_slow_delay_range_accepts_bool(value, expected):
    assert SLOW_DELAY_RANGE.convert(value, None, None) == expected
