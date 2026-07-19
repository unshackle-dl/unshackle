"""Unit tests for the opt-in adaptive worker controller (slow-start then AIMD hill-climb
over segment concurrency) and the plumbing that carries the flag into the downloader. Pure
controller tests only. No threads, sockets, or real downloads; the clock is supplied
explicitly."""

from __future__ import annotations

import inspect
from functools import partial
from pathlib import Path

import pytest

from unshackle.core.downloaders.requests import (
    TAIL_BOOST_MIN_SEGMENT_SIZE,
    TAIL_BOOST_PART_SIZE,
    AdaptiveWorkerController,
    _has_range_header,
    _plan_tail_parts,
    _split_ranges,
    _tail_boost_engages,
    requests,
)
from unshackle.core.tracks.track import DownloadContext

pytestmark = pytest.mark.unit

TICK = 4.0
WINDOW = 10.0
DT = 0.5


def _fill_window(c: AdaptiveWorkerController, rate: float, now: float) -> float:
    """Record ``WINDOW`` seconds of samples at ``rate`` bytes/sec ending on the 0.5s grid.

    Records a sample at ``now`` and every 0.5s up to ``now + WINDOW`` so the oldest kept
    sample sits exactly ``WINDOW`` old. That is the condition ``update`` needs to start acting.
    """
    c.record_bytes(int(rate * DT), now)
    end = now + WINDOW
    while now < end - 1e-9:
        now += DT
        c.record_bytes(int(rate * DT), now)
    return now


def _run_tick(c: AdaptiveWorkerController, rate: float, now: float, errors: int = 0) -> tuple[int, float]:
    """Advance one tick recording samples at ``rate``, optionally inject ``errors``, then update."""
    end = now + TICK
    while now < end - 1e-9:
        now += DT
        c.record_bytes(int(rate * DT), now)
    for _ in range(errors):
        c.record_error(now)
    return c.update(now), now


def test_starts_at_min_six_cap() -> None:
    assert AdaptiveWorkerController(cap=16).target == 6
    assert AdaptiveWorkerController(cap=8).target == 6
    assert AdaptiveWorkerController(cap=4).target == 4  # min(6, cap)
    assert AdaptiveWorkerController(cap=1).target == 2  # clamped up to ADAPTIVE_MIN


def test_no_op_before_warmup() -> None:
    c = AdaptiveWorkerController(cap=16, tick=TICK, window=WINDOW)
    assert c.update(0.0) == 6  # arms last_tick before any samples are recorded
    c.record_bytes(500_000, 3.0)
    # a full tick has elapsed since arming, but under half a tick of samples exists -> hold
    assert c.update(4.5) == 6
    c.record_bytes(500_000, 6.5)
    # warmed up (first sample 3.0, now 8.5) -> slow-start probe fires
    assert c.update(8.5) == 12


def test_ramps_up_while_speed_improves() -> None:
    c = AdaptiveWorkerController(cap=16, tick=TICK, window=WINDOW)
    now = 0.0
    c.update(now)  # arm
    rate = 1_000_000.0
    now = _fill_window(c, rate, now)
    targets = [c.update(now)]  # first evaluation: slow-start doubles 6 -> 12
    for _ in range(6):
        rate *= 2  # sustained >10% windowed gain each tick
        target, now = _run_tick(c, rate, now)
        targets.append(target)
    assert targets == sorted(targets)  # monotonic non-decreasing
    assert targets[-1] == 16  # climbed to the cap
    assert max(targets) <= 16


def test_reverts_on_plateau_and_ends_slow_start() -> None:
    c = AdaptiveWorkerController(cap=16, tick=TICK, window=WINDOW)
    now = 0.0
    c.update(now)
    rate = 1_000_000.0
    now = _fill_window(c, rate, now)
    assert c.update(now) == 12  # first increase slow-starts 6 -> 12
    # hold the rate flat: <10% gain -> the tick reverts the increase to its restore point
    target, now = _run_tick(c, rate, now)
    assert target == 6
    # slow-start is over: the next successful probe is additive (+2), not a double
    target, now = _run_tick(c, rate * 4, now)
    assert target == 8


