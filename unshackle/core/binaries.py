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


FFMPEG = find("ffmpeg")
FFProbe = find("ffprobe")
FFPlay = find("ffplay")
# seconv = SubtitleEdit 5+ CLI (the 5.0 GUI has no batch mode); SubtitleEdit = 4.x
SubtitleEdit = find("seconv", "SubtitleEdit")
ShakaPackager = find(
    "shaka-packager",
    "packager",
    f"packager-{__shaka_platform}",
    f"packager-{__shaka_platform}-arm64",
    f"packager-{__shaka_platform}-x64",
)
CCExtractor = find("ccextractor", "ccextractorwin", "ccextractorwinfull")
HolaProxy = find("hola-proxy")
MPV = find("mpv")
Caddy = find("caddy")
MKVToolNix = find("mkvmerge")
Mkvpropedit = find("mkvpropedit")
DoviTool = find("dovi_tool")
HDR10PlusTool = find("hdr10plus_tool", "HDR10Plus_tool")
Mp4decrypt = find("mp4decrypt")
Docker = find("docker")
ML_Worker = find("ML-Worker")


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
    "find",
)
