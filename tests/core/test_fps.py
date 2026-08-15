import warnings

import pytest

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
