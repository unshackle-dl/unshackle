"""Live full-flow test over public DASH/HLS/ISM manifests on several CDNs.

Drives the real pipeline for each stream: <PARSER>.from_url().to_tracks() picks a
video track, then the parser's own download_track() runs the unified downloader
with the adaptive worker controller enabled (adaptive_workers=True, max_workers=16).
The controller logs its worker-target changes to a JSON-lines debug log; this script
reads them back and prints the scaling sequence per stream, so you can see whether the
downloader ramps workers up against each CDN or finishes before the controller acts.

STREAMS carries a public 4K/HDR/Dolby-Vision tier (labels uhd-hevc, dv-atmos, hevc-hls,
h265-cmaf) for exercising large high-bitrate tracks; filter it with --only uhd / dv / hevc / h265.

Throwaway-ish operational tool: it makes real network requests to public test assets.
Run: uv run python scripts/live_manifest_test.py [--workers N] [--track smallest|largest]
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Optional

import click
from requests import Session

from unshackle.core.manifests import DASH, HLS, ISM
from unshackle.core.tracks.track import DownloadContext
from unshackle.core.utilities import close_debug_logger, init_debug_logger

# (format label, parser, CDN label, manifest URL); tears-of-steel is the same asset in
# all three formats on Unified Streaming, so the formats are comparable on one CDN.
STREAMS: list[tuple[str, Any, str, str]] = [
    ("HLS", HLS, "mux", "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"),
    (
        "HLS",
        HLS,
        "unified",
        "https://demo.unified-streaming.com/k8s/features/stable/video/tears-of-steel/tears-of-steel.ism/.m3u8",
    ),
    ("DASH", DASH, "akamai", "https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps.mpd"),
    (
        "DASH",
        DASH,
        "unified",
        "https://demo.unified-streaming.com/k8s/features/stable/video/tears-of-steel/tears-of-steel.ism/.mpd",
    ),
    ("DASH", DASH, "dashif", "https://livesim2.dashif.org/vod/testpic_2s/Manifest.mpd"),
    (
        "ISM",
        ISM,
        "unified",
        "https://demo.unified-streaming.com/k8s/features/stable/video/tears-of-steel/tears-of-steel.ism/Manifest",
    ),
    (
        "HLS",
        HLS,
        "apple",
        "https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_fmp4/master.m3u8",
    ),
    ("HLS", HLS, "gcs", "https://storage.googleapis.com/shaka-demo-assets/angel-one-hls/hls.m3u8"),
    ("DASH", DASH, "gcs", "https://storage.googleapis.com/shaka-demo-assets/angel-one/dash.mpd"),
    ("DASH", DASH, "axinom", "https://media.axprod.net/TestVectors/v7-Clear/Manifest_1080p.mpd"),
    (
        "ISM",
        ISM,
        "dtaps",
        "https://playready.directtaps.net/smoothstreaming/SSWSS720H264/SuperSpeedway_720.ism/Manifest",
    ),
    # public 4K / HDR / Dolby-Vision tier: large high-bitrate tracks, no auth
    ("DASH", DASH, "uhd-hevc", "https://dash.akamaized.net/dash264/TestCasesUHD/2b/11/MultiRate.mpd"),
    ("HLS", HLS, "dv-atmos", "https://devstreaming-cdn.apple.com/videos/streaming/examples/adv_dv_atmos/main.m3u8"),
    (
        "HLS",
        HLS,
        "hevc-hls",
        "https://devstreaming-cdn.apple.com/videos/streaming/examples/bipbop_adv_example_hevc/master.m3u8",
    ),
    ("DASH", DASH, "h265-cmaf", "https://media.axprod.net/TestVectors/H265/clear_cmaf_1080p_h265/manifest.mpd"),
]


class Counter:
    """Collects downloader progress events in place of the rich progress callable."""

    def __init__(self) -> None:
        self.files = 0

    def __call__(self, **ev: Any) -> None:
        if "file_downloaded" in ev:
            self.files += 1


def pick_track(tracks: object, which: str) -> Optional[Any]:
    vids = list(getattr(tracks, "videos", []))
    if not vids:
        auds = list(getattr(tracks, "audio", []))
        return auds[0] if auds else None
    key = lambda t: getattr(t, "bitrate", 0) or 0  # noqa: E731
    return min(vids, key=key) if which == "smallest" else max(vids, key=key)


def read_new_transitions(log_path: Path, start_line: int) -> tuple[list[str], int]:
    """Return (worker-target transitions since start_line, new line count)."""
    if not log_path.exists():
        return [], start_line
    lines = log_path.read_text(encoding="utf-8").splitlines()
    moves = []
    for raw in lines[start_line:]:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if rec.get("operation") == "adaptive_workers":
            ctx = rec.get("context", {})
            moves.append(f"{ctx.get('old')}->{ctx.get('new')}({ctx.get('reason')})")
    return moves, len(lines)


def run_one(label: str, parser: Any, cdn: str, url: str, workers: int, which: str, log_path: Path, line0: int) -> str:
    session = Session()
    try:
        tracks = parser.from_url(url=url, session=session).to_tracks(language="en")
    except Exception as e:  # noqa: BLE001
        return f"{label:5} {cdn:8} PARSE ERROR {type(e).__name__}: {e}"
    track = pick_track(tracks, which)
    if track is None:
        return f"{label:5} {cdn:8} NO TRACK"

    work = Path(mkdtemp(prefix="live_"))
    save_path = work / "out.mp4"
    counter = Counter()
    # adaptive_workers only exists on the downloader-speed branch; skip on older checkouts
    extra = (
        {"adaptive_workers": True} if "adaptive_workers" in getattr(DownloadContext, "__dataclass_fields__", {}) else {}
    )
    ctx = DownloadContext(
        save_path=save_path,
        save_dir=work / "segs",
        progress=counter,
        session=session,
        proxy=None,
        max_workers=workers,
        **extra,
    )

    track.drm = None  # download-speed only: time raw segment download, no license/decrypt
    t0 = time.time()
    err = ""
    try:
        parser.download_track(track=track, ctx=ctx)
    except Exception as e:  # noqa: BLE001
        err = f"  ERROR {type(e).__name__}: {e}"
    dt = time.time() - t0
    size = save_path.stat().st_size if save_path.exists() else 0
    mbps = (size / 1e6 / dt) if dt > 0 and size else 0.0
    shutil.rmtree(work, ignore_errors=True)

    moves, _ = read_new_transitions(log_path, line0)
    scaling = " ".join(moves) if moves else "none (finished under controller tick)"
    res = f"{getattr(track, 'width', '?')}x{getattr(track, 'height', '?')}"
    return f"{label:5} {cdn:8} {dt:6.1f}s {size / 1e6:7.1f}MB {mbps:6.1f}MB/s  [{res}]  workers: {scaling}{err}"


@click.command()
@click.option("--workers", default=16, help="max workers (the adaptive ceiling)")
@click.option("--track", "which", type=click.Choice(["smallest", "largest"]), default="largest")
@click.option("--only", default="", help="substring filter on CDN/format labels (e.g. 'apple' or 'ISM')")
def main(workers: int, which: str, only: str) -> None:
    # download-speed only: skip HLS key handling so segments just download, no license server
    HLS.get_drm = staticmethod(lambda *a, **k: None)  # type: ignore[assignment]

    log_path = Path(mkdtemp(prefix="live_dbg_")) / "debug.jsonl"
    init_debug_logger(log_path=log_path, enabled=True)
    print(f"debug log: {log_path}\ntrack: {which}, max_workers: {workers}\n")
    rows = []
    line0 = 0
    try:
        streams = [s for s in STREAMS if only.lower() in s[0].lower() or only.lower() in s[2].lower()]
        for label, parser, cdn, url in streams:
            _, line0 = read_new_transitions(log_path, line0)  # advance past prior run's lines
            row = run_one(label, parser, cdn, url, workers, which, log_path, line0)
            print(row)
            rows.append(row)
    finally:
        close_debug_logger()
    ok = sum(1 for r in rows if "MB/s" in r and "ERROR" not in r and "NO TRACK" not in r)
    ramped = sum(1 for r in rows if "->" in r)
    print(f"\n{ok}/{len(rows)} streams downloaded; {ramped} showed live worker scaling")
    sys.exit(0 if ok == len(rows) else 1)


if __name__ == "__main__":
    main()
