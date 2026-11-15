import os

import pytest

from unshackle.core import console as console_module
from unshackle.core.config import config
from unshackle.core.console import console, print_listing


@pytest.mark.parametrize("wide, lines", [(True, 1), (False, 2)])
def test_wide_output_prints_a_listing_at_the_terminal_width(
    monkeypatch: pytest.MonkeyPatch, wide: bool, lines: int
) -> None:
    monkeypatch.setattr(config, "wide_output", wide)
    monkeypatch.setattr(console_module.shutil, "get_terminal_size", lambda: os.terminal_size((140, 40)))
    monkeypatch.setattr(console, "width", 80)
    with console.capture() as capture:
        print_listing("x" * 100)
    assert len(capture.get().strip("\n").splitlines()) == lines
    assert console.width == 80


def test_print_wide_keeps_a_legacy_windows_console_at_its_width(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_module.shutil, "get_terminal_size", lambda: os.terminal_size((140, 40)))
    monkeypatch.setattr(console, "legacy_windows", True)
    monkeypatch.setattr(console, "_width", 80)
    monkeypatch.setattr(console, "_height", 25)
    with console.capture():
        for _ in range(3):
            console_module.print_wide("x")
    assert console._width == 80
