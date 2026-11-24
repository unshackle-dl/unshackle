"""Channel values that no float can hold.

An immersive layout names its height channels in a third figure, e.g. 5.1.4. A service
assigning that straight onto a track keeps it out of validation, but a remote download
sends the track as JSON and rebuilds it through Audio(), which parses the value.
"""

from __future__ import annotations

import pytest

from unshackle.core.tracks import Audio


@pytest.mark.parametrize("layout", ["5.1.4", "7.1.2", "9.1.6"])
def test_an_immersive_layout_survives_construction(layout: str) -> None:
    assert Audio(id_="a1", url="https://example.test/a", language="en", channels=layout).channels == layout


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5.1", 5.1), ("2ch", 2.0), ("2", 2.0), ("2.0", 2.0), ("A000", 2.0), ("F801", 5.1), (6, 6.0)],
)
def test_existing_channel_forms_are_unchanged(value: object, expected: float) -> None:
    assert Audio.parse_channels(value) == expected


@pytest.mark.parametrize("value", ["stereo", "5.1.4.2", "", "..", "5.x.4"])
def test_a_value_that_is_no_layout_still_raises(value: str) -> None:
    with pytest.raises(NotImplementedError):
        Audio.parse_channels(value)


def test_an_immersive_layout_round_trips_as_a_dict() -> None:
    """A remote download rebuilds every track from the dict the server sent."""
    track = Audio(id_="a1", url="https://example.test/a", language="en", channels="5.1.4", bitrate=448000)
    assert Audio.from_dict(track.to_dict()).channels == "5.1.4"


@pytest.mark.parametrize(
    ("channels", "total"),
    [("5.1.4", 10.0), ("7.1.2", 10.0), (5.1, 5.1), (6.0, 6.0), ("2.0", 2.0)],
)
def test_channel_total_compares_both_forms(channels: object, total: float) -> None:
    """--channels rounds the sub channel up, so both forms have to reach that arithmetic."""
    assert Audio.channel_total(channels) == total
