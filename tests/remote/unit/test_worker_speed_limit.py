import asyncio
import importlib
import json
import tempfile
from types import SimpleNamespace

import pytest

from unshackle.core.api import download_worker
from unshackle.core.api.download_manager import DownloadQueueManager
from unshackle.core.api.download_worker import apply_payload_speed_limit

requests_downloader = importlib.import_module("unshackle.core.downloaders.requests")

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_limiter():
    """Start and end each test with no limiter, whatever an earlier test leaked."""
    requests_downloader.speed_limit_locked = False
    requests_downloader.speed_limiter = None
    yield
    requests_downloader.speed_limit_locked = False
    requests_downloader.speed_limiter = None


@pytest.mark.parametrize(("lock", "expected"), [(True, 2_000_000.0), (False, None)])
def test_worker_share_of_limit(lock, expected):
    requests_downloader.set_speed_limit(8_000_000, lock=lock)
    assert DownloadQueueManager(max_concurrent_downloads=4).worker_speed_limit() == expected


def test_worker_locks_payload_limit_against_job_reset():
    apply_payload_speed_limit({"speed_limit": 1_000_000})
    requests_downloader.set_speed_limit(None)
    limiter = requests_downloader.speed_limiter
    assert limiter is not None and limiter.rate == 1_000_000


def test_worker_without_payload_limit_stays_unlimited():
    apply_payload_speed_limit({"speed_limit": None})
    assert requests_downloader.speed_limiter is None


def test_run_download_async_sends_share_in_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    class Stop(Exception):
        pass

    seen = {}

    async def fake_spawn(*args, **kwargs):
        with open(args[3], encoding="utf-8") as handle:
            seen["payload"] = json.load(handle)
        raise Stop

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    requests_downloader.set_speed_limit(8_000_000, lock=True)
    job = SimpleNamespace(job_id="j1", service="SVC", title_id="t", parameters={})
    with pytest.raises(Stop):
        asyncio.run(DownloadQueueManager(max_concurrent_downloads=4).run_download_async(job))
    assert seen["payload"]["speed_limit"] == 2_000_000.0


def test_worker_main_applies_share_before_download(monkeypatch, tmp_path):
    seen = {}

    def fake_perform(*args, **kwargs):
        requests_downloader.set_speed_limit(None)  # what dl.result() does
        seen["rate"] = requests_downloader.speed_limiter.rate
        return []

    monkeypatch.setattr(download_worker, "perform_download", fake_perform)
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps({"job_id": "j1", "service": "SVC", "title_id": "t", "parameters": {}, "speed_limit": 500_000}),
        encoding="utf-8",
    )
    assert download_worker.main(["w", str(payload), str(tmp_path / "result.json")]) == 0
    assert seen["rate"] == 500_000
