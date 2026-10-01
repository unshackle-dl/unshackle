"""The banner prints once above top-level help and once above a subcommand's help."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from unshackle.core import __version__
from unshackle.core.__main__ import main
from unshackle.core.config import config


@pytest.mark.parametrize("args", [["--help"], ["env", "--help"]])
def test_help_prints_the_banner_once(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    monkeypatch.setattr(config, "update_checks", False)

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0, result.output
    assert result.output.count(f"v {__version__}") == 1
    assert "Usage:" in result.output
