"""Unit tests for DownloadJob + DownloadQueueManager state machine.

These tests focus on the queue manager's data layer (create/get/list/cancel/
cleanup/serialize); they do not exercise the actual subprocess download path.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from unshackle.core.api.download_manager import DownloadJob, DownloadQueueManager, JobStatus, get_download_manager

pytestmark = pytest.mark.unit


@pytest.fixture
def manager() -> DownloadQueueManager:
    """Fresh manager. We never call start_workers() so no async tasks are created."""
    return DownloadQueueManager(max_concurrent_downloads=2, job_retention_hours=24)


def test_create_job_returns_queued_job(manager: DownloadQueueManager) -> None:
    job = manager.create_job("EXAMPLE", "movie-123", profile="default")
    assert isinstance(job, DownloadJob)
    assert job.status is JobStatus.QUEUED
    assert job.service == "EXAMPLE"
    assert job.title_id == "movie-123"
    assert job.parameters == {"profile": "default"}


def test_get_and_list_jobs(manager: DownloadQueueManager) -> None:
    a = manager.create_job("EXAMPLE", "a")
    b = manager.create_job("DEMO", "b")
    assert manager.get_job(a.job_id) is a
    assert manager.get_job("missing") is None
    listed = manager.list_jobs()
    assert {j.job_id for j in listed} == {a.job_id, b.job_id}


def test_to_dict_short_vs_full(manager: DownloadQueueManager) -> None:
    job = manager.create_job("EXAMPLE", "t", profile="p")
    short = job.to_dict()
    assert "parameters" not in short
    assert short["status"] == "queued"
    assert short["service"] == "EXAMPLE"
    full = job.to_dict(include_full_details=True)
    assert full["parameters"] == {"profile": "p"}
    assert "error_message" in full
    assert "output_files" in full


def test_cancel_queued_job_sets_cancelled_and_signals_event(manager: DownloadQueueManager) -> None:
    job = manager.create_job("EXAMPLE", "t")
    assert manager.cancel_job(job.job_id) is True
    assert job.status is JobStatus.CANCELLED
    assert job.cancel_event.is_set()


def test_cancel_unknown_job_returns_false(manager: DownloadQueueManager) -> None:
    assert manager.cancel_job("never-existed") is False


def test_cancel_completed_job_returns_false(manager: DownloadQueueManager) -> None:
    job = manager.create_job("EXAMPLE", "t")
    job.status = JobStatus.COMPLETED
    assert manager.cancel_job(job.job_id) is False


def test_cancel_downloading_job_signals(manager: DownloadQueueManager) -> None:
    job = manager.create_job("EXAMPLE", "t")
    job.status = JobStatus.DOWNLOADING
    assert manager.cancel_job(job.job_id) is True
    assert job.status is JobStatus.CANCELLED
    assert job.cancel_event.is_set()


def test_cleanup_old_jobs_drops_old_terminal_states(manager: DownloadQueueManager) -> None:
    now = datetime.now()
    old = now - timedelta(hours=48)
    keep_recent = manager.create_job("EXAMPLE", "recent")
    drop_old_done = manager.create_job("EXAMPLE", "old-done")
    drop_old_failed = manager.create_job("EXAMPLE", "old-failed")
    keep_running = manager.create_job("EXAMPLE", "running")

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


async def test_busy_services_counts_a_live_remote_session() -> None:
    from unshackle.core.api.download_manager import busy_services
    from unshackle.core.api.session_store import get_session_store

    store = get_session_store()
    session = await store.create("EXAMPLE", object(), session_id="busy-session")
    try:
        assert "EXAMPLE" in busy_services()
    finally:
        await store.delete(session.session_id)
    assert "EXAMPLE" not in busy_services()


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


class _CapturedCtx(Exception):
    pass


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, (["orig"], [], [], [], False)),
        (
            {"lang": ["es-419"], "v_lang": ["en"], "a_lang": ["ja"], "acodec": "AAC,EC3", "forced_subs": True},
            (["es-419"], ["en"], ["ja"], ["AAC", "EC3"], True),
        ),
    ],
)
def test_perform_download_puts_the_lang_selection_in_the_service_ctx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, params: dict, expected: tuple
) -> None:
    """Services read the language selection from ctx.parent.params, not from dl.result()."""
    import click

    import unshackle.commands.dl as dl_module
    from unshackle.core.api import download_manager, handlers
    from unshackle.core.services import Services

    class FakeDl:
        cli = click.Command("dl")

        def __init__(self, ctx: click.Context, **kwargs: object) -> None:
            raise _CapturedCtx(ctx)

    monkeypatch.setattr(dl_module, "dl", FakeDl)
    monkeypatch.setattr(Services, "get_path", staticmethod(lambda s: tmp_path))
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *a: None)

    with pytest.raises(_CapturedCtx) as exc:
        download_manager.perform_download("job-1", "EXAMPLE", "t1", dict(params))

    ctx = exc.value.args[0]
    p = ctx.params
    assert (p["lang"], p["v_lang"], p["a_lang"], [c.name for c in p["acodec"]], p["forced_subs"]) == expected


def test_worker_relays_a_prompt_and_reads_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import os
    import sys
    from types import SimpleNamespace

    from unshackle.core.api import download_worker
    from unshackle.core.console import prompt_user, set_prompt_handler

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(fileno=lambda: read_fd))
    updates: list[dict] = []

    def answer_on_prompt(update: dict) -> None:
        """Answer only once the prompt is out: the prompt discards answers that arrived before it."""
        updates.append(update)
        if update["input_prompt"]:
            os.write(write_fd, (json.dumps("12\n34") + "\n").encode("utf-8"))

    download_worker.relay_prompts(answer_on_prompt)
    try:
        assert prompt_user("Enter PIN") == "12\n34"
    finally:
        set_prompt_handler(None)
        os.close(write_fd)
    assert updates == [{"input_prompt": "Enter PIN"}, {"input_prompt": None}]


def test_a_prompt_discards_an_answer_sent_before_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A late answer to an earlier prompt must not answer the next one; the timeout is not retryable."""
    import sys
    import threading

    from unshackle.core.api import download_worker
    from unshackle.core.api.errors import APIErrorCode, categorize_exception
    from unshackle.core.console import prompt_user, set_prompt_handler

    answers = tmp_path / "stdin"
    answers.write_text('"stale"\n', encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", answers.open("rb"))
    monkeypatch.setattr(download_worker, "AUTH_INPUT_TIMEOUT", 0.2)
    download_worker.relay_prompts(lambda update: None)
    # The reader exits at EOF, so a finished join means the stale line is already queued.
    for reader in [t for t in threading.enumerate() if t.name == "prompt-answers"]:
        reader.join(timeout=5)
    try:
        with pytest.raises(TimeoutError) as exc_info:
            prompt_user("Enter PIN")
    finally:
        set_prompt_handler(None)
    assert categorize_exception(exc_info.value).error_code is APIErrorCode.AUTH_FAILED


def test_concurrent_prompts_go_out_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import os
    import sys
    import threading
    import time
    from types import SimpleNamespace

    from unshackle.core.api import download_worker
    from unshackle.core.console import prompt_user, set_prompt_handler

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(fileno=lambda: read_fd))
    monkeypatch.setattr(download_worker, "AUTH_INPUT_TIMEOUT", 5.0)
    updates: list[dict] = []
    download_worker.relay_prompts(updates.append)
    results: list[str] = []
    askers = [threading.Thread(target=lambda p=p: results.append(prompt_user(p)), daemon=True) for p in ("A", "B")]
    try:
        for asker in askers:
            asker.start()
        time.sleep(0.2)
        assert len(updates) == 1
        for answer, prompts_sent in (("1", 1), ("2", 3)):
            deadline = time.monotonic() + 5
            while len(updates) < prompts_sent and time.monotonic() < deadline:
                time.sleep(0.01)
            os.write(write_fd, (json.dumps(answer) + "\n").encode("utf-8"))
        for asker in askers:
            asker.join(timeout=5)
    finally:
        set_prompt_handler(None)
        os.close(write_fd)
    assert sorted(results) == ["1", "2"]
    assert [u["input_prompt"] is None for u in updates] == [False, True, False, True]


