"""Tests for the ``unshackle import`` command: it reads every export format and forwards to ``dl``."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from unshackle.commands.dl import dl

KID = "00000000000000000000000000000001"
KEY = "aa" * 16

import_cli = importlib.import_module("unshackle.commands.import").ImportCommand.cli

MEDIAEXPORT = {
    "kind": "mediaexport",
    "version": 1,
    "service": {"tag": "EXAMPLE", "name": "Example"},
    "titles": [
        {
            "id": "movie-1",
            "kind": "movie",
            "title": "Example Movie",
            "manifests": [{"url": "https://example.test/m.mpd", "type": "dash", "role": "primary"}],
            "keys": {KID: KEY},
        }
    ],
}

UNSHACKLE_V2 = {
    "version": 2,
    "service": "EXAMPLE",
    "titles": {
        "movie-1": {
            "meta": {"type": "movie", "name": "Example Movie"},
            "manifest_url": "https://example.test/m.mpd",
            "manifest_type": "DASH",
            "tracks": {"v1": {"id": "v1", "url": "https://example.test/m.mpd", "keys": {KID: KEY}}},
        }
    },
}

UNIDL_V1 = {
    "kind": "unidl-export",
    "version": 1,
    "service": "EXAMPLE",
    "titles": [
        {
            "title": {"id": "movie-1", "kind": "movie", "name": "Example Movie"},
            "manifest_url": "https://example.test/m.mpd",
            "keys": [f"{KID}:{KEY}"],
        }
    ],
}


@pytest.fixture
def forwarded(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The argument lists the import command sends to ``dl``."""
    calls: list[list[str]] = []

    def fake_main(*, args: list[str], **_: Any) -> None:
        calls.append(list(args))

    monkeypatch.setattr(dl.cli, "main", fake_main)
    return calls


@pytest.mark.parametrize("raw", [MEDIAEXPORT, UNSHACKLE_V2, UNIDL_V1], ids=["mediaexport", "unshackle-v2", "unidl-v1"])
def test_import_forwards_service_tag(tmp_path: Path, forwarded: list[list[str]], raw: dict[str, Any]) -> None:
    export = tmp_path / "export.json"
    export.write_text(json.dumps(raw), encoding="utf-8")

    result = CliRunner().invoke(import_cli, [str(export), "-r", "HDR10"])

    assert result.exit_code == 0, result.output
    assert forwarded == [["-r", "HDR10", "--import", str(export), "EXAMPLE"]]


def test_import_rejects_a_file_that_is_not_an_export(tmp_path: Path, forwarded: list[list[str]]) -> None:
    export = tmp_path / "export.json"
    export.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    # rich-click wraps the error panel to the terminal width, so a long tmp path can split the phrase in the output
    result = CliRunner().invoke(import_cli, [str(export)], standalone_mode=False)

    assert result.exit_code != 0
    assert isinstance(result.exception, click.ClickException)
    assert "is not a usable export file" in result.exception.message
    assert forwarded == []
