"""Unit tests for the aggregate per-job download progress sink.

``build_job_progress_callables`` wraps the per-track progress callables so the API job sees one
aggregate signal - a bitrate-weighted completion percentage, track counts, and the labels of the
tracks downloading now - instead of each track's own bouncing 0-100%. These tests pin that
contract."""

from __future__ import annotations

import pytest

from unshackle.core.api.progress import (DOWNLOAD_PROGRESS_CEILING, build_job_progress_callables,
                                         track_progress_label, track_progress_weight)

pytestmark = pytest.mark.unit


# --- lightweight track stand-ins (label/weight key off class name + attributes) ---
class _Range:
    def __init__(self, value):
        self.value = value


class Video:
    def __init__(self, height=1080, range_value="SDR", bitrate=4_000_000):
        self.height = height
        self.range = _Range(range_value)
        self.bitrate = bitrate


class Audio:
    def __init__(self, language="en-US", channels="2.0", bitrate=200_000):
        self.language = language
        self.channels = channels
        self.bitrate = bitrate


class Subtitle:
    def __init__(self, language="fr"):
        self.language = language
        self.bitrate = None


def _noop(**kwargs):
    pass


def test_track_progress_label():
    assert track_progress_label(Video(2160, "DV")) == "video 2160p DV"
    assert track_progress_label(Video(1080, "HDR10+")) == "video 1080p HDR10+"
    assert track_progress_label(Audio("en-US", "5.1")) == "audio en-US 5.1"
    assert track_progress_label(Subtitle("ro")) == "subtitle ro"


def test_weight_video_over_audio_over_subtitle():
    assert track_progress_weight(Video(bitrate=4_000_000)) == 4_000_000
    assert track_progress_weight(Audio(bitrate=200_000)) == 200_000
    # subtitle has no bitrate -> small fixed weight, far below media
    assert track_progress_weight(Subtitle()) < track_progress_weight(Audio(bitrate=200_000))


def test_weighting_makes_video_dominate_progress():
    updates: list[dict] = []
    video, sub = Video(bitrate=4_000_000), Subtitle()
    cbs = build_job_progress_callables([video, sub], [_noop, _noop], updates.append)

    # subtitle fully done, video untouched -> progress is tiny (subtitle barely weighted)
    cbs[1](downloaded="Downloaded")
    assert updates[-1]["completed_tracks"] == 1
    assert updates[-1]["progress"] < 5.0

    # video half done -> progress is dominated by video (scaled into the 0..ceiling download band)
    cbs[0](total=100, completed=50)
    assert updates[-1]["progress"] > 40.0


def test_active_tracks_labels_reported_and_cleared_on_done():
    updates: list[dict] = []
    cbs = build_job_progress_callables(
        [Video(2160, "DV"), Audio("en-US", "2.0")], [_noop, _noop], updates.append
    )

    cbs[0](total=100, completed=10)  # video downloading
    assert updates[-1]["active_tracks"] == ["video 2160p DV"]
    assert updates[-1]["phase"] == "downloading video 2160p DV"

    cbs[1](total=100, completed=10)  # audio also downloading
    assert updates[-1]["active_tracks"] == ["video 2160p DV", "audio en-US 2.0"]

    cbs[0](downloaded="Downloaded")  # video done -> drops out of active
    assert updates[-1]["active_tracks"] == ["audio en-US 2.0"]


def test_aggregate_progress_is_monotonic_with_counts():
    updates: list[dict] = []
    inner_calls = [0, 0, 0]

    def make_inner(i):
        def inner(**kwargs):
            inner_calls[i] += 1

        return inner

    tracks = [Video(bitrate=1000), Audio(bitrate=1000), Subtitle()]
    cbs = build_job_progress_callables(tracks, [make_inner(0), make_inner(1), make_inner(2)], updates.append)
    assert len(cbs) == 3

    cbs[0](total=100, completed=50)
    cbs[0](downloaded="Downloaded")
    cbs[1](total=100, completed=50)

    progresses = [u["progress"] for u in updates]
    assert progresses == sorted(progresses)
    assert updates[-1]["completed_tracks"] == 1
    assert updates[-1]["total_tracks"] == 3
    assert inner_calls == [2, 1, 0]


def test_all_tracks_done_reaches_download_ceiling():
    # Downloads fill up to the ceiling; dl.result drives muxing the rest of the way to 100.
    updates: list[dict] = []
    cbs = build_job_progress_callables([Audio(bitrate=1000), Audio(bitrate=1000)], [_noop, _noop], updates.append)

    cbs[0](total=10, completed=10, downloaded="Downloaded")
    assert updates[-1]["progress"] < DOWNLOAD_PROGRESS_CEILING
    assert updates[-1]["completed_tracks"] == 1

    cbs[1](total=10, completed=10, downloaded="Decrypted")
    assert updates[-1]["progress"] == pytest.approx(DOWNLOAD_PROGRESS_CEILING)
    assert updates[-1]["completed_tracks"] == 2


def test_finished_track_does_not_dip_when_callable_reused_for_decrypt():
    """A track hits 100% (then decrypt reuses the callable with completed=0); the aggregate must
    hold, never dip - even before the terminal 'Downloaded'/'Decrypted' string arrives."""
    updates: list[dict] = []
    cbs = build_job_progress_callables([Video(bitrate=1000), Video(bitrate=1000)], [_noop, _noop], updates.append)

    cbs[0](total=100, completed=100)  # download hits 100% BEFORE any terminal string -> 50%
    cbs[0](total=200, completed=0)  # decrypt phase resets counts, still no terminal string
    cbs[0](total=200, completed=100)  # decrypt mid-way
    cbs[0](total=200, completed=200, downloaded="Decrypted")  # terminal

    progresses = [u["progress"] for u in updates]
    assert progresses == sorted(progresses)  # monotonic, no dip
    assert updates[-1]["progress"] == pytest.approx(DOWNLOAD_PROGRESS_CEILING / 2)
    assert updates[-1]["completed_tracks"] == 1


def test_skipped_subtitle_counts_as_done():
    updates: list[dict] = []
    cbs = build_job_progress_callables([Subtitle()], [_noop], updates.append)
    cbs[0](downloaded="[yellow]SKIPPED")
    assert updates[-1]["completed_tracks"] == 1
    assert updates[-1]["progress"] == pytest.approx(DOWNLOAD_PROGRESS_CEILING)
