from __future__ import annotations

import logging
import re
import shutil
import subprocess  # nosec B404 - git is resolved via binaries.find, args are composed locally
import time
from pathlib import Path
from typing import Optional

from unshackle.core import binaries

log = logging.getLogger("services")


class DirtyServiceRepo(Exception):
    """A clone has uncommitted or unpushed local changes; refresh is refused to protect them."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(str(path))

DEFAULT_TTL = 24 * 60 * 60  # refresh at most once a day (mirrors UpdateChecker)
STAMP_SUFFIX = ".fetch"  # last-fetch unix time, kept beside (not inside) the clone


def stamp_for(dest: Path) -> Path:
    """Fetch-timestamp file path, kept OUTSIDE the clone so it never shows up in `git status`."""
    return dest.parent / f"{dest.name}{STAMP_SUFFIX}"

# owner/repo shorthand: exactly two path-like segments, no scheme
SHORTHAND_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def is_repo_spec(value: object) -> bool:
    """True if value is a git repo spec (URL or owner/repo shorthand), not a local path."""
    if not isinstance(value, str):
        return False
    if value.startswith(("http://", "https://", "ssh://", "git@")) or value.endswith(".git"):
        return True
    # owner/repo shorthand, but only if there's no local dir by that name (paths win)
    base = value.split("@", 1)[0]
    return bool(SHORTHAND_RE.match(base)) and not Path(value).expanduser().exists()


def parse_spec(spec: str) -> tuple[str, Optional[str], str]:
    """Return (clone_url, branch, dest_name). `@branch` is honored for http/shorthand, not ssh."""
    is_ssh = spec.startswith(("git@", "ssh://"))
    branch: Optional[str] = None
    url = spec
    if not is_ssh and "@" in spec:
        url, branch = spec.rsplit("@", 1)
    if SHORTHAND_RE.match(url) and not url.startswith(("http", "ssh", "git@")):
        url = f"https://github.com/{url}"
    return url, branch, _slug(url)


def _slug(url: str) -> str:
    """Filesystem-safe clone dir name from a repo URL, host included so same owner/repo on
    different hosts don't collide, e.g. github.com__owner__repo."""
    cleaned = re.sub(r"^[a-z]+://", "", url).split("@")[-1].replace(":", "/")
    cleaned = re.sub(r"\.git$", "", cleaned)
    return "__".join(p for p in cleaned.split("/") if p)


def repos_base() -> Path:
    """Where clones live: <first configured local services dir>/_repos (else the default)."""
    from unshackle.core.config import Config, config  # lazy: avoid config <-> service_repo import cycle

    entries = config.directories.services
    if not isinstance(entries, list):
        entries = [entries]
    for entry in entries:
        if not (isinstance(entry, str) and is_repo_spec(entry)):
            return Path(entry).expanduser() / "_repos"
    return Config._Directories.namespace_dir / "services" / "_repos"


def resolve_service_repo(spec: str, *, ttl: int = DEFAULT_TTL, force: bool = False) -> Optional[Path]:
    """Resolve a repo spec to its local clone dir.

    First use clones it. ``force=True`` (manual ``refresh-services``) hard-resets the clone to
    upstream, discarding local changes. Otherwise a TTL-stale clone is fast-forwarded, but a dirty
    clone raises ``DirtyServiceRepo`` so the automated dl/search path can warn and exit instead of
    clobbering in-progress edits.
    """
    url, branch, dest_name = parse_spec(spec)
    dest = repos_base() / dest_name
    stamp = stamp_for(dest)

    if not dest.exists():
        return _clone(url, branch, dest, stamp)
    if force:
        _force_sync(dest, stamp)
    elif _is_stale(stamp, ttl):
        _pull(dest, stamp)
    return dest  # ponytail: git IS the cache layer — no bespoke fetch/integrity code


def _clone(url: str, branch: Optional[str], dest: Path, stamp: Path) -> Optional[Path]:
    if not binaries.Git:
        log.error("git not found on PATH — cannot fetch service repo %s", url)
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [str(binaries.Git), "clone", "--depth", "1"]
    if branch:
        args += ["-b", branch]
    args += [url, str(dest)]
    try:
        subprocess.run(args, check=True, capture_output=True)  # nosec B603
    except subprocess.CalledProcessError as e:
        log.error("failed to clone service repo %s: %s", url, _stderr(e))
        shutil.rmtree(dest, ignore_errors=True)  # don't leave a half-cloned dir that sticks on retries
        return None
    _write_stamp(stamp)
    log.info("cloned service repo %s", url)
    return dest


def _pull(dest: Path, stamp: Path) -> None:
    if not binaries.Git:
        _write_stamp(stamp)
        return
    # never clobber local edits — refuse to refresh a dirty clone and let the caller exit cleanly
    if _is_dirty(dest):
        raise DirtyServiceRepo(dest)
    try:
        subprocess.run(
            [str(binaries.Git), "-C", str(dest), "pull", "--ff-only"], check=True, capture_output=True
        )  # nosec B603
    except subprocess.CalledProcessError as e:
        log.warning("could not update service repo at %s, using existing copy: %s", dest, _stderr(e))
    _write_stamp(stamp)  # stamp regardless so a flaky remote can't trigger a fetch every run (force overrides)


