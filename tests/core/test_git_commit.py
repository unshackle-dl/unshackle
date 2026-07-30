"""Unit tests for the git commit probe behind the banner and the /api/health `commit` field.
The probe runs at import time, so it must always return a string and must never raise, whatever
state git and the checkout are in."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import unshackle.core
from unshackle.core import _git_commit

pytestmark = pytest.mark.unit

# Without a .git the probe returns before it runs git, so the subprocess stubs below prove nothing.
needs_checkout = pytest.mark.skipif(
    not (Path(unshackle.core.__file__).parents[2] / ".git").exists(), reason="not running from a git checkout"
)


def test_returns_str_in_this_checkout() -> None:
    assert isinstance(_git_commit(), str)


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("git"),
        PermissionError("git"),
        subprocess.TimeoutExpired(cmd="git", timeout=2),
        subprocess.SubprocessError("boom"),
    ],
)
@needs_checkout
def test_swallows_subprocess_errors(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def raise_it(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", raise_it)
    assert _git_commit() == ""


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (0, "a24c9b9\n", "a24c9b9"),
        (128, "", ""),  # empty repo, corrupt repo, or a stale worktree pointer
        (0, "", ""),  # no output despite success
    ],
)
@needs_checkout
def test_maps_git_result(monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, expected: str) -> None:
    completed = subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="noise")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed)
    assert _git_commit() == expected
