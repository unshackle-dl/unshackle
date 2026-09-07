"""Unit tests for the progress write rate and for concurrent progress writers.

The aggregate sink rewrites a whole JSON file per call, and one track sends thousands of ticks
while the reader polls twice a second. These tests pin the two guards on that: repeat ticks are
throttled, a tick that changes the job shape is not, and two threads writing the same file at the
same time do not lose a write."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from unshackle.core.api import progress as progress_module
from unshackle.core.api.download_worker import write_result
from unshackle.core.api.progress import build_job_progress_callables

pytestmark = pytest.mark.unit


class Video:
    def __init__(self, height=1080, bitrate=4_000_000):
        self.height = height
        self.range = None
        self.bitrate = bitrate


def noop(**kwargs):
    pass


def test_repeat_ticks_are_throttled():
    updates: list[dict] = []
    cbs = build_job_progress_callables([Video()], [noop], updates.append)

    for completed in range(1, 501):
        cbs[0](total=1000, completed=completed)

    # One emit for the first tick (the track starts); the rest say the same thing and coalesce.
    assert len(updates) == 1


def test_track_finish_bypasses_the_throttle():
    updates: list[dict] = []
    cbs = build_job_progress_callables([Video(), Video()], [noop, noop], updates.append)

    cbs[0](total=100, completed=1)
    cbs[0](total=100, completed=2)
    cbs[0](total=100, completed=3)
    assert len(updates) == 1

    cbs[0](downloaded="Downloaded")
    assert len(updates) == 2
    assert updates[-1]["completed_tracks"] == 1


def test_phase_change_bypasses_the_throttle():
    updates: list[dict] = []
    cbs = build_job_progress_callables([Video(2160), Video(720)], [noop, noop], updates.append)

    cbs[0](total=100, completed=1)
    first_phase = updates[-1]["phase"]

    cbs[1](total=100, completed=1)
    assert len(updates) == 2
    assert updates[-1]["phase"] != first_phase


def test_filled_track_bypasses_the_throttle_without_a_terminal_string():
    """A track can reach its segment total and never send "Downloaded". That last tick must land,
    or the job reports a percentage that is short for good."""
    updates: list[dict] = []
    cbs = build_job_progress_callables([Video()], [noop], updates.append)

    cbs[0](total=100, completed=10)
    cbs[0](total=100, completed=50)
    assert len(updates) == 1

    cbs[0](total=100, completed=100)
    assert len(updates) == 2
    assert updates[-1]["progress"] == pytest.approx(progress_module.DOWNLOAD_PROGRESS_CEILING)


def test_throttle_releases_after_the_interval(monkeypatch: pytest.MonkeyPatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(progress_module.time, "monotonic", lambda: clock["now"])

    updates: list[dict] = []
    cbs = build_job_progress_callables([Video()], [noop], updates.append)

    cbs[0](total=100, completed=1)
    cbs[0](total=100, completed=2)
    assert len(updates) == 1

    clock["now"] += progress_module.PROGRESS_EMIT_INTERVAL + 0.01
    cbs[0](total=100, completed=3)
    assert len(updates) == 2


def test_concurrent_writers_do_not_clobber_each_other(tmp_path: Path):
    """Two threads can write the same progress file. A temp name shared by two writers lets one
    replace away the file the other is about to move, which fails that write."""
    target = tmp_path / "progress.json"
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def writer(index: int) -> None:
        start.wait()
        try:
            for tick in range(40):
                write_result(target, {"writer": index, "tick": tick})
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert set(payload) == {"writer", "tick"}
    assert [p.name for p in tmp_path.iterdir()] == ["progress.json"]
