"""The banner prints once above top-level help and once above a subcommand's help."""

from __future__ import annotations

import io
import sys

import pytest
from click.testing import CliRunner

from unshackle.core import __version__
from unshackle.core.__main__ import main, print_banner
from unshackle.core.config import config
from unshackle.core.console import force_utf8_streams


@pytest.mark.parametrize("args", [["--help"], ["env", "--help"]])
def test_help_prints_the_banner_once(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    monkeypatch.setattr(config, "update_checks", False)

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0, result.output
    assert result.output.count(f"v {__version__}") == 1
    assert "Usage:" in result.output


def test_banner_prints_to_a_legacy_code_page_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buffer, encoding="cp1252"))

    force_utf8_streams()
    print_banner()

    assert "▄".encode() in buffer.getvalue()
