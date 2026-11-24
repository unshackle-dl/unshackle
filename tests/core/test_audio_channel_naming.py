"""Audio codec and channel layout in a filename."""

from __future__ import annotations

import types

import pytest

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
