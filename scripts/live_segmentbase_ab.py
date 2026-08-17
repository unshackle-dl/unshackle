"""Live A/B test for SegmentBase (byte-range) DASH downloads.

Drives the real DASH pipeline against public SegmentBase test manifests and reports
wall time, size, throughput and output validity (ffprobe). On branches that have the
single-URL collapse (DASH.collapsible_single_url) it also reports whether the
collapse fired; on checkouts without it (e.g. dev) the same script runs the classic
per-segment + merge path, so running it from both checkouts gives an A/B comparison:

    uv run python scripts/live_segmentbase_ab.py --track largest
    /path/to/dev/.venv/bin/python scripts/live_segmentbase_ab.py --track largest

Throwaway-ish operational tool: it makes real network requests to public test assets.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Optional

import click
from requests import Session

from unshackle.core.manifests import DASH
from unshackle.core.tracks.track import DownloadContext

# (label, SegmentBase MPD). netflix-1a is collapse-eligible (Initialization range from
# byte 0); sony-1a has no Initialization range so every checkout takes the classic path.
STREAMS: list[tuple[str, str]] = [
    ("netflix-1a", "https://dash.akamaized.net/dash264/TestCases/1a/netflix/exMPD_BIP_TC1.mpd"),
    ("sony-1a", "https://dash.akamaized.net/dash264/TestCases/1a/sony/SNE_DASH_SD_CASE1A_REVISED.mpd"),
]

guard_results: list[bool] = []


def install_guard_spy() -> bool:
    """Record collapse decisions when this checkout has the guard; no-op otherwise."""
    orig = getattr(DASH, "collapsible_single_url", None)
    if orig is None:
        return False

    def spy(is_subtitle: bool, segments: Any, init_len: Optional[int]) -> bool:
        result = orig(is_subtitle, segments, init_len)
        guard_results.append(result)
        return result

    DASH.collapsible_single_url = staticmethod(spy)  # type: ignore[method-assign]
    return True


def _major_brand(path: Path) -> str:
    # ftyp box: [4-byte size][b"ftyp"][4-byte major brand]; empty if not an ftyp file
    with open(path, "rb") as f:
        head = f.read(12)
    return head[8:12].decode("latin-1") if head[4:8] == b"ftyp" else ""


def ffprobe(path: Path) -> str:
    if not path.exists() or not shutil.which("ffprobe"):
        return "n/a"
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
    )
    if r.stdout.strip():
        return f"{float(r.stdout.strip()):.0f}s"
    # Sony's senvu-profile MP4s use major brand "senv", which ffmpeg can't parse
    # ("Not yet implemented in FFmpeg"). The download is intact, so verify by size.
    if _major_brand(path) == "senv":
        return f"senv ok ({path.stat().st_size} B)"
    return "INVALID"


def run_one(label: str, url: str, which: str, workers: int, has_guard: bool) -> str:
    guard_results.clear()
    session = Session()
    try:
        tracks = DASH.from_url(url=url, session=session).to_tracks(language="en")
    except Exception as e:  # noqa: BLE001
        return f"{label:12} PARSE ERROR {type(e).__name__}: {e}"
    vids = list(tracks.videos)
    if not vids:
        return f"{label:12} NO VIDEO TRACKS"
    key = lambda t: t.bitrate or 0  # noqa: E731
    track = min(vids, key=key) if which == "smallest" else max(vids, key=key)

    work = Path(mkdtemp(prefix="segbase_ab_"))
    save_path = work / "out.mp4"
    # adaptive_workers only exists on the downloader-speed branch; skip elsewhere
    extra = {"adaptive_workers": True} if "adaptive_workers" in DownloadContext.__dataclass_fields__ else {}
    ctx = DownloadContext(
        save_path=save_path,
        save_dir=work / "segs",
        progress=lambda **ev: None,
        session=session,
        proxy=None,
        max_workers=workers,
        **extra,
    )

    t0 = time.time()
    err = ""
    try:
        DASH.download_track(track=track, ctx=ctx)
    except Exception as e:  # noqa: BLE001
        err = f"  ERROR {type(e).__name__}: {e}"
    dt = time.time() - t0
    size = save_path.stat().st_size if save_path.exists() else 0
    mbps = (size / 1e6 / dt) if dt > 0 and size else 0.0
    probe = ffprobe(save_path)
    shutil.rmtree(work, ignore_errors=True)

    if has_guard:
        collapse = "yes" if guard_results and guard_results[0] else "no"
    else:
        collapse = "unsupported"
    res = f"{getattr(track, 'width', '?')}x{getattr(track, 'height', '?')}"
    return (
        f"{label:12} {dt:6.1f}s {size / 1e6:8.1f}MB {mbps:6.1f}MB/s  [{res}]  "
        f"collapse: {collapse:11}  duration: {probe}{err}"
    )


@click.command()
@click.option("--workers", default=16, show_default=True, help="max workers (adaptive cap where supported)")
@click.option("--track", "which", type=click.Choice(["smallest", "largest"]), default="smallest", show_default=True)
@click.option("--only", default="", help="substring filter on stream labels")
def main(workers: int, which: str, only: str) -> None:
    has_guard = install_guard_spy()
    print(f"track: {which}, max_workers: {workers}, collapse guard present: {has_guard}\n")
    for label, url in STREAMS:
        if only and only.lower() not in label.lower():
            continue
        print(run_one(label, url, which, workers, has_guard))


if __name__ == "__main__":
    main()
