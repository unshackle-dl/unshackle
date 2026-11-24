"""HLS.merge_segments must keep the ffmpeg child off the terminal.

merge_segments runs while the download progress tree is live. A child that inherits the
terminal moves the cursor out from under rich, which leaves an orphaned copy of the tree
on screen. Real trigger: ffmpeg refuses DTS-UHD audio with "Cannot map stream #0:0".
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from unshackle.core.manifests.hls import HLS

FFMPEG_ERROR = "[out#0/mp4 @ 0x0] Cannot map stream #0:0 - unsupported type."


@pytest.fixture
def segments(tmp_path: Path) -> list[Path]:
    save_dir = tmp_path / "Audio_segments"
    save_dir.mkdir()
    paths = []
    for i in range(3):
        path = save_dir / f"{i:05}.mp4"
        path.write_bytes(f"segment-{i}".encode())
        paths.append(path)
    return paths


def _capture_fd(fd: int):
    """Redirect a raw file descriptor to a temp file, returning (restore, read)."""
    sink = tempfile.TemporaryFile()
    saved = os.dup(fd)
    os.dup2(sink.fileno(), fd)

    def restore() -> bytes:
        os.dup2(saved, fd)
        os.close(saved)
        sink.seek(0)
        data = sink.read()
        sink.close()
        return data

    return restore


def test_failed_ffmpeg_concat_writes_nothing_to_the_terminal(
    tmp_path: Path, segments: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args, **kwargs):
        if not kwargs.get("capture_output") and kwargs.get("stderr") is None:
            os.write(2, (FFMPEG_ERROR + "\n").encode())
            raise subprocess.CalledProcessError(1, args)
        raise subprocess.CalledProcessError(1, args, output="", stderr=FFMPEG_ERROR)

    monkeypatch.setattr("unshackle.core.manifests.hls.binaries.FFMPEG", Path("ffmpeg"))
    monkeypatch.setattr("unshackle.core.manifests.hls.subprocess.run", fake_run)

    save_path = tmp_path / "Audio.mp4"
    read_stderr = _capture_fd(2)
    try:
        HLS.merge_segments(segments=segments, save_path=save_path)
    finally:
        leaked = read_stderr()

    assert leaked == b"", f"ffmpeg output reached the terminal: {leaked!r}"
    assert save_path.read_bytes() == b"segment-0segment-1segment-2"


def test_failed_ffmpeg_concat_logs_the_reason(
    tmp_path: Path, segments: list[Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fake_run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, output="", stderr=FFMPEG_ERROR)

    monkeypatch.setattr("unshackle.core.manifests.hls.binaries.FFMPEG", Path("ffmpeg"))
    monkeypatch.setattr("unshackle.core.manifests.hls.subprocess.run", fake_run)

    with caplog.at_level("DEBUG", logger="HLS"):
        HLS.merge_segments(segments=segments, save_path=tmp_path / "Audio.mp4")

    assert FFMPEG_ERROR in caplog.text


def test_binary_concat_advances_progress_per_segment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FFmpeg concat reports nothing, so the fallback is the only place a count can come from."""
    segments = []
    for i in range(4):
        seg = tmp_path / f"seg{i}.mp4"
        seg.write_bytes(b"\x00" * 16)
        segments.append(seg)

    calls: list[dict] = []
    monkeypatch.setattr("unshackle.core.manifests.hls.binaries.FFMPEG", None)
    HLS.merge_segments(
        segments=segments,
        save_path=tmp_path / "out.mp4",
        progress=lambda **kwargs: calls.append(kwargs),
    )

    assert calls.count({"advance": 1}) == len(segments), f"expected one advance per segment, got {calls}"
    assert (tmp_path / "out.mp4").stat().st_size == 4 * 16


def test_merge_segments_without_progress_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """progress stays optional; other callers pass nothing."""
    seg = tmp_path / "seg0.mp4"
    seg.write_bytes(b"\x01" * 8)
    monkeypatch.setattr("unshackle.core.manifests.hls.binaries.FFMPEG", None)
    size = HLS.merge_segments(segments=[seg], save_path=tmp_path / "out.mp4")
    assert size == 8


def test_cleanup_never_climbs_past_the_segment_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A download directory may itself say "_segments". Only the track's own segment store
    may be removed, never a parent that merely contains the word.
    """
    library = tmp_path / "media_segments"
    store = library / "temp" / "task_x" / "Audio_1.mp4_segments" / "segments"
    store.mkdir(parents=True)
    keep = library / "Some Other Movie.mkv"
    keep.write_bytes(b"do not delete me")

    segments = []
    for i in range(2):
        seg = store / f"{i:05}.mp4"
        seg.write_bytes(b"\x00" * 8)
        segments.append(seg)

    monkeypatch.setattr("unshackle.core.manifests.hls.binaries.FFMPEG", None)
    HLS.merge_segments(segments=segments, save_path=library / "out.mp4")

    assert keep.exists(), "cleanup deleted an unrelated file in the download directory"
    assert library.exists()
    assert not (library / "temp" / "task_x" / "Audio_1.mp4_segments").exists(), "the segment store was left behind"


@pytest.fixture
def ffmpeg_ok(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Patch in an ffmpeg that writes b"merged" and succeeds; gives the kwargs of each call."""
    calls: list[dict] = []

    def fake_run(args, **kwargs):
        calls.append(kwargs)
        Path(args[-1]).write_bytes(b"merged")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("unshackle.core.manifests.hls.binaries.FFMPEG", Path("ffmpeg"))
    monkeypatch.setattr("unshackle.core.manifests.hls.subprocess.run", fake_run)
    return calls


def test_segment_delete_error_after_ffmpeg_success_keeps_the_output(
    tmp_path: Path, segments: list[Path], ffmpeg_ok: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_unlink = Path.unlink

    def locked_unlink(self, *args, **kwargs):
        if self == segments[1]:
            raise PermissionError(13, "The process cannot access the file", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)

    save_path = tmp_path / "out.mp4"
    size = HLS.merge_segments(segments=segments, save_path=save_path)

    assert size == len(b"merged")
    assert save_path.read_bytes() == b"merged"


def test_ffmpeg_timeout_grows_with_input_size(tmp_path: Path, segments: list[Path], ffmpeg_ok: list[dict]) -> None:
    HLS.merge_segments(segments=segments, save_path=tmp_path / "small.mp4")

    segments[0].parent.mkdir(exist_ok=True)
    for i, path in enumerate(segments):
        path.write_bytes(f"segment-{i}".encode())
    segments[0].write_bytes(b"\0" * 10 * 1024 * 1024)
    HLS.merge_segments(segments=segments, save_path=tmp_path / "large.mp4")

    assert [call["timeout"] for call in ffmpeg_ok] == [300, 302]
