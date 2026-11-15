"""Run --worst through the real dl.result() selection loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.lang_selection import test_kind_only_selection as harness
from unshackle.core import binaries
from unshackle.core.titles import Episode
from unshackle.core.tracks import Tracks, Video

LADDER = [
    ("1080-hi", 1920, 1080, 6_000_000),
    ("1080-lo", 1920, 1080, 3_000_000),
    ("1080-unknown", 1920, 1080, None),
    ("540", 960, 540, 900_000),
    ("270-lo", 480, 270, 79_000),
    ("240-hi", 320, 240, 153_000),
    ("240-lo", 320, 240, 140_000),
    ("240-unknown", 320, 240, None),
]


class LadderService(harness.FakeService):
    """The harness episode with the video ladder above; a track with no bitrate is never the worst."""

    def __init__(self) -> None:
        super().__init__({1: {}})

    def get_tracks(self, title: Episode) -> Tracks:
        tracks = super().get_tracks(title)
        tracks.videos = [
            Video(
                id_=id_,
                url=f"https://x/{id_}",
                language="en",
                codec=Video.Codec.AVC,
                range_=Video.Range.SDR,
                width=width,
                height=height,
                bitrate=bitrate,
            )
            for id_, width, height, bitrate in LADDER
        ]
        return tracks


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({}, ["240-lo"]),
        ({"quality": [1080]}, ["1080-lo"]),
        ({"quality": [1080, 540]}, ["1080-lo", "540"]),
        ({"quality": [2160], "best_available": True}, ["240-lo"]),
    ],
)
def test_worst_picks_the_lowest_bitrate_of_the_quality_or_of_the_lowest_resolution(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, flags: dict[str, Any], expected: list[str]
) -> None:
    ids = harness.run(monkeypatch, caplog, {1: {}}, service=LadderService(), worst=True, **flags)
    assert ids["video"] == expected


def test_worst_in_hybrid_mode_needs_a_quality(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(binaries, "DoviTool", Path("dovi_tool"))
    code = harness.run(monkeypatch, caplog, {1: {}}, service=LadderService(), worst=True, range_=[Video.Range.HYBRID])
    assert code == 1
    assert "--worst requires -q/--quality to be specified in hybrid mode" in caplog.text
