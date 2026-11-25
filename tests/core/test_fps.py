import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

from unshackle.core.tracks import Tracks, Video
from unshackle.core.utilities import FPS


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("24", 24),
        ("23.976", pytest.approx(23.976)),
        ("30000/1001", pytest.approx(29.97, abs=0.001)),
    ],
)
def test_parse_pins_results_without_deprecation_warnings(expr: str, expected: object) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert FPS.parse(expr) == expected


def test_parse_rejects_non_numeric_constant() -> None:
    with pytest.raises(ValueError, match="Invalid fps value"):
        FPS.parse("'24'")


def test_parse_rejects_non_division_operation() -> None:
    with pytest.raises(ValueError, match="Invalid operation"):
        FPS.parse("24+1")


@pytest.mark.parametrize(
    ("fps", "expected"),
    [
        (24000 / 1001, "24000/1001"),
        (23.976, "24000/1001"),
        (23.97, "24000/1001"),
        (59.94, "60000/1001"),
        (24.0, "24"),
        (25, "25"),
        (5, "5"),
        (12.5, "25/2"),
    ],
)
def test_exact_keeps_ntsc_rates(fps: float, expected: str) -> None:
    assert str(FPS.exact(fps)) == expected


def test_mux_passes_the_exact_rate_as_default_duration(tmp_path: Path, mux_args: Callable[..., list[str]]) -> None:
    video = Video(id_="v1", url="https://example.test/v", codec=Video.Codec.HEVC, language="en", fps="24000/1001")
    video.needs_duration_fix = True
    video.path = tmp_path / "v.hevc"
    video.path.write_bytes(b"\x00" * 16)
    args = mux_args(Tracks(video))
    assert args[args.index("--default-duration") + 1] == "0:24000/1001p"
