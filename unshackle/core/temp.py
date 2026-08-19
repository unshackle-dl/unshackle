from __future__ import annotations

import functools
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
    """Remove task dirs left by processes that died without cleaning up."""
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry.is_dir() and not entry.is_symlink() and entry.name.startswith(TASK_PREFIX) and is_stale(entry):
            shutil.rmtree(entry, ignore_errors=True)


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
