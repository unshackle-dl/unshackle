from __future__ import annotations

import contextlib
import functools
import logging
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional, ParamSpec, TypeVar

from filelock import FileLock, Timeout

from unshackle.core.config import config

TASK_PREFIX = "task_"
LOCK_NAME = ".lock"
STALE_GRACE = 60.0
RESUME_STASH = "resume"
STASH_MAX_AGE = 7 * 24 * 3600

log = logging.getLogger("temp")

P = ParamSpec("P")
R = TypeVar("R")


def is_stale(task_dir: Path) -> bool:
    """A task dir is stale when nothing holds its lock and it is past the setup grace."""
    try:
        if time.time() - task_dir.stat().st_mtime < STALE_GRACE:
            return False
    except OSError:
        return False
    lock_path = task_dir / LOCK_NAME
    if not lock_path.exists():
        return True
    lock = FileLock(lock_path, timeout=0)
    try:
        lock.acquire()
    except (Timeout, OSError):
        return False
    lock.release()
    return True


def sweep_task_dirs(root: Path) -> None:
    """Remove task dirs left by processes that died without cleaning up, and resume stash entries past their age."""
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry.is_dir() and not entry.is_symlink() and entry.name.startswith(TASK_PREFIX) and is_stale(entry):
            shutil.rmtree(entry, ignore_errors=True)

    stash = root / RESUME_STASH
    if stash.is_dir():
        cutoff = time.time() - STASH_MAX_AGE
        for entry in stash.iterdir():
            try:
                if entry.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)


def resume_stash() -> Optional[Path]:
    """The dir beside the task dirs that keeps a failed track's segments for the next run.

    None outside a task dir: there config.directories.temp itself outlives the run.
    """
    temp = config.directories.temp
    if not temp.name.startswith(TASK_PREFIX):
        return None
    return temp.parent / RESUME_STASH


def _move_with_sidecar(source: Path, target: Path) -> bool:
    """Rename a segment dir, then its resume sidecar. False if the dir rename fails; a sidecar failure only logs."""
    from unshackle.core.tracks import resume

    try:
        source.rename(target)
    except OSError as e:
        log.debug("Could not move %s: %s", source, e)
        return False
    sidecar = resume.sidecar_path(source)
    try:
        if sidecar.exists():
            sidecar.replace(resume.sidecar_path(target))
    except OSError as e:
        log.debug("Could not move %s: %s", sidecar, e)
    return True


def stash_segments(save_dir: Path) -> None:
    """Move save_dir and its resume sidecar into the stash. Leave them in place on any OS error."""
    from unshackle.core.tracks import resume

    stash = resume_stash()
    if stash is None or not save_dir.is_dir():
        return
    target = stash / save_dir.name
    try:
        stash.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return
    except OSError as e:
        log.debug("Could not stash %s: %s", save_dir, e)
        return
    if _move_with_sidecar(save_dir, target):
        # rename keeps the old mtime, and the sweep ages entries by it
        for path in (target, resume.sidecar_path(target)):
            with contextlib.suppress(OSError):
                os.utime(path)


def restore_segments(save_dir: Path) -> None:
    """Move a stashed save_dir and its sidecar back to save_dir, if the stash has them and save_dir is free."""
    stash = resume_stash()
    if stash is None:
        return
    source = stash / save_dir.name
    if source.is_symlink() or not source.is_dir() or save_dir.exists():
        return
    _move_with_sidecar(source, save_dir)


@contextmanager
def task_temp_dir(task_id: Optional[str] = None) -> Iterator[Path]:
    """Point config.directories.temp at a private dir for this task. Remove it on any exit."""
    root = config.directories.temp
    root.mkdir(parents=True, exist_ok=True)
    sweep_task_dirs(root)

    task_dir = root / f"{TASK_PREFIX}{task_id or uuid.uuid4().hex[:12]}"
    task_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(task_dir / LOCK_NAME)
    lock.acquire()
    config.directories.temp = task_dir
    try:
        yield task_dir
    finally:
        config.directories.temp = root
        lock.release()
        shutil.rmtree(task_dir, ignore_errors=True)


def with_task_temp(fn: Callable[P, R]) -> Callable[P, R]:
    """Call the wrapped callable inside its own task temp dir."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with task_temp_dir():
            return fn(*args, **kwargs)

    return wrapper
