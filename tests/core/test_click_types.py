"""Tests for SubtitleCodecChoice — notably the ``original`` keep-source sentinel that
services set via the ``sub_format`` override (must not be rejected as an invalid codec)."""

from __future__ import annotations

import click
import pytest

from unshackle.core.tracks.subtitle import Subtitle
from unshackle.core.utils.click_types import QUALITY_LIST, SLOW_DELAY_RANGE, SeasonRange, SubtitleCodecChoice

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


# --- SeasonRange ------------------------------------------------------------


def parse(*tokens: str) -> list[str]:
    return SeasonRange().parse_tokens(*tokens)


def test_single_episode_token_is_unchanged():
    assert parse("S01E01") == ["1x1"]


def test_bare_season_spans_the_whole_episode_range():
    """A bare S01 reads the one match object with a different default per side."""
    keys = parse("S01")
    assert len(keys) == 1000
    assert "1x0" in keys and "1x999" in keys


def test_multi_token_range_is_unchanged():
    assert sorted(parse("S02E01", "S02E03-S02E05")) == ["2x1", "2x3", "2x4", "2x5"]


def test_exclusions_are_unchanged():
    keys = parse("S01-S05", "-S03", "-S02E01")
    assert not any(k.startswith("3x") for k in keys)
    assert "2x1" not in keys
    assert "2x0" in keys and "2x2" in keys
    assert len(keys) == 3999  # 5 seasons less all of S03 less 2x1


@pytest.mark.parametrize("tokens", [("S01E01",), ("S01",), ("S01-S05", "-S03", "-S02E01"), ("S02E03-S02E05",)])
def test_part_less_tokens_never_emit_a_part_or_exclusion_key(tokens):
    assert all("." not in k and "!" not in k for k in parse(*tokens))


def test_part_token():
    assert parse("S01E01.2") == ["1x1.2"]


def test_part_range_within_one_episode():
    assert sorted(parse("S01E01.1-S01E01.3")) == ["1x1.1", "1x1.2", "1x1.3"]


def test_part_exclusion_becomes_a_negative_key():
    keys = parse("S01", "-S01E01.2")
    assert "!1x1.2" in keys
    assert "1x1" in keys  # base key stays so the other parts still match


def test_base_exclusion_covers_part_keys():
    assert parse("S01E01.1-S01E01.3", "S01E02", "-S01E01") == ["1x2"]


def test_exclusion_removes_duplicate_keys():
    # "S01" and "S01E01" both emit 1x1; the exclusion must remove every copy
    assert "1x1" not in parse("S01", "S01E01", "-S01E01")


@pytest.mark.parametrize(
    "token",
    [
        "S01E01.0",  # parts count from 1
        "S01E01.1-S01E02.2",  # a part range cannot cross episodes
        "S01-S01E01.2",  # a part on only one side
        "S01E01.3-S01E01.1",  # reversed part range
        "S01E01.200",  # above MAX_PART
    ],
)
def test_bad_part_tokens_fail(token):
    with pytest.raises(click.UsageError):
        parse(token)
