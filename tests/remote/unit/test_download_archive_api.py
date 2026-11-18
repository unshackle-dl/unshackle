"""Completion reporting survives the worker boundary and is scoped by the server."""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from unshackle.core.api import download_manager as dm
from unshackle.core.api import download_worker
from unshackle.core.api.handlers import cast_dl_params, validate_download_parameters
from unshackle.core.config import config

PRESENT = [
    {
        "service": "EXAMPLE",
        "title_id": "example-1",
        "reason": "already_present",
        "variants": [{"quality": 720, "range": "SDR", "codec": "H.264"}],
    }
]


@pytest.mark.parametrize("progress_file", [True, False])
def test_worker_result_keeps_already_present(monkeypatch, tmp_path, progress_file):
    seen = {}

    def perform(*args, **kwargs):
        seen.update(kwargs)
        kwargs["progress_callback"]({"already_present": PRESENT})
        return []

    monkeypatch.setattr(download_worker, "perform_download", perform)
    monkeypatch.setattr(download_worker, "relay_prompts", lambda *a: None)
    payload = tmp_path / "payload.json"
    result = tmp_path / "result.json"
    scope = "api-" + "a" * 64
    payload.write_text(
        json.dumps(
            {
                "job_id": "test",
                "service": "EXAMPLE",
                "title_id": "example-1",
                "archive_scope": scope,
                "parameters": {"archive_scope": "cli"},
            }
        )
    )
    argv = ["worker", str(payload), str(result)]
    if progress_file:
        argv.append(str(tmp_path / "progress.json"))
    assert download_worker.main(argv) == 0
    assert seen["archive_scope"] == scope
    assert json.loads(result.read_text())["already_present"] == PRESENT
    assert json.loads(result.read_text())["output_files"] == []


def test_api_options_are_typed_and_reach_dl():
    from unshackle.commands.dl import dl

    assert cast_dl_params(dl.cli, {"skip_existing": True, "force": True})["skip_existing"] is True
    assert cast_dl_params(dl.cli, {"force": True})["force"] is True
    assert validate_download_parameters({"skip_existing": "true"}) == "skip_existing must be a boolean"
    assert validate_download_parameters({"force": "true"}) == "force must be a boolean"


async def test_manager_uses_owner_scope_and_final_result_for_history(monkeypatch, tmp_path):
    monkeypatch.setattr(config.directories, "cache", tmp_path / "cache")
    manager = dm.DownloadQueueManager()
    job = manager.create_job("EXAMPLE", "example-1", owner_key="test-owner", archive_scope="cli")
    spawn = asyncio.create_subprocess_exec
    worker = tmp_path / "worker.py"
    # Test the real manager subprocess protocol without media, a network or credentials.
    worker.write_text(
        """import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text())
Path(sys.argv[2]).write_text(json.dumps({"status": "success", "output_files": [], "already_present": %s}))
Path(sys.argv[3]).write_text(json.dumps({"scope": payload["archive_scope"]}))
"""
        % repr(PRESENT)
    )
    scopes = []

    async def fake_worker(executable, *args, **kwargs):
        payload = json.loads(Path(args[2]).read_text())
        scopes.append(payload["archive_scope"])
        return await spawn(executable, str(worker), *args[2:], **kwargs)

    monkeypatch.setattr(dm.asyncio, "create_subprocess_exec", fake_worker)
    await manager.execute_download(job)
    assert scopes == ["api-" + hashlib.sha256(b"test-owner").hexdigest()]
    assert job.status == dm.JobStatus.COMPLETED
    assert job.output_files == []
    assert job.skipped_titles == []
    assert job.to_dict()["already_present"] == PRESENT
    dm.record_job_history(job)
    assert dm.read_job_history(owner=dm.owner_id("test-owner"))[0]["already_present"] == PRESENT
    assert dm.read_job_history(owner=dm.owner_id("another-owner")) == []


def test_request_cannot_choose_archive_scope_or_credential_namespace(monkeypatch, tmp_path):
    import click

    import unshackle.commands.dl as dl_module
    from unshackle.core.api import handlers
    from unshackle.core.services import Services

    original_cli = dl_module.dl.cli
    seen = {}

    class Command:
        cli = original_cli

        def __init__(self, **kwargs):
            self.completed_files = []
            self.already_present = []

        def result(self, **kwargs):
            seen.update(scope=self.archive_scope, cache=self.archive_cache)

    class EXAMPLE:
        cli = click.Command("EXAMPLE")

        def __init__(self, ctx):
            pass

    monkeypatch.setattr(dl_module, "dl", Command)
    monkeypatch.setattr(Services, "get_path", lambda service: tmp_path)
    monkeypatch.setattr(Services, "load", lambda service: EXAMPLE)
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *a: None)
    monkeypatch.setattr(config.directories, "cache", tmp_path / "cache")
    monkeypatch.setattr(config, "credentials", {})
    scope = "api-" + "c" * 64
    assert (
        dm.perform_download(
            "test",
            "EXAMPLE",
            "example-1",
            {
                "archive_scope": "cli",
                "archive_cache": "/not-allowed",
                "credential": "test-user:test-password",
            },
            archive_scope=scope,
        )
        == []
    )
    assert seen == {"scope": scope, "cache": tmp_path / "cache"}


def test_worker_failure_preserves_earlier_archive_skips(monkeypatch, tmp_path):
    def perform(*a, **kwargs):
        kwargs["progress_callback"]({"already_present": PRESENT})
        raise RuntimeError("later title failed")

    monkeypatch.setattr(download_worker, "perform_download", perform)
    monkeypatch.setattr(download_worker, "relay_prompts", lambda *a: None)
    payload = tmp_path / "payload.json"
    result = tmp_path / "result.json"
    payload.write_text(json.dumps({"job_id": "test", "service": "EXAMPLE", "title_id": "example-1"}))
    assert download_worker.main(["worker", str(payload), str(result)]) == 1
    assert json.loads(result.read_text())["already_present"] == PRESENT
