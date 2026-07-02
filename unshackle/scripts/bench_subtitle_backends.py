#!/usr/bin/env python3
"""
Benchmark subtitle conversion backends to (re-)tune the preference ranks in
``unshackle/core/tracks/subtitle_convert.py``.

Runs every backend that can read each input file, converting to a target format (default
SRT), and reports cue count, leaked ASS override tags, and output size — so you can compare
fidelity per (source, target) pair on real files. Read-only: copies inputs to a temp dir.

Usage:
    uv run python scripts/bench_subtitle_backends.py <file-or-dir> [<file-or-dir> ...] [--target SRT]

Example:
    uv run python scripts/bench_subtitle_backends.py downloads/
"""

from __future__ import annotations

import argparse
import re
import shutil
import tempfile
from pathlib import Path

from unshackle.core.tracks import subtitle_convert as sc
from unshackle.core.tracks.subtitle import Subtitle

Codec = Subtitle.Codec

EXT_TO_CODEC = {
    ".srt": Codec.SubRip,
    ".vtt": Codec.WebVTT,
    ".ass": Codec.SubStationAlphav4,
    ".ssa": Codec.SubStationAlpha,
    ".ttml": Codec.TimedTextMarkupLang,
    ".smi": Codec.SAMI,
    ".sami": Codec.SAMI,
}


def gather(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files.extend(f for f in path.rglob("*") if f.suffix.lower() in EXT_TO_CODEC)
        elif path.suffix.lower() in EXT_TO_CODEC:
            files.append(path)
    return sorted(files)


def metrics(text: str) -> tuple[int, int, int]:
    cues = len(re.findall(r"-->", text))
    ass_residue = len(re.findall(r"\{\\", text))
    return cues, ass_residue, len(text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="subtitle files or directories")
    ap.add_argument("--target", default="SRT", help="target codec value (SRT, VTT, ASS, ...)")
    args = ap.parse_args()

    target = Codec(args.target.upper())
    files = gather(args.paths)
    if not files:
        print("No subtitle files found.")
        return

    tmp = Path(tempfile.mkdtemp(prefix="subbench_"))
    print(f"{'file':40} {'source':10} {'backend':12} {'ok':3} {'cues':>5} {'resid':>5} {'bytes':>7}")
    for f in files:
        source = EXT_TO_CODEC[f.suffix.lower()]
        if source == target:
            continue
        for backend in sc.REGISTRY:
            if not (backend.is_available() and backend.can_convert(source, target)):
                continue
            work = tmp / f"{f.stem}.{backend.name}{f.suffix}"
            shutil.copy2(f, work)
            sub = Subtitle(url="x", language="en", codec=source)
            sub.path = work
            try:
                # Call the backend directly so each row reflects only that backend (no fallback).
                out = work.with_suffix(f".{target.value.lower()}")
                backend.convert(sub, target, out)
                cues, resid, size = metrics(out.read_text("utf8", errors="replace"))
                print(
                    f"{f.name[:40]:40} {source.name[:10]:10} {backend.name:12} {'Y':3} {cues:>5} {resid:>5} {size:>7}"
                )
            except Exception as e:  # noqa: BLE001 - benchmark reports failures, does not raise
                print(
                    f"{f.name[:40]:40} {source.name[:10]:10} {backend.name:12} {'N':3} {'-':>5} {'-':>5} {'-':>7}  {type(e).__name__}"
                )


if __name__ == "__main__":
    main()
