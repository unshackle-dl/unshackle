import sys
import time
from pathlib import Path
from typing import Callable, Optional

import pytest

from unshackle.core import binaries
from unshackle.core.binaries import binary_version, parse_version, probe_output


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ffmpeg version n9.0.1-11-ge47273f4d9 Copyright (c) 2000-2026\nbuilt with gcc 15.2.0", "9.0.1"),
        ("ffmpeg version 2024-03-07-git-97beb63a66-full_build\n  built with gcc 12.2.0", "20240307"),
        ("ffmpeg version N-113672-g1e2a6dbb46-20240307\n  built with gcc 13.2.0", "20240307"),
        ("ffprobe version N-119416-g1dbc5675c1-20250503 Copyright (c) 2007-2025", "20250503"),
        ("ffmpeg version N-113672-g1e2a6dbb46 Copyright (c) 2000-2024", "N-113672"),
        ("ffmpeg version N-128263-g9f9b9548583-tessus  Copyright (c) 2000-2026", "N-128263"),
        ("ffmpeg version N-71064-gd5e603ddc0-static  Copyright (c) 2000-2024", "N-71064"),
        ("ffmpeg version git-2026-11-02-b034eb5 Copyright (c) 2000-2026", "20261102"),
        ("packager-linux-x64 version v2.6.1-634af65-release", "2.6.1"),
        ("MP4 Decrypter - Version 1.4\n(Bento4 Version 1.6.0.0)", "1.4"),
        ("mkvmerge v88.0 ('All I Know') 64-bit", "88.0"),
        ("mpv 0.34.1 Copyright 2000-2021\nFFmpeg library versions:\n   libavutil 56.70.100", "0.34.1"),
        ("mpv 0.34.1 Copyright 2000-2021\nFFmpeg version: 4.4.2", "0.34.1"),
        ("\nffmpeg version 7.0.2-static Copyright (c) 2000-2024", "7.0.2"),
        ("unknown option: --version\nusage: tool [--listen 127.0.0.1:8080]", None),
        ("dovi_tool e617c48", None),
        ("CCExtractor 0.93, Carlos Fernandez Sanz\nCCExtractor detailed version info\n\tVersion: 0.93", "0.93"),
    ],
)
def test_parse_version(text: str, expected: str | None) -> None:
    assert parse_version(text) == expected


def fake_probe(outputs: dict[str, Optional[str]], calls: list[list[str]]) -> Callable[[Path, list[str]], Optional[str]]:
    def probe(path: Path, args: list[str]) -> Optional[str]:
        calls.append(args)
        return outputs.get(" ".join(args), "")

    return probe


def test_bare_call_only_for_listed_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    outputs = {"": "MP4 Decrypter - Version 1.4\nusage: tool [options]", "--version": "ERROR: missing output"}
    monkeypatch.setattr(binaries, "probe_output", fake_probe(outputs, calls))

    assert binary_version(Path("/opt/bin/mp4decrypt")) == "1.4"
    assert calls == [[]]

    calls.clear()
    assert binary_version(Path("/opt/bin/othertool")) is None
    assert calls == [["--version"], ["-version"]]


def test_timeout_stops_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(binaries, "probe_output", fake_probe({"--version": None}, calls))

    assert binary_version(Path("/opt/bin/othertool")) is None
    assert calls == [["--version"]]


def test_timeout_kills_the_whole_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(binaries, "PROBE_TIMEOUT", 0.5)
    if sys.platform == "win32":
        tool = tmp_path / "wrapper.bat"
        tool.write_text("@echo off\r\nping -n 30 127.0.0.1 >nul\r\n")
    else:
        tool = tmp_path / "wrapper"
        tool.write_text("#!/bin/sh\nsleep 30 &\nsleep 30\n")
        tool.chmod(0o755)

    start = time.monotonic()
    assert probe_output(tool, ["--version"]) is None
    assert time.monotonic() - start < 10
