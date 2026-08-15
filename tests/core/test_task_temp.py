"""Tests for per-task temp directories.

Each run gets a private dir that is removed on every exit path. The sweep reclaims
dirs from hard-killed processes via lock liveness, and `env clear temp` skips the
dir of a task that is still running.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from filelock import FileLock

from unshackle.commands.env import clear_directory
from unshackle.core.config import config
from unshackle.core.temp import LOCK_NAME, TASK_PREFIX, is_stale, sweep_task_dirs, task_temp_dir

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(config.directories, "temp", root)
    return root


def backdate(path: Path) -> None:
    stamp = time.time() - 120
    os.utime(path, (stamp, stamp))


def make_task_dir(root: Path, name: str = "task_deadbeef", lock: bool = True) -> Path:
    task_dir = root / name
    task_dir.mkdir()
    if lock:
        (task_dir / LOCK_NAME).touch()
    return task_dir


def test_task_dir_created_repointed_and_restored(temp_root: Path) -> None:
    with task_temp_dir() as task_dir:
        assert task_dir.parent == temp_root
        assert task_dir.name.startswith(TASK_PREFIX)
        assert config.directories.temp == task_dir
    assert config.directories.temp == temp_root
    assert not task_dir.exists()


def test_task_dir_removed_on_exception(temp_root: Path) -> None:
    with pytest.raises(ValueError):
        with task_temp_dir() as task_dir:
            (task_dir / "partial.mp4").write_bytes(b"x")
            raise ValueError("boom")
    assert not task_dir.exists()
    assert config.directories.temp == temp_root


def test_task_dir_removed_on_keyboard_interrupt(temp_root: Path) -> None:
    with pytest.raises(KeyboardInterrupt):
        with task_temp_dir() as task_dir:
            raise KeyboardInterrupt
    assert not task_dir.exists()
    assert config.directories.temp == temp_root


def test_sweep_removes_unlocked_stale_dir(temp_root: Path) -> None:
    task_dir = make_task_dir(temp_root)
    (task_dir / "leftover.mp4").write_bytes(b"x")
    backdate(task_dir)
    sweep_task_dirs(temp_root)
    assert not task_dir.exists()


def test_sweep_skips_locked_dir(temp_root: Path) -> None:
    task_dir = make_task_dir(temp_root)
    lock = FileLock(task_dir / LOCK_NAME)
    lock.acquire()
    backdate(task_dir)
    try:
        assert is_stale(task_dir) is False
        sweep_task_dirs(temp_root)
        assert task_dir.exists()
    finally:
        lock.release()


def test_sweep_skips_young_unlocked_dir(temp_root: Path) -> None:
    task_dir = make_task_dir(temp_root)
    assert is_stale(task_dir) is False
    sweep_task_dirs(temp_root)
    assert task_dir.exists()


def test_sweep_removes_stale_dir_without_lock_file(temp_root: Path) -> None:
    task_dir = make_task_dir(temp_root, lock=False)
    backdate(task_dir)
    assert is_stale(task_dir) is True
    sweep_task_dirs(temp_root)
    assert not task_dir.exists()


def test_sweep_ignores_non_task_entries(temp_root: Path) -> None:
    loose_file = temp_root / "leftover.mp4"
    loose_file.write_bytes(b"x")
    other_dir = temp_root / "not_a_task"
    other_dir.mkdir()
    backdate(loose_file)
    backdate(other_dir)
    sweep_task_dirs(temp_root)
    assert loose_file.exists()
    assert other_dir.exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_sweep_never_follows_symlinked_task_dir(temp_root: Path, tmp_path: Path) -> None:
    target = tmp_path / "victim"
    target.mkdir()
    (target / "precious.mkv").write_bytes(b"data")
    link = temp_root / "task_evil"
    link.symlink_to(target)
    backdate(target)
    sweep_task_dirs(temp_root)
    assert (target / "precious.mkv").exists()
    assert not (target / LOCK_NAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_clear_directory_unlinks_symlinked_dir_without_following(temp_root: Path, tmp_path: Path) -> None:
    target = tmp_path / "victim"
    target.mkdir()
    (target / "precious.mkv").write_bytes(b"data")
    link = temp_root / "task_evil"
    link.symlink_to(target)
    backdate(target)
    files_count, _ = clear_directory(temp_root)
    assert not link.exists()
    assert (target / "precious.mkv").exists()
    assert files_count == 1


def test_clear_directory_skips_locked_task_dir(temp_root: Path) -> None:
    task_dir = make_task_dir(temp_root)
    (task_dir / "in_flight.mp4").write_bytes(b"xyz")
    lock = FileLock(task_dir / LOCK_NAME)
    lock.acquire()
    backdate(task_dir)
    loose_file = temp_root / "leftover.mp4"
    loose_file.write_bytes(b"1234")
    try:
        files_count, freed_bytes = clear_directory(temp_root)
    finally:
        lock.release()
    assert task_dir.exists()
    assert (task_dir / "in_flight.mp4").exists()
    assert not loose_file.exists()
    assert files_count == 1
    assert freed_bytes == 4


def test_clear_directory_removes_stale_task_dir(temp_root: Path) -> None:
    task_dir = make_task_dir(temp_root)
    (task_dir / "orphan.mp4").write_bytes(b"12345")
    backdate(task_dir)
    files_count, freed_bytes = clear_directory(temp_root)
    assert not task_dir.exists()
    assert temp_root.is_dir()
    # glob("**/*") never matches dotfiles, so .lock is not counted
    assert files_count == 1
    assert freed_bytes == 5
