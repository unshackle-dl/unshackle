import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

from unshackle.core import binaries
from unshackle.core.tracks import Tracks, Video

MASTERING = {
    "side_data_type": "Mastering display metadata",
    "red_x": "46400/65536",
    "red_y": "19137/65536",
    "green_x": "11142/65536",
    "green_y": "52233/65536",
    "blue_x": "8586/65536",
    "blue_y": "3015/65536",
    "white_point_x": "20494/65536",
    "white_point_y": "21562/65536",
    "min_luminance": "2/16384",
    "max_luminance": "256001/256",
}
NO_LIGHT_LEVEL = {"side_data_type": "Content light level metadata", "max_content": 0, "max_average": 0}


def make_video(tmp_path: Path, codec: Video.Codec, range_: Video.Range) -> Video:
    video = Video(url="https://example.test/v.m3u8", language="en", codec=codec, range_=range_)
    video.path = tmp_path / "v.mp4"
    video.path.write_bytes(b"\x00" * 16)
    return video


def probe_returns(monkeypatch: pytest.MonkeyPatch, stream: dict[str, Any]) -> None:
    monkeypatch.setattr(binaries, "FFProbe", Path("ffprobe"))
    stdout = json.dumps({"streams": [stream]})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stdout, returncode=0))


def pairs(args: list[str]) -> dict[str, str]:
    return dict(zip(args[::2], args[1::2]))


def test_hdr10_vp9_gets_cicp_and_mastering_display(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe_returns(monkeypatch, {"color_transfer": "smpte2084", "side_data_list": [MASTERING, NO_LIGHT_LEVEL]})

    args = pairs(make_video(tmp_path, Video.Codec.VP9, Video.Range.HDR10).vp9_colour_args(0))

    assert args["--colour-primaries"] == "0:9"
    assert args["--colour-transfer-characteristics"] == "0:16"
    assert args["--colour-matrix-coefficients"] == "0:9"
    assert args["--chromaticity-coordinates"] == "0:0.70801,0.29201,0.17001,0.79701,0.13101,0.04601"
    assert args["--white-colour-coordinates"] == "0:0.31271,0.32901"
    assert args["--max-luminance"] == "0:1000.003906"
    assert args["--min-luminance"] == "0:0.000122"
    assert "--max-content-light" not in args  # a zero CoLL means unknown


def test_partial_mastering_display_keeps_the_cicp_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    partial = {"side_data_type": "Mastering display metadata", "min_luminance": "1/10000", "max_luminance": "1000/1"}
    probe_returns(monkeypatch, {"color_transfer": "smpte2084", "side_data_list": [partial]})

    args = pairs(make_video(tmp_path, Video.Codec.VP9, Video.Range.HDR10).vp9_colour_args(1))

    assert args["--colour-transfer-characteristics"] == "1:16"
    assert "--max-luminance" not in args


@pytest.mark.parametrize(
    ("transfer", "range_", "expected"),
    [
        ("smpte2084", Video.Range.SDR, "0:16"),  # an unlabelled manifest still gets the file's HDR
        ("arib-std-b67", Video.Range.SDR, "0:18"),
        ("bt709", Video.Range.HDR10, None),  # a wrong HDR label never stamps PQ on SDR
        ("unknown", Video.Range.HLG, "0:18"),
        ("unknown", Video.Range.SDR, None),
    ],
)
def test_the_file_transfer_wins_over_the_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transfer: str, range_: Video.Range, expected: Optional[str]
) -> None:
    probe_returns(monkeypatch, {"color_transfer": transfer})

    args = pairs(make_video(tmp_path, Video.Codec.VP9, range_).vp9_colour_args(0))

    assert args.get("--colour-transfer-characteristics") == expected


def test_no_ffprobe_falls_back_to_the_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries, "FFProbe", None)

    args = pairs(make_video(tmp_path, Video.Codec.VP9, Video.Range.HDR10).vp9_colour_args(0))

    assert args == {
        "--colour-primaries": "0:9",
        "--colour-transfer-characteristics": "0:16",
        "--colour-matrix-coefficients": "0:9",
    }


def test_mux_passes_the_vp9_args_and_keeps_the_hevc_hlg_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mux_args: Callable[..., list[str]]
) -> None:
    monkeypatch.setattr(binaries, "FFProbe", None)

    vp9 = mux_args(Tracks(make_video(tmp_path, Video.Codec.VP9, Video.Range.HDR10)), {"video": 0})
    hevc = mux_args(Tracks(make_video(tmp_path, Video.Codec.HEVC, Video.Range.HLG)), {"video": 0})

    assert vp9[vp9.index("--colour-transfer-characteristics") + 1] == "0:16"
    assert hevc[hevc.index("--color-transfer-characteristics") + 1] == "0:18"
