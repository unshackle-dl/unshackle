"""Unit tests for the HDR10+ to Dolby Vision conversion: the L1 built from HDR10+ histograms, the
generator config, the active-area share, and the dl and REST API entry points. Nothing runs
dovi_tool, hdr10plus_tool or FFmpeg."""

from __future__ import annotations

import contextlib
import gc
import json
import logging
import re
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Optional

import pytest

import unshackle.commands.dl as dl_module
from tests.remote.unit.test_remote_service_helpers import _sent_create_data
from unshackle.commands.dl import (
    hybrid_gate_failure,
    hybrid_plan,
    hybrid_source_tracks,
    hybrid_step_error,
    missing_hybrid_error,
    select_hybrid_bases,
)
from unshackle.core.api import download_manager, handlers
from unshackle.core.api.handlers import validate_download_parameters
from unshackle.core.config import config
from unshackle.core.service import Service, TrackRequest
from unshackle.core.services import Services
from unshackle.core.tracks import Audio, Chapter, Subtitle, Tracks, Video
from unshackle.core.tracks import hybrid as hybrid_mod
from unshackle.core.tracks.hybrid import Hybrid, hdr10plus_mean_pq, hdr10plus_shots, nits_to_pq

pytestmark = pytest.mark.unit

INDEXES = [1, 5, 10, 25, 50, 75, 90, 95, 99]


def histogram(nits: dict[int, float]) -> dict[int, float]:
    """A full 9-slot histogram: every distribution slot at 50 nits unless given."""
    return {index: nits.get(index, 50.0) for index in INDEXES}


def scene_info(values: dict[int, float], average_rgb: float = 30.0) -> dict[str, Any]:
    return {
        "LuminanceParameters": {
            "AverageRGB": round(average_rgb * 10),
            "LuminanceDistributions": {
                "DistributionIndex": list(values),
                "DistributionValues": [round(v * 10) for v in values.values()],
            },
            "MaxScl": [0, 0, 0],
        }
    }


def metadata(
    scenes: list[tuple[int, dict[int, float]]], first_frame: int = 0, average_rgb: float = 30.0
) -> dict[str, Any]:
    """HDR10+ JSON in hdr10plus_tool's layout, one SceneInfo entry per frame."""
    frames = [scene_info(values, average_rgb) for length, values in scenes for _ in range(length)]
    firsts, frame = [], first_frame
    for length, _ in scenes:
        firsts.append(frame)
        frame += length
    return {
        "JSONInfo": {"HDR10plusProfile": "B", "Version": "1.0"},
        "SceneInfo": frames,
        "SceneInfoSummary": {"SceneFirstFrameIndex": firsts, "SceneFrameNumbers": [n for n, _ in scenes]},
    }


def test_pq_matches_the_st2084_reference_points() -> None:
    assert round(nits_to_pq(100)) == 2081
    assert round(nits_to_pq(1000)) == 3079
    assert round(nits_to_pq(10000)) == 4095
    assert round(nits_to_pq(0)) == 0


@pytest.mark.parametrize("active", [1.0, 0.75])
def test_a_flat_histogram_averages_to_its_own_level(active: float) -> None:
    values = {index: 120.0 for index in INDEXES}
    mean = hdr10plus_mean_pq(values, active)
    assert mean is not None and mean == pytest.approx(nits_to_pq(120.0))


def test_the_slots_outside_the_distribution_do_not_move_the_mean() -> None:
    """Index 5 is a luminance-Y value and index 10 a pixel share, so neither is a maxRGB percentile."""
    base = hdr10plus_mean_pq(histogram({5: 50.0, 10: 50.0}))
    changed = hdr10plus_mean_pq(histogram({5: 4000.0, 10: 9.7}))
    assert base == changed


def test_the_mean_is_taken_in_pq_not_in_linear_light() -> None:
    """Half the pixels at 1 nit and half at 1000: PQ of the linear mean is far brighter than the PQ mean."""
    values = histogram({1: 1.0, 25: 1.0, 50: 1.0, 75: 1000.0, 90: 1000.0, 95: 1000.0, 99: 1000.0})
    mean = hdr10plus_mean_pq(values)
    assert mean is not None
    assert mean < nits_to_pq(500.5) - 400


LETTERBOXED = histogram({1: 0.0, 25: 20.0, 50: 40.0, 75: 80.0, 90: 150.0, 95: 200.0, 99: 400.0})


