"""Audio codec and channel layout in a filename."""

from __future__ import annotations

import types

import pytest

from unshackle.core.config import config
from unshackle.core.titles.movie import Movie


class DummyService:
    pass


def named(fmt: str, layout: str | None, channel_s: int = 6) -> dict:
    """Build the naming context from a MediaInfo audio track alone."""
    audio = types.SimpleNamespace(
        language="en",
        format=fmt,
        channel_layout=layout,
        channellayout_original=None,
        channel_s=channel_s,
        channels=channel_s,
        format_additionalfeatures=None,
        joc=None,
    )
    movie = Movie(id_="movie-0001", service=DummyService, name="The Film", year=2024, language="en")
    return movie.build_base_template_context(types.SimpleNamespace(video_tracks=[], audio_tracks=[audio]))


def test_a_height_layout_is_named_bed_lfe_heights() -> None:
    """
    DTS:X and Atmos carry height channels. Folding them into the bed gives 9.1, which names
    nine ear-level speakers: a different layout from five plus four overhead.
    """
    context = named("DTS-UHD", "L C R LFE Ls Rs Tfl Tfr Tbl Tbr", channel_s=10)
    assert context["audio_channels"] == "5.1.4"
    assert context["audio"] == "DTS-X", "the filename carries the brand, not the bitstream name"
    assert context["audio_full"] == "DTS-X 5.1.4"


@pytest.mark.parametrize(
    ("layout", "expected"),
    [
        ("L R C LFE Ls Rs", "5.1"),
        ("L R C LFE Ls Rs Lb Rb", "7.1"),
        ("L R", "2.0"),
        (None, "6.0"),
    ],
)
def test_layouts_without_heights_are_unchanged(layout: str | None, expected: str) -> None:
    assert named("E-AC-3", layout)["audio_channels"] == expected


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [("E-AC-3", "DDP5.1"), ("AC-3", "DD5.1"), ("AC-4", "AC-4 5.1"), ("AAC", "AAC5.1")],
)
def test_a_name_ending_in_a_digit_is_spaced_from_the_channels(fmt: str, expected: str) -> None:
    assert named(fmt, "L R C LFE Ls Rs")["audio_full"] == expected


def test_codec_names_overrides_the_built_in_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The override is keyed by the name the filename would carry, and AC3 still gets its separator."""
    monkeypatch.setattr(config, "codec_names", {"DDP": "EAC3", "DD": "AC3"})
    assert named("E-AC-3", "L R C LFE Ls Rs")["audio_full"] == "EAC3 5.1"
    assert named("AC-3", "L R C LFE Ls Rs")["audio"] == "AC3"


def test_codec_names_renames_video_and_each_hdr_part(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "codec_names", {"H.265": "HEVC", "HDR10P": "HDR10+", "DTS-X": "DTSX"})
    video = types.SimpleNamespace(
        width=3840,
        height=2160,
        format="HEVC",
        scan_type="Progressive",
        other_display_aspect_ratio=["16:9"],
        hdr_format_commercial="HDR10+",
        hdr_format="Dolby Vision, Version 1.0, Profile 8.1",
        transfer_characteristics=None,
        transfer_characteristics_original=None,
        frame_rate="23.976",
    )
    movie = Movie(id_="movie-0001", service=DummyService, name="The Film", year=2024, language="en")
    context = movie.build_base_template_context(types.SimpleNamespace(video_tracks=[video], audio_tracks=[]))
    assert (context["video"], context["hdr"]) == ("HEVC", "DV.HDR10+")
    assert named("DTS-UHD", "L C R LFE Ls Rs")["audio_full"] == "DTSX 5.1", "a renamed DTS-X keeps its separator"


def test_a_missing_audio_format_does_not_break_naming() -> None:
    assert named(None, "L R", channel_s=2)["audio_full"] == "2.0"  # type: ignore[arg-type]
