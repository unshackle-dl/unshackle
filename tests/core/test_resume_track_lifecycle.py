"""Lifecycle tests for the conditional wipe in Track.download itself.

The manifest-parser gates are covered by the DASH/HLS integration tests; these cover the
layer above them: with continue_downloads on, Track.download's entry sweep and its
exception handler must both preserve completed segments and the sidecar while purging
every partial, and with the flag off both must wipe the directory entirely. The manifest
parser is stubbed so only Track.download's own behaviour is under test.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED, DOWNLOAD_LICENCE_ONLY
from unshackle.core.tracks import Video, resume


@pytest.fixture(autouse=True)
def clean_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(config.directories, "temp", tmp_path)
    DOWNLOAD_CANCELLED.clear()
    DOWNLOAD_LICENCE_ONLY.clear()
    yield
    DOWNLOAD_CANCELLED.clear()


def make_track() -> Video:
    return Video(
        id_="t1",
        url="https://example.test/manifest.mpd",
        language="en",
        codec=Video.Codec.AVC,
        range_=Video.Range.SDR,
        width=1920,
        height=1080,
        bitrate=5_000_000,
        descriptor=Video.Descriptor.DASH,
    )


def seed_prior_run(tmp_path: Path) -> Path:
    save_dir = tmp_path / "Video_t1.mp4_segments"
    (save_dir / "segments").mkdir(parents=True)
    (save_dir / "0.mp4").write_bytes(b"complete segment")
    (save_dir / "1.mp4.p.!dev").write_bytes(b"partial")
    (save_dir / "segments" / "2.bin.h.!dev").write_bytes(b"hedge partial")
    resume.write_sidecar(save_dir, "f" * 64)
    return save_dir


def run_download(track: Video, monkeypatch, fail: bool):
    import requests as requests_lib

    from unshackle.core.manifests import DASH

    def stub_download_track(track, ctx):
        if fail:
            raise RuntimeError("simulated mid-download crash")
        ctx.save_path.write_bytes(b"output")
        track.path = ctx.save_path

    monkeypatch.setattr(DASH, "download_track", staticmethod(stub_download_track))
    track.download(
        session=requests_lib.Session(),
        prepare_drm=partial(lambda *a, **kw: None),
        progress=partial(lambda **kw: None),
    )


def test_flag_on_keeps_segments_and_sidecar_sweeps_partials(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "continue_downloads", True)
    save_dir = seed_prior_run(tmp_path)
    track = make_track()

    with pytest.raises(RuntimeError):
        run_download(track, monkeypatch, fail=True)

    assert (save_dir / "0.mp4").read_bytes() == b"complete segment"
    assert resume.sidecar_path(save_dir).exists()
    assert not list(save_dir.rglob("*.!dev"))


def test_flag_off_wipes_segments_and_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "continue_downloads", False)
    save_dir = seed_prior_run(tmp_path)
    track = make_track()

    with pytest.raises(RuntimeError):
        run_download(track, monkeypatch, fail=True)

    assert not save_dir.exists()
    assert not resume.sidecar_path(save_dir).exists()


def test_flag_off_clears_orphaned_sidecar_without_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "continue_downloads", False)
    save_dir = tmp_path / "Video_t1.mp4_segments"
    # a kill between a parser's dir removal and its sidecar clear leaves exactly this
    resume.write_sidecar(save_dir, "f" * 64)
    track = make_track()

    with pytest.raises(RuntimeError):
        run_download(track, monkeypatch, fail=True)

    assert not resume.sidecar_path(save_dir).exists()