def test_letterbox_bars_are_left_out_of_the_mean() -> None:
    """Black up to the bar share and flat above it: the active mean is the plateau itself."""
    bars_then_plateau = histogram({1: 0.0, 25: 200.0, 50: 200.0, 75: 200.0, 90: 200.0, 95: 200.0, 99: 200.0})
    full, active = hdr10plus_mean_pq(bars_then_plateau, 1.0), hdr10plus_mean_pq(bars_then_plateau, 0.75)
    assert full is not None and active is not None
    assert active == pytest.approx(nits_to_pq(200.0))
    assert full < active


def test_a_scene_with_no_black_pixels_keeps_its_whole_frame() -> None:
    """A title that changes aspect ratio has full-frame scenes, and those have no bars to leave out."""
    full_frame = {**LETTERBOXED, 1: 0.5}
    assert hdr10plus_mean_pq(full_frame, 0.75) == hdr10plus_mean_pq(full_frame, 1.0)


def test_a_ten_slot_histogram_uses_the_same_lookup() -> None:
    nine = histogram({99: 300.0})
    ten = {**nine, 98: 250.0}
    assert hdr10plus_mean_pq(ten) == hdr10plus_mean_pq(nine)


def test_a_histogram_without_a_distribution_slot_has_no_mean() -> None:
    values = histogram({})
    del values[75]
    assert hdr10plus_mean_pq(values) is None


def test_each_scene_becomes_one_shot_with_its_own_cut() -> None:
    meta = metadata([(3, histogram({99: 200.0})), (5, histogram({99: 800.0}))], first_frame=1000)

    shots = hdr10plus_shots(meta)

    assert [(s["start"], s["duration"]) for s in shots] == [(0, 3), (3, 5)]
    levels = [s["metadata_blocks"][0]["Level1"] for s in shots]
    assert [lv["max_pq"] for lv in levels] == [round(nits_to_pq(200.0)), round(nits_to_pq(800.0))]
    assert all(lv["min_pq"] == 0 and lv["avg_pq"] < lv["max_pq"] for lv in levels)


def test_avg_is_the_pq_mean_and_not_the_average_rgb() -> None:
    """dovi_tool's PQ(AverageMaxRGB) is far brighter than the PQ mean; the shot must carry the PQ mean."""
    meta = metadata([(2, LETTERBOXED)], average_rgb=150.0)

    (shot,) = hdr10plus_shots(meta, 0.8)

    mean = hdr10plus_mean_pq(LETTERBOXED, 0.8)
    assert mean is not None and mean != hdr10plus_mean_pq(LETTERBOXED, 1.0)
    assert shot["metadata_blocks"][0]["Level1"]["avg_pq"] == round(mean)
    assert round(mean) < round(nits_to_pq(150.0)) - 100


def test_a_scene_without_a_full_distribution_falls_back_to_the_average_rgb() -> None:
    values = histogram({})
    del values[25]
    meta = metadata([(2, values)])

    (shot,) = hdr10plus_shots(meta)

    assert shot["metadata_blocks"][0]["Level1"]["avg_pq"] == round(nits_to_pq(30.0))


def test_the_peak_skips_the_pixel_share_slot_but_keeps_the_luminance_slot() -> None:
    """Index 10 holds a percentage (100 = every pixel under 100 nits), which would read as 10 nits in a
    near-black scene. Index 5 holds the 99.99% luminance, a real brightness that can top index 99."""
    dark = {index: 0.2 for index in INDEXES}
    dark[10] = 10.0
    (dark_shot,) = hdr10plus_shots(metadata([(2, dark)]))
    assert dark_shot["metadata_blocks"][0]["Level1"]["max_pq"] == round(nits_to_pq(0.2))

    (bright_shot,) = hdr10plus_shots(metadata([(2, histogram({5: 1400.0, 99: 1200.0}))]))
    assert bright_shot["metadata_blocks"][0]["Level1"]["max_pq"] == round(nits_to_pq(1400.0))


def test_an_empty_histogram_takes_its_peak_from_maxscl_and_caps_the_avg() -> None:
    meta = metadata([(2, {})], average_rgb=900.0)
    meta["SceneInfo"][0]["LuminanceParameters"]["MaxScl"] = [4000, 6000, 5000]

    (shot,) = hdr10plus_shots(meta)

    level1 = shot["metadata_blocks"][0]["Level1"]
    assert level1["max_pq"] == round(nits_to_pq(600.0))
    assert level1["avg_pq"] == level1["max_pq"]


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config.directories, "temp", tmp_path)
    monkeypatch.setattr(config, "generate_dv_mode", "v29")
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
    obj.hdr10plus_file = "HDR10Plus.json"
    return obj


