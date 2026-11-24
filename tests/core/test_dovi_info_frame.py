"""Unit tests for dovi.info_frame, which parses one frame of RPU metadata out of
`dovi_tool info` output. The tests never run dovi_tool; they mock subprocess.run."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from unshackle.core.utils import dovi

pytestmark = pytest.mark.unit

RPU_JSON = (
    "Parsing RPU file...\n"
    '{"vdr_dm_data": {"cmv29_metadata": {"ext_metadata_blocks": '
    '[{"Level6": {"max_content_light_level": 588}}]}}}'
)


class FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def fake_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dovi.binaries, "DoviTool", Path("dovi_tool"))
    monkeypatch.setattr(dovi, "log_tool_run", lambda *a, **k: None)


def patch_run(monkeypatch: pytest.MonkeyPatch, result: FakeCompleted) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> FakeCompleted:
        calls.append(args)
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_parses_json_after_a_preamble(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = patch_run(monkeypatch, FakeCompleted(0, RPU_JSON))

    info = dovi.info_frame(tmp_path / "RPU.bin")

    blocks = info["vdr_dm_data"]["cmv29_metadata"]["ext_metadata_blocks"]
    assert blocks[0]["Level6"]["max_content_light_level"] == 588
    assert calls[0][1:] == ["info", "-i", str(tmp_path / "RPU.bin"), "-f", "0"]


def test_frame_number_is_passed_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = patch_run(monkeypatch, FakeCompleted(0, RPU_JSON))

    dovi.info_frame(tmp_path / "RPU.bin", frame=12)

    assert calls[0][-2:] == ["-f", "12"]


def test_non_zero_return_code_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_run(monkeypatch, FakeCompleted(1, "", "boom"))

    with pytest.raises(RuntimeError, match="dovi_tool info failed"):
        dovi.info_frame(tmp_path / "RPU.bin")


def test_stdout_without_json_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_run(monkeypatch, FakeCompleted(0, "Parsing RPU file...\nno json here\n"))

    with pytest.raises(RuntimeError, match="no JSON"):
        dovi.info_frame(tmp_path / "RPU.bin")


def test_broken_json_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_run(monkeypatch, FakeCompleted(0, "Parsing RPU file...\n{not: valid}"))

    with pytest.raises(RuntimeError, match="invalid JSON"):
        dovi.info_frame(tmp_path / "RPU.bin")


def test_missing_binary_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(dovi.binaries, "DoviTool", None)

    with pytest.raises(EnvironmentError):
        dovi.info_frame(tmp_path / "RPU.bin")
