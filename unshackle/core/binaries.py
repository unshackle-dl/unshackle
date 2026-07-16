import shutil
import sys
from pathlib import Path
from typing import Optional

__shaka_platform = {"win32": "win", "darwin": "osx"}.get(sys.platform, sys.platform)


def find(*names: str) -> Optional[Path]:
    """Find the path of the first found binary name."""
    current_dir = Path(__file__).resolve().parent.parent
    local_binaries_dir = current_dir / "binaries"

    ext = ".exe" if sys.platform == "win32" else ""

    for name in names:
        if local_binaries_dir.exists():
            candidate_paths = [local_binaries_dir / f"{name}{ext}", local_binaries_dir / name / f"{name}{ext}"]

            for subdir in local_binaries_dir.iterdir():
                if subdir.is_dir():
                    candidate_paths.append(subdir / f"{name}{ext}")

            for path in candidate_paths:
                if path.is_file():
                    # On Unix-like systems, check if file is executable
                    if sys.platform == "win32" or (path.stat().st_mode & 0o111):
                        return path

        # Fall back to system PATH
        path = shutil.which(name)
        if path:
            return Path(path)
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
    "ML_Worker": ("ML-Worker",),
    "Git": ("git",),
}


def __getattr__(name: str) -> Optional[Path]:
    candidates = __binaries.get(name)
    if candidates is None:
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
    "ML_Worker",
    "Git",
    "find",
)