def test_halves_and_cooldown_on_error_burst() -> None:
    c = AdaptiveWorkerController(cap=16, tick=TICK, window=WINDOW)
    now = 0.0
    c.update(now)
    now = _fill_window(c, 1_000_000.0, now)
    for _ in range(3):
        c.record_error(now)
    assert c.update(now) == 3  # 3 errors -> multiplicative decrease 6 // 2

    # cooldown tick holds even with improving throughput
    target, now = _run_tick(c, 2_000_000.0, now)
    assert target == 3

    # cooldown consumed -> climbing resumes
    target, now = _run_tick(c, 4_000_000.0, now)
    assert target == 5


def test_clamps_to_bounds_and_never_exceeds_cap() -> None:
    c = AdaptiveWorkerController(cap=4, tick=TICK, window=WINDOW)
    assert c.target == 4  # already at cap (min(6, 4))
    now = 0.0
    c.update(now)
    rate = 1_000_000.0
    now = _fill_window(c, rate, now)
    seen = [c.update(now)]
    for _ in range(10):
        rate *= 2
        target, now = _run_tick(c, rate, now)
        seen.append(target)
    assert all(2 <= s <= 4 for s in seen)
    assert max(seen) == 4  # never exceeds the cap


def test_error_burst_floors_at_min() -> None:
    c = AdaptiveWorkerController(cap=16, start=2, tick=TICK, window=WINDOW)
    now = 0.0
    c.update(now)
    now = _fill_window(c, 1_000_000.0, now)
    for _ in range(5):
        c.record_error(now)
    assert c.update(now) == 2  # max(ADAPTIVE_MIN, 2 // 2)


def test_tail_guard_suppresses_increase() -> None:
    # with fewer units of work than the target, the tick must hold instead of probing up
    c = AdaptiveWorkerController(cap=16, tick=TICK, window=WINDOW)
    now = 0.0
    c.update(now)  # arm at target 6
    now = _fill_window(c, 1_000_000.0, now)
    assert c.update(now, 3) == 6  # 3 < target 6 -> holds at 6 rather than climbing to 8


def test_tail_guard_none_path_unchanged() -> None:
    # without a tail count, the update still probes upward on the first evaluation
    c = AdaptiveWorkerController(cap=16, tick=TICK, window=WINDOW)
    now = 0.0
    c.update(now)
    now = _fill_window(c, 1_000_000.0, now)
    assert c.update(now) == 12  # first evaluation slow-starts 6 -> 12


def test_tail_guard_still_halves_on_error_burst() -> None:
    # the guard suppresses growth but never blocks an error-burst decrease
    c = AdaptiveWorkerController(cap=16, tick=TICK, window=WINDOW)
    now = 0.0
    c.update(now)
    now = _fill_window(c, 1_000_000.0, now)
    for _ in range(3):
        c.record_error(now)
    assert c.update(now, 2) == 3  # guard active (2 < 6) but 3 errors -> 6 // 2


def test_plan_tail_parts_full_coverage_no_gaps() -> None:
    size = 20 * 1024 * 1024
    parts = _plan_tail_parts(size, spare=8)
    assert parts, "expected a split for a large segment with spare capacity"
    assert parts[0][0] == 0
    assert parts[-1][1] == size - 1
    for (_, prev_end), (start, _) in zip(parts, parts[1:]):
        assert start == prev_end + 1  # each part begins exactly where the previous one ended
    covered = sum(end - start + 1 for start, end in parts)
    assert covered == size


def test_plan_tail_parts_respects_spare_cap() -> None:
    size = 100 * 1024 * 1024  # would want ~25 parts at 4MB, but spare caps it
    parts = _plan_tail_parts(size, spare=3)
    assert len(parts) <= 3
    assert parts[0][0] == 0 and parts[-1][1] == size - 1


