"""Unit tests for the Hybrid Level 6 path: the FFprobe HDR10 metadata probe and the
rule that decides when the RPU L6 block gets overwritten. Nothing runs FFprobe or
dovi_tool; the tests mock subprocess.run and the dovi wrappers."""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from unshackle.core.config import config
from unshackle.core.tracks import hybrid as hybrid_mod
from unshackle.core.tracks.hybrid import Hybrid

pytestmark = pytest.mark.unit

MASTERING = {
    "side_data_type": "Mastering display metadata",
    "min_luminance": "50/10000",
    "max_luminance": "10000000/10000",
}
CLL = {"side_data_type": "Content light level metadata", "max_content": 588, "max_average": 148}


class FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config.directories, "temp", tmp_path)
    monkeypatch.setattr(hybrid_mod, "log_event", lambda *a, **k: None)

    @contextlib.contextmanager
    def no_status(*args: Any, **kwargs: Any) -> Iterator[None]:
        yield

    monkeypatch.setattr(hybrid_mod.console, "status", no_status)


def make_hybrid() -> Hybrid:
    """Build a Hybrid without __init__, which would demux real video tracks."""
    obj = Hybrid.__new__(Hybrid)
    obj.log = logging.getLogger("hybrid-test")
    obj.rpu_file = "RPU.bin"
    return obj


def patch_probe_run(monkeypatch: pytest.MonkeyPatch, result: FakeCompleted) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> FakeCompleted:
        calls.append([str(a) for a in args])
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def probe_output(*side_data: dict) -> str:
    return json.dumps({"frames": [{"side_data_list": list(side_data)}]})


def test_probe_reads_mastering_display_and_light_level(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = patch_probe_run(monkeypatch, FakeCompleted(0, probe_output(MASTERING, CLL)))

    assert make_hybrid().probe_hdr_metadata() == (1000, 50, 588, 148)
    assert "-show_frames" in calls[0]
    assert "frame_side_data_list" in calls[0]


def test_probe_min_luminance_uses_integer_math(monkeypatch: pytest.MonkeyPatch) -> None:
    """Float rounding would turn 0.0003 nits into 2 units instead of 3."""
    mastering = dict(MASTERING, min_luminance="3/10000")
    patch_probe_run(monkeypatch, FakeCompleted(0, probe_output(mastering, CLL)))

    assert make_hybrid().probe_hdr_metadata()[1] == 3


def test_probe_falls_back_on_failed_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_probe_run(monkeypatch, FakeCompleted(1, "", "boom"))

    assert make_hybrid().probe_hdr_metadata() == (1000, 1, 0, 0)


def test_probe_falls_back_on_garbage_output(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_probe_run(monkeypatch, FakeCompleted(0, "not json at all"))

    assert make_hybrid().probe_hdr_metadata() == (1000, 1, 0, 0)


def rpu_info(max_cll: int, max_fall: int, max_mdl: int, min_mdl: int) -> dict:
    return {
        "vdr_dm_data": {
            "cmv29_metadata": {
                "ext_metadata_blocks": [
                    {
                        "Level6": {
                            "max_content_light_level": max_cll,
                            "max_frame_average_light_level": max_fall,
                            "max_display_mastering_luminance": max_mdl,
                            "min_display_mastering_luminance": min_mdl,
                        }
                    }
                ]
            }
        }
    }


def patch_level6(
    monkeypatch: pytest.MonkeyPatch,
    info: dict,
    probe: Optional[tuple[int, int, int, int]],
) -> list[Path]:
    """Mock the RPU read, the HDR10 probe, and the editor. Returns the editor call log."""
    edits: list[Path] = []

    monkeypatch.setattr(hybrid_mod.dovi, "info_frame", lambda *a, **k: info)

    def fake_probe(self: Hybrid) -> tuple[int, int, int, int]:
        if probe is None:
            raise AssertionError("probe_hdr_metadata must not run when the RPU L6 block is populated")
        return probe

    monkeypatch.setattr(Hybrid, "probe_hdr_metadata", fake_probe)

    def fake_editor(source: Path, json_spec: Path, output: Path, **kwargs: Any) -> bytes:
        edits.append(json_spec)
        return b""

    monkeypatch.setattr(hybrid_mod.dovi, "editor", fake_editor)
    return edits


def test_zero_light_levels_are_filled_from_the_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    edits = patch_level6(monkeypatch, rpu_info(0, 0, 1000, 1), (1000, 50, 588, 148))

    obj = make_hybrid()
    obj.level_6()

    assert len(edits) == 1
    written = json.loads(edits[0].read_text())["level6"]
    assert written["max_content_light_level"] == 588
    assert written["max_frame_average_light_level"] == 148
    assert written["max_display_mastering_luminance"] == 1000
    # The RPU mastering display wins over the probed 50; only unset values come from the probe.
    assert written["min_display_mastering_luminance"] == 1
    assert obj.rpu_file == "RPU_L6.bin"


def test_populated_light_levels_skip_the_probe_and_the_edit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    edits = patch_level6(monkeypatch, rpu_info(580, 538, 1000, 1), None)

    obj = make_hybrid()
    obj.level_6()

    assert edits == []
    assert obj.rpu_file == "RPU.bin"


def test_probe_without_light_levels_leaves_the_rpu_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    edits = patch_level6(monkeypatch, rpu_info(0, 0, 1000, 1), (1000, 1, 0, 0))

    obj = make_hybrid()
    obj.level_6()

    assert edits == []
    assert obj.rpu_file == "RPU.bin"


def test_level_6_returns_early_when_the_edited_rpu_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "RPU_L6.bin").write_bytes(b"")

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("level_6 must not read the RPU when RPU_L6.bin is already there")

    monkeypatch.setattr(hybrid_mod.dovi, "info_frame", boom)

    obj = make_hybrid()
    obj.level_6()

    assert obj.rpu_file == "RPU_L6.bin"


def test_level_5_returns_early_when_the_cropped_rpu_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "RPU_L5.bin").write_bytes(b"")

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("level_5 must not run ffprobe when RPU_L5.bin is already there")

    monkeypatch.setattr(subprocess, "run", boom)

    obj = make_hybrid()
    obj.level_5(tmp_path / "HDR10.hevc")

    assert obj.rpu_file == "RPU_L5.bin"
