"""Repackaging a video keeps its Dolby Vision configuration record."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from langcodes import Language

from unshackle.core import binaries
from unshackle.core.tracks import Video


def test_repackage_asks_ffmpeg_to_keep_unofficial_boxes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "v.mp4"
    source.write_bytes(b"\0")
    calls: list[list[str]] = []

    def fake_run(args: list, **kwargs: object) -> subprocess.CompletedProcess:
        calls.append([str(a) for a in args])
        Path(args[-1]).write_bytes(b"\0")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(binaries, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(subprocess, "run", fake_run)
    video = Video(url="https://cdn.example/v.mp4", language=Language.get("en"))
    video.path = source
    video.repackage()

    args = calls[0]
    assert args[args.index("-strict") + 1] == "unofficial"
