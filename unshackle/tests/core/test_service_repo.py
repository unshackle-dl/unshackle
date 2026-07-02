"""Unit tests for remote git service-repo resolution. No network, no real git."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from unshackle.core import service_repo


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://github.com/owner/repo", True),
        ("http://example.com/me/svc", True),
        ("git@gitlab.com:me/my-services.git", True),
        ("ssh://git@host/me/svc", True),
        ("me/svc.git", True),
        ("owner/repo", True),
        ("owner/repo@main", True),
        ("/abs/local/path", False),
        ("~/my-local-services", False),
        (123, False),
    ],
)
def test_is_repo_spec(value, expected):
    assert service_repo.is_repo_spec(value) is expected


def test_is_repo_spec_existing_path_wins(tmp_path):
    # an owner/repo-looking value that exists locally is a path, not a repo
    local = tmp_path / "owner" / "repo"
    local.mkdir(parents=True)
    assert service_repo.is_repo_spec(str(local)) is False


@pytest.mark.parametrize(
    "spec, url, branch, dest",
    [
        ("owner/repo@main", "https://github.com/owner/repo", "main", "github.com__owner__repo"),
        ("owner/repo", "https://github.com/owner/repo", None, "github.com__owner__repo"),
        ("git@gitlab.com:me/my-services.git", "git@gitlab.com:me/my-services.git", None, "gitlab.com__me__my-services"),
        ("https://github.com/a/b", "https://github.com/a/b", None, "github.com__a__b"),
    ],
)
def test_parse_spec(spec, url, branch, dest):
    assert service_repo.parse_spec(spec) == (url, branch, dest)


def test_slug_distinguishes_hosts():
    # same owner/repo on different hosts must not share a clone dir
    a = service_repo.parse_spec("https://github.com/owner/repo")[2]
    b = service_repo.parse_spec("https://gitlab.com/owner/repo")[2]
    assert a != b


def test_repos_base_uses_configured_local_dir(monkeypatch, tmp_path):
    from unshackle.core.config import config

    monkeypatch.setattr(config.directories, "services", ["owner/repo", str(tmp_path / "svc")], raising=False)
    assert service_repo.repos_base() == (tmp_path / "svc" / "_repos")


def test_is_stale(tmp_path):
    stamp = tmp_path / "stamp"
    assert service_repo._is_stale(stamp, ttl=10) is True  # missing → stale
    stamp.write_text(str(time.time()))
    assert service_repo._is_stale(stamp, ttl=10) is False  # fresh
    stamp.write_text(str(time.time() - 100))
    assert service_repo._is_stale(stamp, ttl=10) is True  # old


def test_clone_invokes_git(monkeypatch, tmp_path):
    monkeypatch.setattr(service_repo.binaries, "Git", "git")
    monkeypatch.setattr(service_repo, "repos_base", lambda: tmp_path)

    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        (tmp_path / "github.com__owner__repo").mkdir(parents=True, exist_ok=True)  # simulate clone output
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(service_repo.subprocess, "run", fake_run)

    dest = service_repo.resolve_service_repo("owner/repo@main")
    assert dest == tmp_path / "github.com__owner__repo"
    assert calls[0][:4] == ["git", "clone", "--depth", "1"]
    assert "-b" in calls[0] and "main" in calls[0]
    assert service_repo.stamp_for(dest).exists()  # stamp written (beside the clone, not inside)


def test_clone_missing_git_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(service_repo.binaries, "Git", None)
    monkeypatch.setattr(service_repo, "repos_base", lambda: tmp_path)
    assert service_repo.resolve_service_repo("a/b") is None


def test_fresh_clone_skips_pull(monkeypatch, tmp_path):
    dest = tmp_path / "github.com__a__b"
    dest.mkdir(parents=True)
    service_repo.stamp_for(dest).write_text(str(time.time()))
    monkeypatch.setattr(service_repo, "repos_base", lambda: tmp_path)
    monkeypatch.setattr(
        service_repo.subprocess, "run", lambda *a, **k: pytest.fail("git should not run for a fresh clone")
    )
    assert service_repo.resolve_service_repo("a/b") == dest


def _git_stub(porcelain=b"", ahead=b"0"):
    """Fake subprocess.run for git: dirty checks return given output, pull succeeds."""

    def run(args, **kw):
        sub = args[3] if len(args) > 3 else ""
        if sub == "status":
            return subprocess.CompletedProcess(args, 0, porcelain, b"")
        if sub == "rev-list":
            return subprocess.CompletedProcess(args, 0, ahead, b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")  # pull

    return run


def test_is_dirty_uncommitted(monkeypatch):
    monkeypatch.setattr(service_repo.binaries, "Git", "git")
    monkeypatch.setattr(service_repo.subprocess, "run", _git_stub(porcelain=b" M EXAMPLE/__init__.py\n"))
    assert service_repo._is_dirty(Path("/x")) is True


def test_is_dirty_commits_ahead(monkeypatch):
    monkeypatch.setattr(service_repo.binaries, "Git", "git")
    monkeypatch.setattr(service_repo.subprocess, "run", _git_stub(porcelain=b"", ahead=b"2"))
    assert service_repo._is_dirty(Path("/x")) is True


def test_is_dirty_clean(monkeypatch):
    monkeypatch.setattr(service_repo.binaries, "Git", "git")
    monkeypatch.setattr(service_repo.subprocess, "run", _git_stub(porcelain=b"", ahead=b"0"))
    assert service_repo._is_dirty(Path("/x")) is False


def test_is_dirty_ignores_untracked_files(monkeypatch):
    # status must ask git to skip untracked files so importing services (__pycache__) isn't "dirty"
    seen = []
    monkeypatch.setattr(service_repo.binaries, "Git", "git")

    def run(args, **kw):
        seen.append(args)
        out = b"0" if args[3] == "rev-list" else b""
        return subprocess.CompletedProcess(args, 0, out, b"")

    monkeypatch.setattr(service_repo.subprocess, "run", run)
    service_repo._is_dirty(Path("/x"))
    status_args = next(a for a in seen if a[3] == "status")
    assert "--untracked-files=no" in status_args


def test_auto_refresh_refuses_dirty_clone(monkeypatch, tmp_path):
    """Auto path (force=False): a stale clone with local edits raises instead of clobbering."""
    dest = tmp_path / "github.com__a__b"
    dest.mkdir(parents=True)
    service_repo.stamp_for(dest).write_text("0")  # stale → refresh due
    monkeypatch.setattr(service_repo, "repos_base", lambda: tmp_path)
    monkeypatch.setattr(service_repo.binaries, "Git", "git")
    monkeypatch.setattr(service_repo.subprocess, "run", _git_stub(porcelain=b" M f\n"))
    with pytest.raises(service_repo.DirtyServiceRepo) as ei:
        service_repo.resolve_service_repo("a/b")
    assert ei.value.path == dest


def test_manual_refresh_force_overwrites(monkeypatch, tmp_path):
    """Manual path (force=True): hard-reset to upstream, never raises on a dirty clone."""
    dest = tmp_path / "github.com__a__b"
    dest.mkdir(parents=True)
    monkeypatch.setattr(service_repo, "repos_base", lambda: tmp_path)
    monkeypatch.setattr(service_repo.binaries, "Git", "git")
    calls = []
    monkeypatch.setattr(
        service_repo.subprocess,
        "run",
        lambda args, **kw: (calls.append(args[3]), subprocess.CompletedProcess(args, 0, b"", b""))[1],
    )
    assert service_repo.resolve_service_repo("a/b", force=True) == dest
    assert "fetch" in calls and "reset" in calls  # force-sync ran
    assert "status" not in calls  # no dirty gate on the force path


def test_changed_services_groups_by_top_dir(monkeypatch):
    monkeypatch.setattr(service_repo.binaries, "Git", "git")

    def run(args, **kw):
        if "--name-status" in args:
            out = b"A\tNEW/__init__.py\nM\tOLD/__init__.py\nM\tOLD/config.yaml\nD\tGONE/__init__.py\n"
        elif "--shortstat" in args:
            out = b" 4 files changed, 10 insertions(+), 2 deletions(-)\n"
        else:
            out = b""
        return subprocess.CompletedProcess(args, 0, out, b"")

    monkeypatch.setattr(service_repo.subprocess, "run", run)
    lines = service_repo._changed_services(Path("/x"), "aaa", "bbb")
    assert "+ NEW (added)" in lines
    assert "~ OLD (modified)" in lines
    assert "- GONE (removed)" in lines
    assert any("4 files changed" in line for line in lines)


def test_local_dirty_services_lists_edited_dirs(monkeypatch):
    monkeypatch.setattr(service_repo.binaries, "Git", "git")
    monkeypatch.setattr(
        service_repo.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, b"CR/__init__.py\nCR/config.yaml\nNF/__init__.py\n", b""),
    )
    assert service_repo._local_dirty_services(Path("/x")) == ["CR", "NF"]


def test_refresh_reports_discarded_local_edits(monkeypatch, tmp_path):
    """Force refresh with no upstream advance still reports the local edits it wiped."""
    dest = tmp_path / "github.com__a__b"
    dest.mkdir(parents=True)
    monkeypatch.setattr(service_repo, "repos_base", lambda: tmp_path)
    monkeypatch.setattr(service_repo.binaries, "Git", "git")

    def run(args, **kw):
        if "diff" in args and "--name-only" in args:
            return subprocess.CompletedProcess(args, 0, b"CR/__init__.py\n", b"")  # local edit present
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, b"samehead\n", b"")  # upstream did not advance
        return subprocess.CompletedProcess(args, 0, b"", b"")  # fetch/reset/diff

    monkeypatch.setattr(service_repo.subprocess, "run", run)
    _, changes = service_repo.refresh_repo("a/b")
    assert changes == ["! CR (local changes discarded)"]


def test_changed_services_up_to_date(monkeypatch):
    monkeypatch.setattr(service_repo.binaries, "Git", "git")
    # same HEAD before/after → no diff computed at all
    monkeypatch.setattr(
        service_repo.subprocess, "run", lambda *a, **k: pytest.fail("no git diff when HEAD unchanged")
    )
    assert service_repo._changed_services(Path("/x"), "same", "same") == []


@pytest.mark.parametrize("order", [["a", "b"], ["b", "a"]])
def test_collision_first_source_wins(tmp_path, order):
    """Discovery dedupes by tag honoring list order — the FIRST source to define a tag wins,
    whether it's a local dir or a repo clone; later duplicates are shadowed."""
    dirs = {}
    for name in ("a", "b"):
        svc = tmp_path / name / "EXAMPLE"
        svc.mkdir(parents=True)
        (svc / "__init__.py").touch()
        dirs[name] = tmp_path / name

    seen: dict = {}
    shadowed: list = []
    for name in order:  # services.py preserves config order
        for path in dirs[name].glob("*/__init__.py"):
            tag = path.parent.stem
            if tag in seen:
                shadowed.append((tag, path, seen[tag]))
            else:
                seen[tag] = path

    winner, loser = order[0], order[1]
    assert seen["EXAMPLE"] == dirs[winner] / "EXAMPLE" / "__init__.py"
    assert shadowed and shadowed[0][1] == dirs[loser] / "EXAMPLE" / "__init__.py"
