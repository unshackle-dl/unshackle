"""Tests for the repackaged-copy retry in ``Video.ccextractor``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from langcodes import Language

from unshackle.core.tracks import Video
from unshackle.core.tracks import video as video_module


def test_retry_runs_on_the_repackaged_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = tmp_path / "v.mp4"
    original.write_bytes(b"x")
    repacked = tmp_path / "v_repack.mp4"
    track = Video(id_="v", url="https://example.test/v.m3u8", language="en", codec=Video.Codec.AVC)
    track.path = original

    def fake_repackage() -> None:
        original.unlink()
        repacked.write_bytes(b"x")
        track.path = repacked

    inputs: list[Path] = []

    def fake_run(args: list[Any], **_: Any) -> None:
        inputs.append(Path(args[-1]))
        if not Path(args[-1]).exists():
            raise subprocess.CalledProcessError(2, args)  # ccextractor: no input file
        raise subprocess.CalledProcessError(10, args)  # ccextractor: no captions found

    monkeypatch.setattr(video_module.binaries, "CCExtractor", Path("/usr/bin/ccextractor"))
    monkeypatch.setattr(video_module.subprocess, "run", fake_run)
    monkeypatch.setattr(track, "repackage", fake_repackage)

    assert track.ccextractor("cc", tmp_path / "cc.srt", Language.get("en")) is None
    assert inputs == [original, repacked]
