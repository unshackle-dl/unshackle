"""Offline unit tests for DASH.collapsible_single_url, the guard that decides when a
byte-range track (SegmentBase / ranged SegmentList) can be downloaded as one whole
resource instead of per-segment files + merge."""

from __future__ import annotations

from typing import Optional

from unshackle.core.manifests import DASH

URL = "https://cdn/media.mp4"


def collapsible(segments, init_len: Optional[int], is_subtitle: bool = False) -> bool:
    return DASH.collapsible_single_url(is_subtitle, segments, init_len)


def test_segmentbase_single_range_eligible():
    # init occupies [0, 863); one media segment covers the rest of the same URL
    assert collapsible([(URL, "863-999999")], init_len=863) is True


def test_ranged_segmentlist_contiguous_eligible():
    segments = [(URL, "863-1999"), (URL, "2000-3999"), (URL, "4000-5999")]
    assert collapsible(segments, init_len=863) is True


def test_subtitle_never_eligible():
    assert collapsible([(URL, "863-999999")], init_len=863, is_subtitle=True) is False


def test_missing_init_len_ineligible():
    assert collapsible([(URL, "863-999999")], init_len=None) is False


def test_missing_range_ineligible():
    assert collapsible([(URL, None)], init_len=863) is False
    assert collapsible([(URL, "863-1999"), (URL, None)], init_len=863) is False


def test_mixed_urls_ineligible():
    segments = [(URL, "863-1999"), ("https://cdn/other.mp4", "2000-3999")]
    assert collapsible(segments, init_len=863) is False


def test_first_segment_gap_after_init_ineligible():
    # first media byte does not immediately follow the init prefix
    assert collapsible([(URL, "900-1999")], init_len=863) is False


def test_overlapping_ranges_ineligible():
    segments = [(URL, "863-2500"), (URL, "2000-3999")]
    assert collapsible(segments, init_len=863) is False


def test_non_monotonic_order_ineligible():
    segments = [(URL, "4000-5999"), (URL, "863-1999")]
    assert collapsible(segments, init_len=863) is False


def test_malformed_range_ineligible():
    assert collapsible([(URL, "863")], init_len=863) is False
    assert collapsible([(URL, "abc-def")], init_len=863) is False
    assert collapsible([(URL, "1999-863")], init_len=863) is False


def test_empty_segments_ineligible():
    assert collapsible([], init_len=863) is False
