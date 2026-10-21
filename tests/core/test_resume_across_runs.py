"""A failed track's kept segments survive the per-run temp dir and come back in the next run."""

from __future__ import annotations

import os
import time
from functools import partial
from pathlib import Path

import pytest

from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED, DOWNLOAD_LICENCE_ONLY, DownloadCancelled
from unshackle.core.temp import (
    RESUME_STASH,
    STASH_MAX_AGE,
    restore_segments,
    resume_stash,
    stash_segments,
    sweep_task_dirs,
    task_temp_dir,
)
from unshackle.core.tracks import Video, resume

pytestmark = pytest.mark.unit

DIGEST = "f" * 64
SEG_NAME = "Video_t1.mp4_segments"


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(config.directories, "temp", root)
    DOWNLOAD_CANCELLED.clear()
    DOWNLOAD_LICENCE_ONLY.clear()
    yield root
    DOWNLOAD_CANCELLED.clear()


def make_track() -> Video:
    return Video(
        id_="t1",
        url="https://example.test/manifest.mpd",
        language="en",
        codec=Video.Codec.AVC,
        range_=Video.Range.SDR,
        width=1920,
        height=1080,
        bitrate=5_000_000,
        descriptor=Video.Descriptor.DASH,
    )


def run_download(monkeypatch: pytest.MonkeyPatch, parser) -> None:
    import requests as requests_lib

    from unshackle.core.manifests import DASH

    monkeypatch.setattr(DASH, "download_track", staticmethod(parser))
    make_track().download(
        session=requests_lib.Session(),
        prepare_drm=partial(lambda *a, **kw: None),
        progress=partial(lambda **kw: None),
    )


def seed(ctx):
    ctx.save_dir.mkdir(parents=True, exist_ok=True)
    (ctx.save_dir / "0.mp4").write_bytes(b"complete segment")
    resume.write_sidecar(ctx.save_dir, DIGEST)


def failing_parser(track, ctx):
    seed(ctx)
    (ctx.save_dir / "1.mp4.p.!dev").write_bytes(b"partial")
    raise RuntimeError("simulated crash")


def test_failed_track_resumes_in_next_run(temp_root, monkeypatch):
    monkeypatch.setattr(config, "continue_downloads", True)

    with task_temp_dir():
        with pytest.raises(RuntimeError):
            run_download(monkeypatch, failing_parser)

    stashed = temp_root / RESUME_STASH / SEG_NAME
    assert (stashed / "0.mp4").read_bytes() == b"complete segment"
    assert resume.sidecar_path(stashed).exists()

    seen = {}

    def second_parser(track, ctx):
        seen["bytes"] = (ctx.save_dir / "0.mp4").read_bytes()
        seen["reusable"] = resume.reusable(ctx.save_dir, DIGEST)
        ctx.save_path.write_bytes(b"output")
        track.path = ctx.save_path

    with task_temp_dir():
        run_download(monkeypatch, second_parser)

    assert seen == {"bytes": b"complete segment", "reusable": True}
    assert not stashed.exists()


def test_flag_off_stashes_nothing(temp_root, monkeypatch):
    monkeypatch.setattr(config, "continue_downloads", False)

    with task_temp_dir():
        with pytest.raises(RuntimeError):
            run_download(monkeypatch, failing_parser)

    assert not (temp_root / RESUME_STASH).exists()


def test_partials_are_not_stashed(temp_root, monkeypatch):
    monkeypatch.setattr(config, "continue_downloads", True)

    with task_temp_dir():
        with pytest.raises(RuntimeError):
            run_download(monkeypatch, failing_parser)

    assert (temp_root / RESUME_STASH / SEG_NAME / "0.mp4").exists()
    assert not list((temp_root / RESUME_STASH).rglob("*.!dev"))


