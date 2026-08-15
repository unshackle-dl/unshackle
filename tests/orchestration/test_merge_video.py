"""Tests for ``--merge-video`` track grouping.

``group_videos_by_variant`` (``unshackle/commands/dl.py``) decides which selected video
tracks share one MKV when merge mode is on. The rule: group by ``(resolution, range,
codec)`` so only language varies within a file; resolutions, ranges and codecs stay
separate. With ``merge=False`` every track is its own group (one file per track).

These lock the pure grouping unit; the surrounding mux loop is Click-command orchestration.
"""

from __future__ import annotations

from unshackle.commands.dl import group_videos_by_variant
from unshackle.core.tracks import Video


def make_video(
    track_id: str,
    *,
    range_: Video.Range,
    height: int,
    codec: Video.Codec,
    language: str = "en",
) -> Video:
    return Video(
        id_=track_id,
        url=f"https://example.test/{track_id}.m3u8",
        language=language,
        codec=codec,
        range_=range_,
        width=int(height * 16 / 9),
        height=height,
        bitrate=1_000_000,
    )


HEVC = Video.Codec.HEVC
AVC = Video.Codec.AVC
SDR = Video.Range.SDR
HDR10 = Video.Range.HDR10
DV = Video.Range.DV


def test_merge_collapses_language_only() -> None:
    """Same (height, range, codec), different language → one group."""
    videos = [
        make_video("en", range_=SDR, height=1080, codec=HEVC, language="en"),
        make_video("fr", range_=SDR, height=1080, codec=HEVC, language="fr"),
    ]
    groups = group_videos_by_variant(videos, merge=True)
    assert len(groups) == 1
    assert [v.id for v in groups[0]] == ["en", "fr"]


def test_merge_splits_on_codec() -> None:
    """H264 vs H265 of the same resolution+range → separate groups."""
    videos = [
        make_video("hevc", range_=SDR, height=1080, codec=HEVC),
        make_video("avc", range_=SDR, height=1080, codec=AVC),
    ]
    groups = group_videos_by_variant(videos, merge=True)
    assert len(groups) == 2
    assert all(len(g) == 1 for g in groups)


def test_merge_splits_on_range() -> None:
    """SDR vs HDR10 of the same resolution+codec → separate groups."""
    videos = [
        make_video("sdr", range_=SDR, height=1080, codec=HEVC),
        make_video("hdr10", range_=HDR10, height=1080, codec=HEVC),
    ]
    groups = group_videos_by_variant(videos, merge=True)
    assert len(groups) == 2


def test_merge_splits_on_resolution() -> None:
    """1080p vs 2160p of the same range+codec → separate groups."""
    videos = [
        make_video("1080", range_=SDR, height=1080, codec=HEVC),
        make_video("2160", range_=SDR, height=2160, codec=HEVC),
    ]
    groups = group_videos_by_variant(videos, merge=True)
    assert len(groups) == 2


def test_merge_multi_range_yields_one_group_per_range() -> None:
    """Regression guard: -r HYBRID,DV,HDR10,SDR must never collapse into one file.

    HYBRID is resolved upstream into a DV deliverable plus the requested standalone
    ranges; here the four selected single-range tracks must stay in four groups.
    """
    videos = [
        make_video("sdr", range_=SDR, height=2160, codec=HEVC),
        make_video("hdr10", range_=HDR10, height=2160, codec=HEVC),
        make_video("dv", range_=DV, height=2160, codec=HEVC),
        make_video("dv-hybrid", range_=DV, height=1080, codec=HEVC),  # different height
    ]
    groups = group_videos_by_variant(videos, merge=True)
    assert len(groups) == 4


def test_no_merge_yields_one_group_per_track() -> None:
    """merge=False reproduces today's per-track behaviour exactly."""
    videos = [
        make_video("en", range_=SDR, height=1080, codec=HEVC, language="en"),
        make_video("fr", range_=SDR, height=1080, codec=HEVC, language="fr"),
        make_video("avc", range_=SDR, height=1080, codec=AVC),
    ]
    groups = group_videos_by_variant(videos, merge=False)
    assert len(groups) == 3
    assert all(len(g) == 1 for g in groups)


def test_merge_preserves_first_seen_order() -> None:
    """Group order follows first-seen track order, for stable output filenames."""
    videos = [
        make_video("hevc-en", range_=SDR, height=1080, codec=HEVC, language="en"),
        make_video("avc-en", range_=SDR, height=1080, codec=AVC, language="en"),
        make_video("hevc-fr", range_=SDR, height=1080, codec=HEVC, language="fr"),
    ]
    groups = group_videos_by_variant(videos, merge=True)
    # HEVC group seen first (and gathers both languages), AVC group second.
    assert [v.id for v in groups[0]] == ["hevc-en", "hevc-fr"]
    assert [v.id for v in groups[1]] == ["avc-en"]


def test_empty_input_returns_empty() -> None:
    assert group_videos_by_variant([], merge=True) == []
    assert group_videos_by_variant([], merge=False) == []
