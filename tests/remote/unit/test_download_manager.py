"""Unit tests for DownloadJob + DownloadQueueManager state machine.

These tests focus on the queue manager's data layer (create/get/list/cancel/
cleanup/serialize); they do not exercise the actual subprocess download path.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from unshackle.core.api.download_manager import DownloadJob, DownloadQueueManager, JobStatus, get_download_manager

pytestmark = pytest.mark.unit


@pytest.fixture
def manager() -> DownloadQueueManager:
    """Fresh manager. We never call start_workers() so no async tasks are created."""
    return DownloadQueueManager(max_concurrent_downloads=2, job_retention_hours=24)


def test_create_job_returns_queued_job(manager: DownloadQueueManager) -> None:
    job = manager.create_job("ATV", "movie-123", profile="default")
    assert isinstance(job, DownloadJob)
    assert job.status is JobStatus.QUEUED
    assert job.service == "ATV"
    assert job.title_id == "movie-123"
    assert job.parameters == {"profile": "default"}


def test_get_and_list_jobs(manager: DownloadQueueManager) -> None:
    a = manager.create_job("ATV", "a")
    b = manager.create_job("NF", "b")
    assert manager.get_job(a.job_id) is a
    assert manager.get_job("missing") is None
    listed = manager.list_jobs()
    assert {j.job_id for j in listed} == {a.job_id, b.job_id}


def test_to_dict_short_vs_full(manager: DownloadQueueManager) -> None:
    job = manager.create_job("ATV", "t", profile="p")
    short = job.to_dict()
    assert "parameters" not in short
    assert short["status"] == "queued"
    assert short["service"] == "ATV"
    full = job.to_dict(include_full_details=True)
    assert full["parameters"] == {"profile": "p"}
    assert "error_message" in full
    assert "output_files" in full


def test_cancel_queued_job_sets_cancelled_and_signals_event(manager: DownloadQueueManager) -> None:
    job = manager.create_job("ATV", "t")
    assert manager.cancel_job(job.job_id) is True
    assert job.status is JobStatus.CANCELLED
    assert job.cancel_event.is_set()


def test_cancel_unknown_job_returns_false(manager: DownloadQueueManager) -> None:
    assert manager.cancel_job("never-existed") is False


def test_cancel_completed_job_returns_false(manager: DownloadQueueManager) -> None:
    job = manager.create_job("ATV", "t")
    job.status = JobStatus.COMPLETED
    assert manager.cancel_job(job.job_id) is False


def test_cancel_downloading_job_signals(manager: DownloadQueueManager) -> None:
    job = manager.create_job("ATV", "t")
    job.status = JobStatus.DOWNLOADING
    assert manager.cancel_job(job.job_id) is True
    assert job.status is JobStatus.CANCELLED
    assert job.cancel_event.is_set()


def test_cleanup_old_jobs_drops_old_terminal_states(manager: DownloadQueueManager) -> None:
    now = datetime.now()
    old = now - timedelta(hours=48)
    keep_recent = manager.create_job("ATV", "recent")
    drop_old_done = manager.create_job("ATV", "old-done")
    drop_old_failed = manager.create_job("ATV", "old-failed")
    keep_running = manager.create_job("ATV", "running")

    keep_recent.status = JobStatus.COMPLETED
    keep_recent.completed_time = now

    drop_old_done.status = JobStatus.COMPLETED
    drop_old_done.completed_time = old

    drop_old_failed.status = JobStatus.FAILED
    drop_old_failed.created_time = old  # never set completed_time

    keep_running.status = JobStatus.DOWNLOADING

    removed = manager.cleanup_old_jobs()
    assert removed == 2
    remaining = {j.job_id for j in manager.list_jobs()}
    assert keep_recent.job_id in remaining
    assert keep_running.job_id in remaining
    assert drop_old_done.job_id not in remaining
    assert drop_old_failed.job_id not in remaining


def test_get_download_manager_returns_singleton() -> None:
    a = get_download_manager()
    b = get_download_manager()
    assert a is b


def test_job_status_values() -> None:
    assert {s.value for s in JobStatus} == {
        "queued",
        "downloading",
        "completed",
        "failed",
        "cancelled",
    }


def test_normalize_sub_format_preserves_original_sentinel() -> None:
    """An API job asking for sub_format="original" must keep the "original" sentinel.

    dl.py treats "original" as "keep source format" (skip conversion). If it were normalized to
    None (as to_enum(["original"]) yields, since there is no such enum member), dl.py would fall
    into the default branch and re-encode TTML -> WebVTT, contradicting the CLI.
    """
    from unshackle.core.api.download_manager import normalize_sub_format
    from unshackle.core.tracks import Subtitle
    from unshackle.core.utils.click_types import SubtitleCodecChoice

    # API path keeps the sentinel, case-insensitively.
    assert normalize_sub_format("original") == "original"
    assert normalize_sub_format("ORIGINAL") == "original"

    # The CLI produces the exact same sentinel value, so dl.py's `== "original"` branch handles both.
    assert SubtitleCodecChoice(Subtitle.Codec).convert("original") == "original"

    # A real codec still maps to its enum member; unknowns collapse to None.
    assert normalize_sub_format("srt") is Subtitle.Codec.SubRip
    assert normalize_sub_format("nonsense") is None


def test_worker_write_result_retries_denied_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows denies os.replace while the parent holds the progress file open, so the write must retry.

    Without the retry a denied replace loses that update. A value the worker writes once, such as a
    phase change, then stays invisible until the next write.
    """
    import json
    import os

    from unshackle.core.api import download_worker

    target = tmp_path / "progress.json"
    real_replace = os.replace
    attempts = []

    def flaky_replace(src: Path, dst: Path) -> None:
        attempts.append(dst)
        if len(attempts) == 1:
            raise PermissionError(13, "Permission denied", str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(download_worker.os, "replace", flaky_replace)
    download_worker.write_result(target, {"progress": 42.0})
    assert json.loads(target.read_text(encoding="utf-8")) == {"progress": 42.0}
    assert len(attempts) == 2