@pytest.mark.parametrize("top, expected", [(277, (2160 - 554) / 2160), (1000, None)])
def test_active_area_share(top: int, expected: Optional[float], quiet: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A letterboxed picture gives its crop and share of the frame; an implausibly small one gives None.

    cropdetect settles over its frames, so the last crop line counts.
    """

    def fake_run(args: list[Any], **kwargs: Any) -> SimpleNamespace:
        if "format=duration" in args:
            return SimpleNamespace(returncode=0, stdout='{"format": {"duration": "100"}}', stderr="")
        if "stream=width,height" in args:
            return SimpleNamespace(returncode=0, stdout='{"streams": [{"width": 3840, "height": 2160}]}', stderr="")
        return SimpleNamespace(
            returncode=0, stdout="", stderr=f"crop=3840:2160:0:0\ncrop=3840:{2160 - 2 * top}:0:{top}"
        )

    monkeypatch.setattr(hybrid_mod.subprocess, "run", fake_run)

    area = make_hybrid().detect_active_area(Path("base.mkv"))

    if expected is None:
        assert area is None
    else:
        assert area is not None
        assert area[0] == (0, top, 0, top)
        assert area[1] == pytest.approx(expected)


def test_each_crop_sample_evaluates_enough_frames(quiet: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """cropdetect ignores its first two frames (skip=2) and only grows the picture over the rest."""
    cropdetect_calls: list[list[str]] = []

    def fake_run(args: list[Any], **kwargs: Any) -> SimpleNamespace:
        if "format=duration" in args:
            return SimpleNamespace(returncode=0, stdout='{"format": {"duration": "100"}}', stderr="")
        if "stream=width,height" in args:
            return SimpleNamespace(returncode=0, stdout='{"streams": [{"width": 3840, "height": 2160}]}', stderr="")
        cropdetect_calls.append([str(a) for a in args])
        return SimpleNamespace(returncode=0, stdout="", stderr="crop=3840:1600:0:280")

    monkeypatch.setattr(hybrid_mod.subprocess, "run", fake_run)

    make_hybrid().detect_active_area(Path("base.mkv"))

    assert cropdetect_calls
    for args in cropdetect_calls:
        filtergraph = args[args.index("-vf") + 1]
        skip_match = re.search(r"skip=(\d+)", filtergraph)
        skip = int(skip_match.group(1)) if skip_match else 2
        assert int(args[args.index("-vframes") + 1]) - skip >= 8


def test_the_parsed_metadata_is_released_before_dovi_tool_runs(
    quiet: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """hdr10plus_tool writes one entry per frame, so a film's parsed JSON is hundreds of MB of objects."""

    class Tracked(dict):  # type: ignore[type-arg]
        pass

    (tmp_path / "HDR10Plus.json").write_text(json.dumps(metadata([(2, histogram({}))])))
    hybrid = make_hybrid()
    monkeypatch.setattr(hybrid, "probe_hdr_metadata", lambda: (1000, 1, 0, 0))
    real_load = json.load
    refs: list[weakref.ReferenceType[Tracked]] = []

    def tracked_load(fp: Any) -> Tracked:
        loaded = Tracked(real_load(fp))
        refs.append(weakref.ref(loaded))
        return loaded

    def fake_generate(config_json: Path, output: Path, **kwargs: Any) -> bytes:
        gc.collect()
        assert refs and refs[0]() is None, "the parsed HDR10+ metadata is still alive"
        output.write_bytes(b"rpu")
        return b""

    monkeypatch.setattr(hybrid_mod.json, "load", tracked_load)
    monkeypatch.setattr(hybrid_mod.dovi, "generate", fake_generate)

    hybrid.convert_hdr10plus_to_dv()


def test_conversion_writes_a_cm29_config_with_shots_and_cleans_up(
    quiet: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "HDR10Plus.json").write_text(json.dumps(metadata([(4, LETTERBOXED), (2, histogram({99: 900.0}))])))
    hybrid = make_hybrid()
    monkeypatch.setattr(hybrid, "probe_hdr_metadata", lambda: (1000, 1, 650, 120))
    seen: dict[str, Any] = {}

    def fake_generate(config_json: Path, output: Path, **kwargs: Any) -> bytes:
        seen.update(json.loads(config_json.read_text()))
        output.write_bytes(b"rpu")
        return b""

    monkeypatch.setattr(hybrid_mod.dovi, "generate", fake_generate)

    hybrid.convert_hdr10plus_to_dv(0.9)

    assert seen["cm_version"] == "V29"
    assert seen["level6"]["max_content_light_level"] == 650
    assert [s["duration"] for s in seen["shots"]] == [4, 2]
    assert seen["shots"][0]["metadata_blocks"][0]["Level1"]["avg_pq"] == round(hdr10plus_mean_pq(LETTERBOXED, 0.9) or 0)
    assert "length" not in seen
    assert (tmp_path / "RPU.bin").exists()
    assert not (tmp_path / "HDR10Plus.json").exists() and not (tmp_path / "generate.json").exists()


def run_conversion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str) -> tuple[dict, dict]:
    """Convert a two-scene HDR10+ file in the given generate_dv_mode; return the config and generate's kwargs."""
    monkeypatch.setattr(config, "generate_dv_mode", mode)
    (tmp_path / "HDR10Plus.json").write_text(json.dumps(metadata([(4, LETTERBOXED), (2, histogram({99: 900.0}))])))
    hybrid = make_hybrid()
    monkeypatch.setattr(hybrid, "probe_hdr_metadata", lambda: (1000, 1, 650, 120))
    seen: dict[str, Any] = {}
    called: dict[str, Any] = {}

    def fake_generate(config_json: Path, output: Path, **kwargs: Any) -> bytes:
        seen.update(json.loads(config_json.read_text()))
        called.update(
            kwargs, hdr10plus_existed=bool(kwargs.get("hdr10plus_json") and kwargs["hdr10plus_json"].exists())
        )
        output.write_bytes(b"rpu")
        return b""

    monkeypatch.setattr(hybrid_mod.dovi, "generate", fake_generate)
    hybrid.convert_hdr10plus_to_dv(0.9)
    return seen, called


