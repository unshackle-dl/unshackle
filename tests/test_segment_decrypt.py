from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID

import pytest

from unshackle.core import binaries
from unshackle.core.drm.segment_decrypt import SegmentDecrypter

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the mp4decrypt stub is a shell script")

ZERO_KID = "00" * 16
KID = UUID(hex="11111111111111111111111111111111")
KEY = "22222222222222222222222222222222"


class FakeDRM:
    """Stands in for Widevine/PlayReady: only the mp4decrypt key args matter here."""

    def __init__(self) -> None:
        self.content_keys = {KID: KEY}

    def mp4decrypt_key_args(self) -> list[str]:
        return ["--key", f"{KID.hex}:{KEY}", "--key", f"{ZERO_KID}:{KEY}"]


@pytest.fixture
def stub_mp4decrypt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An mp4decrypt that copies its input to its output and records its argv."""
    argv_log = tmp_path / "argv.jsonl"
    script = tmp_path / "mp4decrypt"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, shutil, sys\n"
        f"open({str(argv_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "shutil.copyfile(sys.argv[-2], sys.argv[-1])\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(binaries, "Mp4decrypt", script)
    return argv_log


def read_argv(argv_log: Path) -> list[list[str]]:
    return [json.loads(line) for line in argv_log.read_text().splitlines()]


def test_segments_and_init_decrypt(tmp_path: Path, stub_mp4decrypt: Path) -> None:
    segment_dir = tmp_path / "track_segments"
    segment_dir.mkdir()
    segments = []
    for i in range(3):
        segment = segment_dir / f"{i:03}.mp4"
        segment.write_bytes(f"segment-{i}".encode())
        segments.append(segment)

    decrypter = SegmentDecrypter(FakeDRM(), b"init-data", tmp_path / "work", workers=2)
    work_dir = decrypter.work_dir
    for segment in segments:
        decrypter.submit(segment)
    init_data = decrypter.finish()

    assert init_data == b"init-data"
    # the stub copies, so a replaced segment keeps its bytes under its original name
    assert sorted(p.name for p in segment_dir.iterdir()) == ["000.mp4", "001.mp4", "002.mp4"]
    for i, segment in enumerate(segments):
        assert segment.read_bytes() == f"segment-{i}".encode()
    assert not work_dir.exists()

    calls = read_argv(stub_mp4decrypt)
    assert len(calls) == 4

    segment_calls = [call for call in calls if "--fragments-info" in call]
    assert len(segment_calls) == 3
    for call in segment_calls:
        assert call[call.index("--fragments-info") + 1].endswith("init.mp4")
        assert call[-1].endswith(".dec")
        assert Path(call[-2]).parent == segment_dir

    init_calls = [call for call in calls if "--fragments-info" not in call]
    assert len(init_calls) == 1
    assert init_calls[0][-2].endswith("init.mp4")

    for call in calls:
        assert "--key" in call
        assert f"{ZERO_KID}:{KEY}" in call
        assert f"{KID.hex}:{KEY}" in call


def test_failure_reraises_and_leaves_no_temp_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "mp4decrypt"
    script.write_text("#!/bin/sh\necho 'no key' >&2\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setattr(binaries, "Mp4decrypt", script)

    segment_dir = tmp_path / "track_segments"
    segment_dir.mkdir()
    segment = segment_dir / "000.mp4"
    segment.write_bytes(b"segment-0")

    decrypter = SegmentDecrypter(FakeDRM(), b"init-data", tmp_path / "work", workers=1)
    decrypter.submit(segment)
    with pytest.raises(RuntimeError, match="no key"):
        decrypter.finish()

    assert [p.name for p in segment_dir.iterdir()] == ["000.mp4"]
    assert not decrypter.work_dir.exists()