def refresh_repo(spec: str) -> tuple[Optional[Path], list[str]]:
    """Force-sync a repo to upstream and return (clone dir, git-style per-service change lines).

    Used by the manual ``refresh-services`` command. Change lines look like ``+ TAG (added)`` /
    ``~ TAG (modified)`` / ``- TAG (removed)`` plus a shortstat; empty list means already up to date.
    """
    url, branch, dest_name = parse_spec(spec)
    dest = repos_base() / dest_name
    stamp = stamp_for(dest)
    if not dest.exists():
        return _clone(url, branch, dest, stamp), ["cloned (new)"]
    if not binaries.Git:
        return dest, []
    old = _head(dest)
    discarded = _local_dirty_services(dest)  # uncommitted edits the force reset is about to wipe
    _force_sync(dest, stamp)
    lines = [f"! {tag} (local changes discarded)" for tag in discarded]
    lines += _changed_services(dest, old, _head(dest))  # upstream changes pulled in
    return dest, lines


def _head(dest: Path) -> Optional[str]:
    r = subprocess.run([str(binaries.Git), "-C", str(dest), "rev-parse", "HEAD"], capture_output=True)  # nosec B603
    return r.stdout.decode(errors="ignore").strip() if r.returncode == 0 else None


def _local_dirty_services(dest: Path) -> list[str]:
    """Top-level service dirs with uncommitted edits to tracked files (lost on a force reset)."""
    r = subprocess.run([str(binaries.Git), "-C", str(dest), "diff", "--name-only", "HEAD"], capture_output=True)  # nosec B603
    if r.returncode != 0:
        return []
    return sorted({line.split("/")[0] for line in r.stdout.decode(errors="ignore").splitlines() if line})


def _changed_services(dest: Path, old: Optional[str], new: Optional[str]) -> list[str]:
    """Summarise a HEAD old->new diff grouped by top-level service dir, plus a line shortstat."""
    if not old or not new or old == new:
        return []
    git = str(binaries.Git)
    name_status = subprocess.run(
        [git, "-C", str(dest), "diff", "--name-status", f"{old}..{new}"], capture_output=True
    )  # nosec B603
    by_dir: dict[str, set[str]] = {}
    if name_status.returncode == 0:
        for raw in name_status.stdout.decode(errors="ignore").splitlines():
            parts = raw.split("\t")
            if len(parts) < 2:
                continue
            by_dir.setdefault(parts[-1].split("/")[0], set()).add(parts[0][0])  # parts[-1]=new path (handles renames)
    sym = {"added": "+", "modified": "~", "removed": "-"}
    lines = []
    for top in sorted(by_dir):
        codes = by_dir[top]
        kind = "added" if codes == {"A"} else "removed" if codes == {"D"} else "modified"
        lines.append(f"{sym[kind]} {top} ({kind})")
    short = subprocess.run([git, "-C", str(dest), "diff", "--shortstat", f"{old}..{new}"], capture_output=True)  # nosec B603
    if short.returncode == 0 and short.stdout.strip():
        lines.append(short.stdout.decode(errors="ignore").strip())
    return lines


def _force_sync(dest: Path, stamp: Path) -> None:
    """Hard-reset the clone to its upstream, discarding local changes (manual refresh only)."""
    if not binaries.Git:
        _write_stamp(stamp)
        return
    git = str(binaries.Git)
    try:
        subprocess.run([git, "-C", str(dest), "fetch", "--depth", "1", "origin"], check=True, capture_output=True)  # nosec B603
        subprocess.run([git, "-C", str(dest), "reset", "--hard", "@{u}"], check=True, capture_output=True)  # nosec B603
    except subprocess.CalledProcessError as e:
        log.warning("could not force-refresh service repo at %s, using existing copy: %s", dest, _stderr(e))
    _write_stamp(stamp)


def _is_dirty(dest: Path) -> bool:
    """True if the clone has uncommitted edits to tracked files or local commits not on its upstream.

    Untracked files are intentionally ignored: a `git pull --ff-only` only touches tracked files, so
    untracked content (including `__pycache__` that importing the services creates) is never at risk
    and must not falsely flag the clone as dirty.
    """
    git = str(binaries.Git)
    # modifications to tracked files only (--untracked-files=no skips __pycache__ etc.)
    status = subprocess.run(
        [git, "-C", str(dest), "status", "--porcelain", "--untracked-files=no"], capture_output=True
    )  # nosec B603
    if status.returncode == 0 and status.stdout.strip():
        return True
    # local commits ahead of upstream (committed but not pushed); no upstream → treat as clean
    ahead = subprocess.run(
        [git, "-C", str(dest), "rev-list", "--count", "@{u}..HEAD"], capture_output=True
    )  # nosec B603
    if ahead.returncode == 0:
        try:
            return int(ahead.stdout.strip() or b"0") > 0
        except ValueError:
            return False
    return False


def _is_stale(stamp: Path, ttl: int) -> bool:
    try:
        return (time.time() - float(stamp.read_text().strip())) >= ttl
    except (OSError, ValueError):
        return True


def _write_stamp(stamp: Path) -> None:
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(time.time()))
    except OSError:
        pass


def _stderr(e: subprocess.CalledProcessError) -> str:
    return (e.stderr or b"").decode(errors="ignore").strip()