def test_v40_mode_keeps_the_shots_and_asks_for_the_cm40_avg_floor(
    quiet: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen, called = run_conversion(monkeypatch, tmp_path, "v40")

    assert seen["cm_version"] == "V29" and seen["l1_avg_pq_cm_version"] == "V40"
    assert [s["duration"] for s in seen["shots"]] == [4, 2]
    assert not called.get("hdr10plus_json")


def test_v29_mode_leaves_the_avg_floor_to_the_cm_version(
    quiet: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen, called = run_conversion(monkeypatch, tmp_path, "v29")

    assert "l1_avg_pq_cm_version" not in seen and not called.get("hdr10plus_json")


def test_legacy_mode_hands_the_hdr10plus_json_to_dovi_tool(
    quiet: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen, called = run_conversion(monkeypatch, tmp_path, "legacy")

    assert "shots" not in seen and seen["length"] == 0 and seen["level6"]["max_content_light_level"] == 650
    assert called["hdr10plus_existed"]
    assert not (tmp_path / "HDR10Plus.json").exists() and not (tmp_path / "generate.json").exists()


def test_an_unknown_mode_is_refused_before_dovi_tool_runs(
    quiet: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="v29, v40, legacy"):
        run_conversion(monkeypatch, tmp_path, "v50")


def test_a_failed_generate_still_removes_the_temp_files(
    quiet: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "HDR10Plus.json").write_text(json.dumps(metadata([(2, histogram({}))])))
    hybrid = make_hybrid()
    monkeypatch.setattr(hybrid, "probe_hdr_metadata", lambda: (1000, 1, 0, 0))

    def failed_generate(*args: Any, **kwargs: Any) -> bytes:
        raise RuntimeError("dovi_tool failed")

    monkeypatch.setattr(hybrid_mod.dovi, "generate", failed_generate)

    with pytest.raises(ValueError):
        hybrid.convert_hdr10plus_to_dv()
    assert not (tmp_path / "HDR10Plus.json").exists() and not (tmp_path / "generate.json").exists()


def test_a_conversion_run_removes_its_rpus_so_the_next_base_makes_its_own(
    quiet: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each base's RPU comes from its own HDR10+, and level_6 reuses an RPU_L6.bin that it finds."""
    base = tmp_path / "base.mkv"
    base.write_bytes(b"")
    (tmp_path / "RPU_L6.bin").write_bytes(b"stale")
    seen: dict[str, Any] = {}

    def convert(self: Hybrid, active_fraction: float = 1.0) -> None:
        seen["stale_l6"] = (tmp_path / "RPU_L6.bin").exists()
        seen["active_fraction"] = active_fraction
        (tmp_path / "RPU.bin").write_bytes(b"rpu")

    def level_6(self: Hybrid) -> None:
        (tmp_path / "RPU_L6.bin").write_bytes(b"rpu")

    for name in ("extract_stream", "extract_hdr10plus", "level_5", "injecting"):
        monkeypatch.setattr(Hybrid, name, lambda self, *a, **k: None)
    monkeypatch.setattr(Hybrid, "detect_active_area", lambda self, path: ((0, 100, 0, 100), 0.9))
    monkeypatch.setattr(Hybrid, "convert_hdr10plus_to_dv", convert)
    monkeypatch.setattr(Hybrid, "level_6", level_6)
    monkeypatch.setattr(hybrid_mod.console, "print", lambda *a, **k: None)

    Hybrid([SimpleNamespace(id="b", path=base, height=2160, range=Video.Range.HDR10P)], "TEST")

    assert seen == {"stale_l6": False, "active_fraction": 0.9}
    assert not (tmp_path / "RPU.bin").exists() and not (tmp_path / "RPU_L6.bin").exists()


def test_a_failed_resolution_removes_its_crop_edits_and_keeps_the_dv_files(
    quiet: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """level_5 reuses an RPU_L5.bin it finds, so a failure must not leave the crop of another resolution.

    The RPU and DV stream come from the same DV track for every resolution, so those stay.
    """
    for name in ("base.mkv", "dv.mkv", "DV.hevc"):
        (tmp_path / name).write_bytes(b"")
    writes = {"extract_rpu": "RPU.bin", "level_6": "RPU_L6.bin", "level_5": "RPU_L5.bin"}
    for name, written in writes.items():
        monkeypatch.setattr(Hybrid, name, lambda self, *a, _w=written, **k: (tmp_path / _w).write_bytes(b"x"))
    monkeypatch.setattr(Hybrid, "extract_stream", lambda self, *a, **k: (tmp_path / "HDR10.hevc").write_bytes(b""))
    monkeypatch.setattr(Hybrid, "detect_active_area", lambda self, path: ((0, 100, 0, 100), 0.9))

    def failed_inject(self: Hybrid) -> None:
        (tmp_path / "L5.json").write_text("{}")
        raise ValueError("dovi_tool failed")

    monkeypatch.setattr(Hybrid, "injecting", failed_inject)
    monkeypatch.setattr(hybrid_mod.console, "print", lambda *a, **k: None)
    videos = [
        SimpleNamespace(id="b", path=tmp_path / "base.mkv", height=2160, range=Video.Range.HDR10),
        SimpleNamespace(id="d", path=tmp_path / "dv.mkv", height=1080, range=Video.Range.DV),
    ]

    with pytest.raises(ValueError):
        Hybrid(videos, "TEST")

    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["DV.hevc", "RPU.bin", "RPU_L6.bin", "base.mkv", "dv.mkv"]


def test_a_failed_extract_raises_instead_of_exiting(quiet: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A SystemExit would kill a REST worker; the title must be able to go on without this resolution."""

    def failed_step(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("ffmpeg failed")

    monkeypatch.setattr(hybrid_mod, "run_step", failed_step)

    with pytest.raises(ValueError, match="HDR10 stream"):
        make_hybrid().extract_stream(Path("base.mkv"), "HDR10")


def test_an_empty_hdr10plus_file_is_removed_before_the_error(
    quiet: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The next resolution returns early on a metadata file it finds, so an empty one must not stay."""
    monkeypatch.setattr(hybrid_mod, "HDR10PlusTool", "hdr10plus_tool")
    monkeypatch.setattr(hybrid_mod, "run_step", lambda *a, output, **k: output.write_bytes(b""))

    with pytest.raises(ValueError, match="No HDR10\\+ metadata"):
        make_hybrid().extract_hdr10plus(SimpleNamespace(id="b"))
    assert not (tmp_path / "HDR10Plus.json").exists()


@pytest.mark.parametrize(
    "error", [ValueError("bad"), KeyError("duration"), FileNotFoundError("tool"), RuntimeError("x")]
)
def test_a_hybrid_failure_is_reported_so_the_title_goes_on(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def failed_hybrid(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(dl_module, "Hybrid", failed_hybrid)

    assert hybrid_step_error([], "TEST", "HDR10-DV-2160p.hevc") == str(error)


def test_a_working_hybrid_reports_no_error_and_an_exit_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(dl_module, "Hybrid", lambda tracks, source, output_name: seen.update(name=output_name))
    assert hybrid_step_error([], "TEST", "HDR10-DV-2160p.hevc") is None and seen == {"name": "HDR10-DV-2160p.hevc"}

    def exiting(*args: Any, **kwargs: Any) -> None:
        raise SystemExit(1)

    monkeypatch.setattr(dl_module, "Hybrid", exiting)
    with pytest.raises(SystemExit):
        hybrid_step_error([], "TEST", "HDR10-DV-2160p.hevc")


def test_a_title_without_a_hybrid_file_fails() -> None:
    bases = [video("h2160", Video.Range.HDR10), video("h1080", Video.Range.HDR10, 1080)]
    assert missing_hybrid_error(bases, 1) is None
    assert missing_hybrid_error([], 0) == "No HEVC HDR10 or HDR10+ track to make a hybrid from"
    assert missing_hybrid_error(bases, 0) == ("Every hybrid failed (2160p, 1080p), so the title has no hybrid file")


def test_only_hevc_tracks_are_hybrid_bases() -> None:
    av1 = SimpleNamespace(id="av1", height=2160, range=Video.Range.HDR10P, codec=Video.Codec.AV1)
    hevc = SimpleNamespace(id="hevc", height=1080, range=Video.Range.HDR10P, codec=Video.Codec.HEVC)

    assert [v.id for v in select_hybrid_bases([av1, hevc])] == ["hevc"]  # type: ignore[list-item]


@pytest.mark.parametrize(
    "dv_available, to_dv, hdr10_gives, fetched, kept",
    [
        (True, True, ["HDR10"], ["HDR10", "DV"], ["DV", "HDR10"]),
        (False, False, ["HDR10"], ["HDR10", "DV"], ["HDR10"]),
        (False, True, ["HDR10"], ["HDR10", "DV", "HDR10P"], ["HDR10", "HDR10P"]),
        (False, True, ["HDR10", "HDR10P"], ["HDR10", "DV"], ["HDR10", "HDR10P"]),
    ],
)
def test_hybrid_asks_for_hdr10plus_only_for_a_conversion_without_dv(
    dv_available: bool, to_dv: bool, hdr10_gives: list[str], fetched: list[str], kept: list[str]
) -> None:
    """The HDR10 request may not return HDR10+, and the conversion needs it as its base.

    When that request already returned HDR10+, there is nothing more to ask for.
    """
    calls: list[str] = []

    def fetch(title: Any, codec: Any, range_: Video.Range) -> Tracks:
        calls.append(range_.name)
        if range_ == Video.Range.DV and not dv_available:
            raise ValueError("no DV")
        tracks = Tracks()
        for name in hdr10_gives if range_ == Video.Range.HDR10 else [range_.name]:
            tracks.add(Video(url="https://example.invalid/v", language="en", id_=name, range_=Video.Range[name]))
        return tracks

    service = SimpleNamespace(
        track_request=TrackRequest(ranges=[Video.Range.HYBRID], generate_dv=to_dv),
        log=logging.getLogger("variants-test"),
    )

    tracks = Service.get_tracks_for_variants(service, None, fetch)  # type: ignore[arg-type]

    assert calls == fetched
    assert sorted(v.range.name for v in tracks.videos) == kept


def test_the_hybrid_step_gets_the_base_alone_when_there_is_no_dv(tmp_path: Path) -> None:
    base = SimpleNamespace(id="base", path=tmp_path / "base.mp4", height=2160, range=Video.Range.HDR10P)

    tracks = hybrid_source_tracks(base, None)  # type: ignore[arg-type]

    assert len(tracks) == 1 and tracks[0].range == Video.Range.HDR10P and tracks[0].needs_duration_fix


def test_the_api_needs_hybrid_for_generate_dv() -> None:
    assert "HYBRID" in (validate_download_parameters({"generate_dv": True}) or "")
    assert "HYBRID" in (validate_download_parameters({"generate_dv": True, "range": ["HDR10P"]}) or "")
    assert validate_download_parameters({"generate_dv": True, "range": ["HYBRID"]}) is None
    assert "boolean" in (validate_download_parameters({"generate_dv": "yes", "range": ["HYBRID"]}) or "")


def test_the_api_takes_hybrid_from_the_serve_default_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "serve", {"range": ["HYBRID"]})
    assert validate_download_parameters({"generate_dv": True}) is None
    assert "HYBRID" in (validate_download_parameters({"generate_dv": True, "range": ["HDR10P"]}) or "")


class _Captured(Exception):
    pass


@pytest.mark.parametrize("to_dv", [False, True])
def test_a_rest_job_gives_the_flag_to_the_service_ctx_and_to_dl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, to_dv: bool
) -> None:
    """The REST manager calls dl.result() directly, so the flag must take both of the CLI's paths."""
    import click

    class FakeDl:
        cli = click.Command("dl")

        def __init__(self, ctx: click.Context, **kwargs: object) -> None:
            self.ctx = ctx

        def result(self, **kwargs: object) -> None:
            raise _Captured(self.ctx.params["generate_dv"], kwargs["generate_dv"])

    class FakeService:
        def __init__(self, ctx: click.Context) -> None:
            pass

    monkeypatch.setattr(dl_module, "dl", FakeDl)
    monkeypatch.setattr(Services, "get_path", staticmethod(lambda s: tmp_path))
    monkeypatch.setattr(Services, "load", staticmethod(lambda s: FakeService))
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *a: None)
    params = {"generate_dv": True} if to_dv else {}

    with pytest.raises(_Captured) as exc:
        download_manager.perform_download("job-1", "EXAMPLE", "t1", params)

    assert exc.value.args == (to_dv, to_dv)


@pytest.mark.parametrize("to_dv", [False, True])
def test_the_remote_client_forwards_the_flag_only_when_set(to_dv: bool) -> None:
    data = _sent_create_data({"generate_dv": to_dv}, {})
    assert data.get("generate_dv", False) is to_dv


@pytest.mark.parametrize("to_dv", [False, True])
def test_a_remote_session_puts_the_flag_in_the_service_ctx(monkeypatch: pytest.MonkeyPatch, to_dv: bool) -> None:
    seen: dict[str, Any] = {}

    def fake_instantiate(parent_ctx: Any, *args: Any) -> object:
        seen["ctx"] = parent_ctx
        return object()

    monkeypatch.setattr(handlers, "load_service_yaml", lambda service: {})
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *args: None)
    monkeypatch.setattr(handlers.Services, "load", lambda service: None)
    monkeypatch.setattr(handlers, "instantiate_service", fake_instantiate)
    data = {"generate_dv": True} if to_dv else {}

    handlers.create_service_instance("EXAMPLE", "title", data, None, [], None)

    assert seen["ctx"].params["generate_dv"] is to_dv


def video(
    id_: str,
    range_: Video.Range,
    height: int = 2160,
    codec: Optional[Video.Codec] = Video.Codec.HEVC,
    native_hybrid: bool = False,
) -> Any:
    return SimpleNamespace(id=id_, height=height, range=range_, codec=codec, native_hybrid=native_hybrid)


def test_hybrid_fails_with_neither_a_base_nor_dv() -> None:
    assert hybrid_gate_failure([video("sdr", Video.Range.SDR)], True) == (
        "HYBRID needs HDR10/HDR10+ and DV tracks, available ranges: SDR"
    )
    assert hybrid_gate_failure([], True) == "HYBRID needs HDR10/HDR10+ and DV tracks, available ranges: none"


def test_hybrid_fails_with_dv_only() -> None:
    assert hybrid_gate_failure([video("dv", Video.Range.DV)], True) == (
        "HYBRID needs HDR10/HDR10+ and DV tracks, available ranges: DV"
    )


def test_hybrid_goes_on_without_dv_when_the_flag_has_an_hdr10plus_base() -> None:
    assert hybrid_gate_failure([video("p", Video.Range.HDR10P), video("h", Video.Range.HDR10)], True) is None


def test_hybrid_goes_on_with_a_base_and_dv_whatever_the_flag() -> None:
    videos = [video("h", Video.Range.HDR10), video("dv", Video.Range.DV)]
    assert hybrid_gate_failure(videos, False) is None
    assert hybrid_gate_failure(videos, True) is None


def test_hybrid_fails_up_front_when_no_base_is_hevc() -> None:
    """dovi_tool injects into HEVC, so an all-AV1 title fails before it downloads anything."""
    videos = [video("av1", Video.Range.HDR10P, codec=Video.Codec.AV1), video("dv", Video.Range.DV)]
    assert hybrid_gate_failure(videos, False) == "HYBRID needs an HEVC HDR10/HDR10+ base, available codecs: AV1"
    assert hybrid_gate_failure(videos + [video("h", Video.Range.HDR10, codec=None)], False) is None


@pytest.mark.parametrize(
    "ranges, to_dv, detail",
    [
        ([Video.Range.HDR10P], False, "HDR10P; add --generate-dv to make DV from HDR10+"),
        ([Video.Range.HDR10], False, "HDR10"),
        ([Video.Range.HDR10], True, "HDR10; --generate-dv found no HEVC HDR10+ track"),
    ],
)
def test_hybrid_fails_without_dv_and_hints_at_the_flag_only_when_it_is_off(
    ranges: list[Video.Range], to_dv: bool, detail: str
) -> None:
    """A service that fetches HYBRID by hand may never return HDR10+; the hint must not blame the user then."""
    videos = [video(r.name, r) for r in ranges]
    assert hybrid_gate_failure(videos, to_dv) == f"HYBRID needs HDR10/HDR10+ and DV tracks, available ranges: {detail}"


@pytest.mark.parametrize("to_dv", [False, True])
def test_real_dv_takes_every_base_and_the_lowest_dv(to_dv: bool) -> None:
    videos = [
        video("h2160", Video.Range.HDR10),
        video("p1080", Video.Range.HDR10P, 1080),
        video("dv2160", Video.Range.DV),
        video("dv1080", Video.Range.DV, 1080),
    ]

    bases, dv = hybrid_plan(videos, to_dv)

    assert [v.id for v in bases] == ["h2160", "p1080"]
    assert dv is not None and dv.id == "dv1080"


def test_the_flag_without_dv_takes_only_hdr10plus_bases() -> None:
    videos = [video("h2160", Video.Range.HDR10), video("p1080", Video.Range.HDR10P, 1080)]

    bases, dv = hybrid_plan(videos, True)

    assert [v.id for v in bases] == ["p1080"] and dv is None


def test_no_dv_and_no_flag_gives_no_bases() -> None:
    assert hybrid_plan([video("h", Video.Range.HDR10), video("p", Video.Range.HDR10P)], False) == ([], None)


def test_a_native_hybrid_passes_the_gate_and_takes_no_base_at_its_resolution() -> None:
    videos = [video("dvp2160", Video.Range.DV, native_hybrid=True), video("h1080", Video.Range.HDR10, 1080)]
    assert hybrid_gate_failure(videos[:1], False) is None

    bases, dv = hybrid_plan(videos + [video("p2160", Video.Range.HDR10P)], False)

    assert [v.id for v in bases] == ["h1080"]
    assert dv is not None and dv.id == "dvp2160"


@pytest.mark.parametrize(
    "ranges, codecs, surround_on",
    [
        ([Video.Range.SDR, Video.Range.HDR10], [], Video.Range.HDR10),
        ([Video.Range.HYBRID], [Video.Codec.AVC, Video.Codec.HEVC], Video.Range.DV),
    ],
)
def test_variants_merge_every_manifest_but_skip_a_subtitle_copy_and_later_chapters(
    ranges: list[Video.Range], codecs: list[Video.Codec], surround_on: Video.Range
) -> None:
    """Audio and a subtitle language that only a later manifest offers are kept. A track whose ID
    differs per manifest (a signed URL) is not duplicated, and later chapters do not clash."""
    fetches = 0

    def fetch(title: Any, codec: Any, range_: Video.Range) -> Tracks:
        nonlocal fetches
        fetches += 1
        tracks = Tracks()
        tracks.add(
            Video(
                url="https://example.invalid/v", language="en", id_=f"{range_.name}{codec}", codec=codec, range_=range_
            )
        )
        tracks.add(Audio(url="https://example.invalid/a2", language="en", id_=f"stereo{fetches}", channels=2))
        if range_ == surround_on:
            tracks.add(Audio(url="https://example.invalid/a6", language="en", id_="surround", channels=6))
        tracks.add(Subtitle(url=f"https://example.invalid/s?tok={fetches}", language="en", id_=f"sub{fetches}"))
        if range_ == surround_on:
            tracks.add(Subtitle(url=f"https://example.invalid/j?tok={fetches}", language="ja", id_=f"ja{fetches}"))
        tracks.add(Chapter(0))
        return tracks

    service = SimpleNamespace(
        track_request=TrackRequest(codecs=codecs, ranges=ranges),
        log=logging.getLogger("variants-test"),
    )

    tracks = Service.get_tracks_for_variants(service, None, fetch)  # type: ignore[arg-type]

    assert fetches > 1
    assert sorted(a.id for a in tracks.audio) == ["stereo1", "surround"]
    assert [str(s.language) for s in tracks.subtitles] == ["en", "ja"]
    assert tracks.subtitles[0].id == "sub1"
    assert len(tracks.chapters) == 1
    assert len(tracks.videos) == fetches
