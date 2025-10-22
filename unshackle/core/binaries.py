import shutil
import sys
from pathlib import Path
from typing import Optional

from mypy.types import names

__shaka_platform = {"win32": "win", "darwin": "osx"}.get(sys.platform, sys.platform)


def find(*names: str) -> Optional[Path]:
    """Find the path of the first found binary name."""
    current_dir = Path(__file__).resolve().parent.parent
    local_binaries_dir = current_dir / "binaries"

    ext = ".exe" if sys.platform == "win32" else ""

    for name in names:
        if local_binaries_dir.exists():
            candidate_paths = [
                local_binaries_dir / f"{name}{ext}",
                local_binaries_dir / name / f"{name}{ext}"
            ]

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
SubtitleEdit = find("SubtitleEdit")
ShakaPackager = find(
    "shaka-packager",
    "packager",
    f"packager-{__shaka_platform}",
    f"packager-{__shaka_platform}-arm64",
    f"packager-{__shaka_platform}-x64",
)
Aria2 = find("aria2c", "aria2")
CCExtractor = find("ccextractor", "ccextractorwin", "ccextractorwinfull")
HolaProxy = find("hola-proxy")
MPV = find("mpv")
Caddy = find("caddy")
N_m3u8DL_RE = find("N_m3u8DL-RE", "n-m3u8dl-re")
MKVToolNix = find("mkvmerge")
Mkvpropedit = find("mkvpropedit")
DoviTool = find("dovi_tool")
HDR10PlusTool = find("hdr10plus_tool", "HDR10Plus_tool")
Mp4decrypt = find("mp4decrypt")


__all__ = (
    "FFMPEG",
    "FFProbe",
    "FFPlay",
    "SubtitleEdit",
    "ShakaPackager",
    "Aria2",
    "CCExtractor",
    "HolaProxy",
    "MPV",
    "Caddy",
    "N_m3u8DL_RE",
    "MKVToolNix",
    "Mkvpropedit",
    "DoviTool",
    "HDR10PlusTool",
    "Mp4decrypt",
    "find",
)
