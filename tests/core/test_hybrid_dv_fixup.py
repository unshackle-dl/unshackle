"""Unit tests for the path from the DV fixup to the hybrid step, which must read the
container file and not the raw stream."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unshackle.commands.dl import hybrid_source_tracks, select_hybrid_bases
from unshackle.core.tracks import Video
from unshackle.core.tracks import dv_fixup as dv_fixup_mod
from unshackle.core.tracks.dv_fixup import apply_dv_fixup

pytestmark = pytest.mark.unit


def fake_video(tmp_path: Path, track_id: str, range_: Video.Range, height: int = 2160) -> Any:
    path = tmp_path / f"{track_id}.mp4"
    path.write_bytes(b"x")
    return SimpleNamespace(id=track_id, path=path, height=height, range=range_, dv_compatible_bitstream=True)


@pytest.fixture
def fake_demux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dv_fixup_mod, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(dv_fixup_mod, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(dv_fixup_mod, "run_step", lambda args, **k: Path(args[-1]).write_bytes(b"y"))


@pytest.mark.parametrize("keep_source", [False, True])
def test_dv_fixup_keeps_the_source_on_request(fake_demux: None, tmp_path: Path, keep_source: bool) -> None:
    video = fake_video(tmp_path, "abc", Video.Range.HDR10P)
    source = video.path

    kept = apply_dv_fixup(video, keep_source=keep_source)

    assert video.path == tmp_path / "abc.dv.hevc"
    assert source.exists() is keep_source
    assert kept == (source if keep_source else None)


def test_dv_fixup_that_falls_back_keeps_nothing(
    fake_demux: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed fixup leaves the track on its source, so there is no second file to clean up."""

    def failed_demux(args: list[Any], **kwargs: Any) -> None:
        raise RuntimeError("ffmpeg failed")

    monkeypatch.setattr(dv_fixup_mod, "run_step", failed_demux)
    video = fake_video(tmp_path, "abc", Video.Range.HDR10P)
    source = video.path

    assert apply_dv_fixup(video, keep_source=True) is None
    assert video.path == source and source.exists()


def test_hybrid_step_reads_the_container_after_the_fixup(fake_demux: None, tmp_path: Path) -> None:
    base = fake_video(tmp_path, "base", Video.Range.HDR10P)
    dv = fake_video(tmp_path, "dv", Video.Range.DV)
    container = apply_dv_fixup(base, keep_source=True)
    assert container is not None

    hybrid_base, hybrid_dv = hybrid_source_tracks(base, dv, container)

    assert base.path == tmp_path / "base.dv.hevc"
    assert hybrid_base.path == tmp_path / "base.mp4"
    assert hybrid_dv.path == dv.path
    assert hybrid_base.needs_duration_fix and hybrid_dv.needs_duration_fix


def test_one_hybrid_base_for_each_height(tmp_path: Path) -> None:
    videos = [
        fake_video(tmp_path, "sdr", Video.Range.SDR, 1080),
        fake_video(tmp_path, "low", Video.Range.HDR10, 1080),
        fake_video(tmp_path, "high", Video.Range.HDR10P, 2160),
        fake_video(tmp_path, "high2", Video.Range.HDR10, 2160),
        fake_video(tmp_path, "dv", Video.Range.DV, 2160),
    ]

    assert [v.id for v in select_hybrid_bases(videos)] == ["high", "low"]
