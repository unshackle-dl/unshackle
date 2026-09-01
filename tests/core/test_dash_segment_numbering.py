"""Segment numbering must start at ``startNumber`` and cover every segment, in both
``SegmentTemplate`` forms: an explicit ``SegmentTimeline``, and a fixed segment duration."""

from unittest.mock import MagicMock

import pytest

from unshackle.core.manifests.dash import DASH
from unshackle.core.utils.xml import load_xml

TIMELINE_MPD = """
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
  <Period id="0" start="PT0S">
    <AdaptationSet mimeType="video/mp4">
      <SegmentTemplate timescale="1" media="v/$Number$.mp4" startNumber="{start}">
        <SegmentTimeline><S t="0" d="1" r="{repeat}"/></SegmentTimeline>
      </SegmentTemplate>
      <Representation id="v" bandwidth="1"/>
    </AdaptationSet>
  </Period>
</MPD>
"""

# github.com/unshackle-dl/unshackle/issues/37: 96.72s at 6s per segment is 17 segments, 0 to 16, never a segment 17.
DURATION_MPD = """
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT{seconds}S">
  <Period id="0" start="PT0S" duration="PT{seconds}S">
    <AdaptationSet mimeType="video/mp4">
      <SegmentTemplate timescale="25" duration="150" media="v/$Number$.mp4" startNumber="{start}"/>
      <Representation id="v" bandwidth="1"/>
    </AdaptationSet>
  </Period>
</MPD>
"""


def _numbers(mpd: str) -> list[int]:
    tree = load_xml(mpd.encode())
    period = tree.find("Period")
    aset = period.find("AdaptationSet")
    rep = aset.find("Representation")
    _, segments, _, _, _ = DASH.get_period_segments(
        period, aset, rep, tree, MagicMock(), "https://example.com/main.mpd", MagicMock()
    )
    return [int(url.rsplit("/", 1)[1].split(".")[0]) for url, _ in segments]


@pytest.mark.parametrize(
    ("start", "count"),
    [
        (0, 5),  # zero-based numbering
        (1, 5),  # plain VOD
        (2642, 4667),  # DVR catch-up: startNumber below segment count
        (5000, 10),  # DVR catch-up: startNumber above segment count
    ],
)
def test_timeline_numbers_follow_start_number(start: int, count: int) -> None:
    assert _numbers(TIMELINE_MPD.format(start=start, repeat=count - 1)) == list(range(start, start + count))


@pytest.mark.parametrize(("start", "seconds", "count"), [(0, "1M36.720", 17), (1, "1M36.720", 17), (2642, "30", 5)])
def test_duration_numbers_follow_start_number(start: int, seconds: str, count: int) -> None:
    assert _numbers(DURATION_MPD.format(start=start, seconds=seconds)) == list(range(start, start + count))
