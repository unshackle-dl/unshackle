import os
import re
import shutil
import signal
import subprocess  # nosec B404
import sys
from contextlib import suppress
from pathlib import Path
from typing import Optional

from appdirs import user_data_dir

__shaka_platform = {"win32": "win", "darwin": "osx"}.get(sys.platform, sys.platform)

package_binaries_dir = Path(__file__).resolve().parent.parent / "binaries"
user_binaries_dir = Path(user_data_dir("unshackle", False)) / "binaries"


def find(*names: str, search_dirs: Optional[list[Path]] = None) -> Optional[Path]:
    """Find the path of the first found binary name."""
    services_dir = package_binaries_dir.parent / "services"

    dirs_to_check: list[Path] = [package_binaries_dir, user_binaries_dir]
    if services_dir.exists():
        for s_dir in services_dir.iterdir():
            s_bin = s_dir / "binaries"
            if s_bin.is_dir():
                dirs_to_check.append(s_bin)

    if search_dirs:
        dirs_to_check.extend(search_dirs)

    ext = ".exe" if sys.platform == "win32" else ""

    for name in names:
        for base_dir in dirs_to_check:
            if not base_dir.exists():
                continue
            candidate_paths = [base_dir / f"{name}{ext}", base_dir / name / f"{name}{ext}"]

            for subdir in base_dir.iterdir():
                if subdir.is_dir():
                    candidate_paths.append(subdir / f"{name}{ext}")

            for path in candidate_paths:
                if path.is_file():
                    # On Unix-like systems, check if file is executable
                    if sys.platform == "win32" or (path.stat().st_mode & 0o111):
                        return path

        # Fall back to system PATH
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


# Binary attribute -> candidate names passed to find(). Resolved lazily on first
# attribute access (see __getattr__) to avoid ~500ms of shutil.which PATH scans at import.
__binaries = {
    "FFMPEG": ("ffmpeg",),
    "FFProbe": ("ffprobe",),
    "FFPlay": ("ffplay",),
    # seconv = SubtitleEdit 5+ CLI (the 5.0 GUI has no batch mode); SubtitleEdit = 4.x
    "SubtitleEdit": ("seconv", "SubtitleEdit"),
    "ShakaPackager": (
        "shaka-packager",
        "packager",
        f"packager-{__shaka_platform}",
        f"packager-{__shaka_platform}-arm64",
        f"packager-{__shaka_platform}-x64",
    ),
    "CCExtractor": ("ccextractor", "ccextractorwin", "ccextractorwinfull"),
    "HolaProxy": ("hola-proxy",),
    "MPV": ("mpv",),
    "Caddy": ("caddy",),
    "MKVToolNix": ("mkvmerge",),
    "Mkvpropedit": ("mkvpropedit",),
    "DoviTool": ("dovi_tool",),
    "HDR10PlusTool": ("hdr10plus_tool", "HDR10Plus_tool"),
    "Mp4decrypt": ("mp4decrypt",),
    "Docker": ("docker",),
    "Git": ("git",),
}

_registered_binaries: dict[str, dict] = {}


def register(
    attr_name: str,
    *candidates: str,
    desc: str = "Custom tool",
) -> None:
    """Register a custom binary dynamically."""
    file_candidates = candidates if candidates else (attr_name.lower(),)
    __binaries[attr_name] = file_candidates
    _registered_binaries[attr_name] = {
        "name": attr_name,
        "attr": attr_name,
        "desc": desc,
    }


def get_registered_dependencies() -> list[dict]:
    """Return all registered custom binary dependencies for env check."""
    return list(_registered_binaries.values())


DOTTED_NUMBER = r"\d+\.\d+(?:\.\d+)*"
VERSION_RE = re.compile(DOTTED_NUMBER)
LABELLED_VERSION_RE = re.compile(rf"(?i)\bversion[:\s]+[a-z]?({DOTTED_NUMBER})")
GIT_BUILD_RE = re.compile(r"\bversion\s+(?:N-\d+-g[0-9a-f]+-(\d{8})\b|(?:git-)?(\d{4}-\d{2}-\d{2})\b|(N-\d+)-g)")
VERSION_WORD_RE = re.compile(r"(?i)\bversion\b")
BARE_VERSION_TOOLS = {"mp4decrypt"}
PROBE_TIMEOUT = 5.0


def parse_version(text: str) -> Optional[str]:
    """Return the version from the first line that says "version" or holds a dotted number.

    That line is the tool's own banner; later lines carry library and compiler versions.
    A git build of FFmpeg has no release number, so its build date (YYYYMMDD) stands in, or N-<revision> when undated.
    """
    for line in text.splitlines():
        if match := LABELLED_VERSION_RE.search(line):
            return match.group(1)
        if match := GIT_BUILD_RE.search(line):
            date, dashed_date, revision = match.groups()
            return revision or (date or dashed_date).replace("-", "")
        if VERSION_WORD_RE.search(line):
            return None
        if match := VERSION_RE.search(line):
            return match.group(0)
    return None


def probe_output(path: Path, args: list[str]) -> Optional[str]:
    """Run a binary and return its stdout and stderr, or None when it does not start or times out.

    A timeout kills the whole tree: on Windows subprocess.run waits with no limit for a grandchild that holds the pipes.
    """
    try:
        proc = subprocess.Popen(  # nosec B603
            [str(path), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except (OSError, ValueError):
        return None
    with proc:
        try:
            out, err = proc.communicate(timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)  # nosec B603 B607
            else:
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            with suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=1)
            return None
    return f"{out}\n{err}"


def binary_version(path: Optional[Path]) -> Optional[str]:
    """Best-effort version probe of a binary. Returns None when nothing is parseable.

    BARE_VERSION_TOOLS run with no arguments because they print their version only in the usage text.
    """
    if not path:
        return None
    bare = path.stem.lower() in BARE_VERSION_TOOLS
    for args in [[]] if bare else [["--version"], ["-version"]]:
        output = probe_output(path, args)
        if output is None:
            return None
        if version := parse_version(output):
            return version
    return None


_services_loaded = False


def __getattr__(name: str) -> Optional[Path]:
    global _services_loaded
    candidates = __binaries.get(name)
    if candidates is None and not _services_loaded:
        _services_loaded = True
        try:
            candidates = __binaries.get(name)
        except Exception:
            pass

    if candidates is None:
        # Fallback to direct name and lowercase candidate
        resolved = find(name, name.lower())
        if resolved is not None:
            globals()[name] = resolved
            return resolved
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    # Cache the result (including None for an absent binary) so later access is a plain attr hit.
    resolved = find(*candidates)
    globals()[name] = resolved
    return resolved


__all__ = (
    "FFMPEG",
    "FFProbe",
    "FFPlay",
    "SubtitleEdit",
    "ShakaPackager",
    "CCExtractor",
    "HolaProxy",
    "MPV",
    "Caddy",
    "MKVToolNix",
    "Mkvpropedit",
    "DoviTool",
    "HDR10PlusTool",
    "Mp4decrypt",
    "Docker",
    "Git",
    "find",
    "register",
    "get_registered_dependencies",
)