def test_existing_stash_target_is_left_alone(temp_root):
    with task_temp_dir() as task_dir:
        target = temp_root / RESUME_STASH / SEG_NAME
        target.mkdir(parents=True)
        (target / "old.mp4").write_bytes(b"old")
        save_dir = task_dir / SEG_NAME
        save_dir.mkdir()
        (save_dir / "new.mp4").write_bytes(b"new")

        stash_segments(save_dir)

        assert (target / "old.mp4").read_bytes() == b"old"
        assert not (target / "new.mp4").exists()
        assert save_dir.exists()


def test_restore_is_a_noop_without_stash_or_with_present_dir(temp_root):
    with task_temp_dir() as task_dir:
        save_dir = task_dir / SEG_NAME
        restore_segments(save_dir)
        assert not save_dir.exists()

        target = temp_root / RESUME_STASH / SEG_NAME
        target.mkdir(parents=True)
        (target / "a.mp4").write_bytes(b"a")
        save_dir.mkdir()

        restore_segments(save_dir)

        assert (target / "a.mp4").exists()
        assert not (save_dir / "a.mp4").exists()


def test_rename_failure_never_replaces_download_error(temp_root, monkeypatch):
    monkeypatch.setattr(config, "continue_downloads", True)

    def deny(self, target):
        raise PermissionError("locked")

    with task_temp_dir():
        monkeypatch.setattr(Path, "rename", deny)
        with pytest.raises(RuntimeError, match="simulated crash"):
            run_download(monkeypatch, failing_parser)


def test_sweep_removes_only_old_stash_entries(temp_root):
    stash = temp_root / RESUME_STASH
    old, fresh = stash / "old_segments", stash / "fresh_segments"
    old.mkdir(parents=True)
    fresh.mkdir()
    old_sidecar = stash / "old_segments.resume.json"
    old_sidecar.write_text("{}")
    stamp = time.time() - STASH_MAX_AGE - 60
    for path in (old, old_sidecar):
        os.utime(path, (stamp, stamp))

    sweep_task_dirs(temp_root)

    assert not old.exists()
    assert not old_sidecar.exists()
    assert fresh.exists()


def test_no_stash_outside_task_dir(temp_root):
    assert resume_stash() is None


def test_cancelled_track_is_stashed(temp_root, monkeypatch):
    monkeypatch.setattr(config, "continue_downloads", True)

    def cancelled_parser(track, ctx):
        seed(ctx)
        raise DownloadCancelled

    with task_temp_dir():
        run_download(monkeypatch, cancelled_parser)

    stashed = temp_root / RESUME_STASH / SEG_NAME
    assert (stashed / "0.mp4").read_bytes() == b"complete segment"
    assert resume.sidecar_path(stashed).exists()


def test_presized_data_file_is_dropped_with_its_control_file(temp_root, monkeypatch):
    monkeypatch.setattr(config, "continue_downloads", True)

    def parser(track, ctx):
        seed(ctx)
        (ctx.save_dir / "1.mp4").write_bytes(b"\0" * 8)
        (ctx.save_dir / "1.mp4.!dev").write_bytes(b"")
        raise RuntimeError("simulated crash")

    with task_temp_dir():
        with pytest.raises(RuntimeError):
            run_download(monkeypatch, parser)

    stashed = temp_root / RESUME_STASH / SEG_NAME
    assert (stashed / "0.mp4").exists()
    assert not (stashed / "1.mp4").exists()
    assert not (stashed / "1.mp4.!dev").exists()


def test_stash_refreshes_mtime(temp_root):
    with task_temp_dir() as task_dir:
        save_dir = task_dir / SEG_NAME
        save_dir.mkdir()
        resume.write_sidecar(save_dir, DIGEST)
        stamp = time.time() - STASH_MAX_AGE - 60
        os.utime(save_dir, (stamp, stamp))

        stash_segments(save_dir)

    target = temp_root / RESUME_STASH / SEG_NAME
    assert time.time() - target.stat().st_mtime < 60
