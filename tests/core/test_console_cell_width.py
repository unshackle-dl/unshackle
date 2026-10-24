"""Rich measures Indic spacing marks as one cell, so a panel border stays aligned after such a label."""

from __future__ import annotations

import io

from rich.cells import cell_len
from rich.console import Console
from rich.panel import Panel

import unshackle.core.console  # noqa: F401  # installs the cell width override


def test_spacing_marks_take_one_cell() -> None:
    assert cell_len("हिंदी") == 4  # two Mc marks count, the Mn anusvara does not
    assert cell_len("தமிழ்") == 4
    assert cell_len("ไทย") == 3


def test_panel_pads_an_indic_label_to_the_border() -> None:
    out = io.StringIO()
    Console(file=out, width=20, color_system=None).print(Panel("हिंदी"))

    assert out.getvalue().splitlines()[1] == "│ हिंदी" + " " * 12 + " │"


def test_a_virama_conjunct_shares_one_cell() -> None:
    assert cell_len("हिन्दी") == 4  # न्द is one conjunct
    assert cell_len("ಕನ್ನಡ") == 4  # a Kannada virama does not join, so ನ್ನ keeps two cells