def test_submit_input_writes_one_json_line_to_the_worker(manager: DownloadQueueManager) -> None:
    import io
    from types import SimpleNamespace

    job = manager.create_job("EXAMPLE", "t")
    stdin = io.BytesIO()
    manager._download_processes[job.job_id] = SimpleNamespace(stdin=stdin)  # type: ignore[assignment]
    assert manager.submit_input(job, "1234") is False

    job.input_prompt = "Enter PIN"
    assert manager.submit_input(job, "1234") is True
    assert stdin.getvalue() == b'"1234"\n'
    assert job.input_prompt is None
    assert manager.submit_input(job, "5678") is False


FAKE_PROMPT_WORKER = """
import sys
from pathlib import Path
from typing import Any

from unshackle.core.api import download_worker
from unshackle.core.console import prompt_user

result_path, progress_path = Path(sys.argv[-2]), Path(sys.argv[-1])
state = {}


def progress(update):
    state.update(update)
    download_worker.write_result(progress_path, state)


download_worker.relay_prompts(progress)
answers = [prompt_user("Enter OTP"), prompt_user("Enter OTP")]
download_worker.write_result(result_path, {"status": "success", "output_files": answers})
"""


async def test_a_repeated_prompt_reaches_an_event_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: DownloadQueueManager
) -> None:
    """A re-prompt with the same text right after an answer must still publish an event."""
    import asyncio

    from unshackle.core.api import download_manager

    worker = tmp_path / "worker.py"
    worker.write_text(FAKE_PROMPT_WORKER, encoding="utf-8")
    spawn = asyncio.create_subprocess_exec

    async def spawn_fake_worker(executable: str, *args: str, **kwargs: Any) -> Any:
        return await spawn(executable, str(worker), *args[2:], **kwargs)

    monkeypatch.setattr(download_manager.asyncio, "create_subprocess_exec", spawn_fake_worker)
    job = manager.create_job("EXAMPLE", "t")
    events = manager.subscribe(job.job_id)
    run = asyncio.create_task(manager.run_download_async(job))

    async def answer_next_prompt(answer: str) -> None:
        while True:
            event = await events.get()
            if event and event["data"]["input_prompt"]:
                assert manager.submit_input(job, answer) is True
                return

    await asyncio.wait_for(answer_next_prompt("111111"), timeout=20)
    await asyncio.wait_for(answer_next_prompt("222222"), timeout=20)
    assert await asyncio.wait_for(run, timeout=20) == ["111111", "222222"]
    assert job.input_prompt is None


async def test_input_handler_rejects_bad_requests(
    monkeypatch: pytest.MonkeyPatch, manager: DownloadQueueManager
) -> None:
    from unshackle.core.api import download_manager
    from unshackle.core.api.errors import APIError, APIErrorCode
    from unshackle.core.api.handlers import download_job_input_handler

    monkeypatch.setattr(download_manager, "get_download_manager", lambda: manager)
    job = manager.create_job("EXAMPLE", "t")

    async def error_code(data: dict, job_id: str) -> APIErrorCode:
        with pytest.raises(APIError) as exc_info:
            await download_job_input_handler(data, job_id)
        return exc_info.value.error_code

    assert await error_code({"response": "1"}, "missing") is APIErrorCode.JOB_NOT_FOUND
    assert await error_code({}, job.job_id) is APIErrorCode.INVALID_INPUT
    assert await error_code({"response": "1"}, job.job_id) is APIErrorCode.CONFLICT
    job.owner_key = "another-key"
    assert await error_code({"response": "1"}, job.job_id) is APIErrorCode.JOB_NOT_FOUND
