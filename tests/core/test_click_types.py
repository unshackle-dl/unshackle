"""Tests for SubtitleCodecChoice, notably the ``original`` keep-source sentinel that
services set via the ``sub_format`` override (must not be rejected as an invalid codec)."""

from __future__ import annotations

import click
import pytest

from unshackle.core.tracks.subtitle import Subtitle
from unshackle.core.tracks.video import Video
from unshackle.core.utils.click_types import (
    AUDIO_CODEC_LIST,
    LANGUAGE_RANGE,
    QUALITY_LIST,
    SLOW_DELAY_RANGE,
    SUBTITLE_CODEC,
    VIDEO_CODEC_LIST,
    MultipleChoice,
    SeasonRange,
    SubtitleCodecChoice,
)

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


@pytest.mark.parametrize(
    "value,expected",
    [
        ("all,-es", ["all", "-es"]),
        ("-es", ["-es"]),
        ("all, -es ; -fr", ["all", "-es", "-fr"]),
        (["all", "-es"], ["all", "-es"]),
    ],
)
def test_language_range_passes_exclusion_tokens_through(value, expected):
    """The '-' prefix is resolved at filter time, so the type must keep the token intact."""
    assert LANGUAGE_RANGE.convert(value) == expected


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


def test_single_date_token_is_its_own_key():
    assert parse("2026-08-11") == ["2026-08-11"]


def test_date_range_expands_every_day_inclusive():
    assert sorted(parse("2026-08-01:2026-08-03")) == ["2026-08-01", "2026-08-02", "2026-08-03"]


def test_date_range_crosses_a_month():
    assert sorted(parse("2026-01-30:2026-02-02")) == ["2026-01-30", "2026-01-31", "2026-02-01", "2026-02-02"]


def test_date_exclusion_removes_the_day():
    assert sorted(parse("2026-08-01:2026-08-03", "-2026-08-02")) == ["2026-08-01", "2026-08-03"]


def test_dates_mix_with_season_episode_tokens():
    assert sorted(parse("S01E01", "2026-08-11")) == ["1x1", "2026-08-11"]


def test_comma_separated_dates_convert():
    assert sorted(SeasonRange().convert("2026-08-11, 2026-08-12")) == ["2026-08-11", "2026-08-12"]


@pytest.mark.parametrize(
    "token",
    [
        "2026-13-01",  # not a real month
        "2026-02-30",  # not a real day
        "2026-08-03:2026-08-01",  # reversed range
        "2020-01-01:2026-01-01",  # over MAX_DATE_SPAN
    ],
)
def test_bad_date_tokens_fail(token):
    with pytest.raises(click.UsageError):
        parse(token)


def test_audio_metavar_lists_every_accepted_spelling():
    """--help must not advertise fewer codecs than convert() takes, aliases included."""
    metavar = AUDIO_CODEC_LIST.get_metavar()
    assert metavar.startswith("[") and metavar.endswith("]")
    listed = metavar[1:-1].split("|")
    assert listed == AUDIO_CODEC_LIST.choices
    for spelling in ("ddp", "eac3", "vorbis", "ac-4", "vorb"):
        assert spelling in listed
        assert AUDIO_CODEC_LIST.convert(spelling)


@pytest.mark.parametrize("param_type", [AUDIO_CODEC_LIST, VIDEO_CODEC_LIST, SUBTITLE_CODEC])
def test_no_duplicate_choices(param_type):
    """A codec whose name and value are the same word (VP9, AV1) is listed once."""
    lowered = [choice.lower() for choice in param_type.choices]
    assert len(lowered) == len(set(lowered))


def test_api_codec_lists_come_from_the_param_types():
    from unshackle.core.api.handlers import VALID_ACODECS, VALID_SUB_FORMATS, VALID_VCODECS

    assert VALID_ACODECS == [c.upper() for c in AUDIO_CODEC_LIST.choices]
    assert VALID_VCODECS == [c.upper() for c in VIDEO_CODEC_LIST.choices]
    assert VALID_SUB_FORMATS == [c.upper() for c in SUBTITLE_CODEC.choices]


@pytest.mark.parametrize("value", ["avc,,hevc", ["avc", "", "hevc"], " avc , hevc "])
def test_empty_video_codec_tokens_are_skipped(value):
    """A None in the list reaches track filtering and crashes it, so blanks must drop out."""
    assert VIDEO_CODEC_LIST.convert(value) == [Video.Codec.AVC, Video.Codec.HEVC]


@pytest.mark.parametrize("spelling", ["H264", "h265", "H.265", "hevc"])
def test_video_codec_aliases_resolve(spelling):
    """The API advertised H264/H265 while dropping them; both paths now resolve the same."""
    assert VIDEO_CODEC_LIST.convert(spelling)[0] in (Video.Codec.AVC, Video.Codec.HEVC)


def test_audio_codec_list_shell_complete():
    """--acodec offered no completion at all; it now completes the segment after the last comma."""
    assert [c.value for c in AUDIO_CODEC_LIST.shell_complete(None, None, "fl")] == ["flac"]
    assert [c.value for c in AUDIO_CODEC_LIST.shell_complete(None, None, "AAC,e")] == ["AAC,ec3", "AAC,eac3"]
    assert AUDIO_CODEC_LIST.shell_complete(None, None, "zzz") == []


def test_multiple_choice_shell_complete():
    """super(self) raised TypeError, so every --range completion crashed instead of completing."""
    range_choice = MultipleChoice(Video.Range, case_sensitive=False)
    assert [c.value for c in range_choice.shell_complete(None, None, "hd")] == ["hdr10", "hdr10p"]
    assert [c.value for c in range_choice.shell_complete(None, None, "sdr,hd")] == ["sdr,hdr10", "sdr,hdr10p"]
    assert range_choice.shell_complete(None, None, "zzz") == []
