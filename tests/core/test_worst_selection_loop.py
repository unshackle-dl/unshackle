"""Run --worst through the real dl.result() selection loop."""

from __future__ import annotations

from typing import Any

import pytest

from tests.lang_selection import test_kind_only_selection as harness
from unshackle.core.titles import Episode
from unshackle.core.tracks import Tracks, Video

LADDER = [
    ("1080-hi", Video.Range.SDR, 1920, 1080, 6_000_000),
    ("1080-lo", Video.Range.SDR, 1920, 1080, 3_000_000),
    ("1080-unknown", Video.Range.SDR, 1920, 1080, None),
    ("540", Video.Range.SDR, 960, 540, 900_000),
    ("270-lo", Video.Range.SDR, 480, 270, 79_000),
    ("240-hi", Video.Range.SDR, 320, 240, 153_000),
    ("240-lo", Video.Range.SDR, 320, 240, 140_000),
    ("240-unknown", Video.Range.SDR, 320, 240, None),
]


class LadderService(harness.FakeService):
    """The harness episode with a video ladder of one codec; a track with no bitrate is never the worst."""

    def __init__(self, ladder: list[tuple[Any, ...]] = LADDER, codec: Video.Codec = Video.Codec.AVC) -> None:
        super().__init__({1: {}})
        self.ladder, self.codec = ladder, codec

    def get_tracks(self, title: Episode) -> Tracks:
        tracks = super().get_tracks(title)
        tracks.videos = [
            Video(
                id_=id_,
                url=f"https://x/{id_}",
                language="en",
                codec=self.codec,
                range_=range_,
                width=width,
                height=height,
                bitrate=bitrate,
            )
            for id_, range_, width, height, bitrate in self.ladder
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


HDR_LADDER = [
    ("hdr-2160", Video.Range.HDR10, 3840, 2160, 15_000_000),
    ("hdr-1080-hi", Video.Range.HDR10, 1920, 1080, 6_000_000),
    ("hdr-1080-lo", Video.Range.HDR10, 1920, 1080, 4_000_000),
    ("hdr-1080-unknown", Video.Range.HDR10, 1920, 1080, None),
    ("dv-1080", Video.Range.DV, 1920, 1080, 5_000_000),
    ("dv-540", Video.Range.DV, 960, 540, 1_000_000),
]


@pytest.mark.parametrize(
    "flags",
    [{}, {"quality": [1080]}, {"quality": [4320], "best_available": True}],
    ids=["no-quality", "quality", "best-available"],
)
def test_worst_in_hybrid_mode_takes_the_lowest_bitrate_base_of_the_lowest_base_height(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, flags: dict[str, Any]
) -> None:
    harness.stub_hybrid(monkeypatch)
    service = LadderService(HDR_LADDER, Video.Codec.HEVC)
    ids = harness.run(monkeypatch, caplog, {1: {}}, service=service, worst=True, range_=[Video.Range.HYBRID], **flags)
    assert ids["video"] == ["hdr-1080-lo", "dv-540"]