def test_plan_tail_parts_part_size_bounded() -> None:
    size = 40 * 1024 * 1024
    parts = _plan_tail_parts(size, spare=16)
    # never more parts than ~ceil(size / TAIL_BOOST_PART_SIZE)
    assert len(parts) <= -(-size // TAIL_BOOST_PART_SIZE)


def test_plan_tail_parts_skips_small_or_no_spare() -> None:
    assert _plan_tail_parts(TAIL_BOOST_MIN_SEGMENT_SIZE - 1, spare=8) == []  # too small
    assert _plan_tail_parts(20 * 1024 * 1024, spare=1) == []  # not enough spare workers
    assert _plan_tail_parts(20 * 1024 * 1024, spare=0) == []


def test_split_ranges_covers_exactly_no_gaps_no_overlaps() -> None:
    for size in (1, 100, 4 * 1024 * 1024, 16 * 1024 * 1024 + 7, 64 * 1024 * 1024):
        parts = _split_ranges(size, max_parts=8, part_target=4 * 1024 * 1024)
        assert parts[0][0] == 0 and parts[-1][1] == size - 1
        for (_, prev_end), (start, _) in zip(parts, parts[1:]):
            assert start == prev_end + 1
        assert all(start <= end for start, end in parts)
        assert sum(end - start + 1 for start, end in parts) == size


def test_split_ranges_respects_max_parts_and_target() -> None:
    assert len(_split_ranges(100 * 1024 * 1024, max_parts=4, part_target=16 * 1024 * 1024)) == 4  # capped
    assert len(_split_ranges(10 * 1024 * 1024, max_parts=8, part_target=16 * 1024 * 1024)) == 1  # below one target
    assert _split_ranges(1, max_parts=8, part_target=16 * 1024 * 1024) == [(0, 0)]


def test_tail_boost_engages_on_idle_capacity() -> None:
    # engages at the tail: idle workers outnumber the remaining whole segments
    assert _tail_boost_engages(remaining=4, pending=2, target=8)  # spare 6 >= 4
    assert _tail_boost_engages(remaining=6, pending=6, target=16)  # spare 10 >= 6


def test_tail_boost_engages_when_remaining_is_a_full_stride() -> None:
    # remaining sits at 6 (one worker stride) while the target has climbed to 16, so 14 workers
    # are idle. idle capacity far exceeds the remaining segments, so the boost must engage.
    assert _tail_boost_engages(remaining=6, pending=2, target=16)


def test_tail_boost_does_not_engage_early_or_saturated() -> None:
    assert not _tail_boost_engages(remaining=54, pending=2, target=6)  # early: 54 > spare 4
    assert not _tail_boost_engages(remaining=1, pending=8, target=8)  # saturated: spare 0
    assert not _tail_boost_engages(remaining=0, pending=2, target=8)  # nothing left
    assert not _tail_boost_engages(remaining=5, pending=10, target=8)  # over-subscribed: spare < 0


def test_has_range_header_detects_byte_range_slices() -> None:
    # items carrying a Range header are byte-range slices; ranged-parallel must skip them
    assert _has_range_header({"url": "u", "headers": {"Range": "bytes=0-99"}})
    assert _has_range_header({"url": "u", "headers": {"range": "bytes=0-99"}})  # case-insensitive
    assert not _has_range_header({"url": "u", "headers": {"Authorization": "Bearer x"}})
    assert not _has_range_header({"url": "u", "headers": {}})
    assert not _has_range_header({"url": "u"})


def test_download_context_adaptive_default_false() -> None:
    ctx = DownloadContext(save_path=Path("x.mp4"), save_dir=Path("."), progress=partial(lambda **_: None))
    assert ctx.adaptive_workers is False


def test_requests_accepts_adaptive_kwarg() -> None:
    sig = inspect.signature(requests)
    assert "adaptive" in sig.parameters
    assert sig.parameters["adaptive"].default is False
