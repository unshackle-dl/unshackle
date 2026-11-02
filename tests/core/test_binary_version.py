import subprocess
from pathlib import Path
from typing import Any

import pytest

from unshackle.core import binaries
from unshackle.core.binaries import binary_version, parse_version


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ffmpeg version n9.0.1-11-ge47273f4d9 Copyright (c) 2000-2026\nbuilt with gcc 15.2.0", "9.0.1"),
        ("ffmpeg version 2024-03-07-git-97beb63a66-full_build\n  built with gcc 12.2.0", None),
        ("ffmpeg version N-113672-g1e2a6dbb46-20240307\n  built with gcc 13.2.0", None),
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


def test_bare_call_only_for_listed_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(cmd)
        out = "MP4 Decrypter - Version 1.4\nusage: tool [options]" if len(cmd) == 1 else "ERROR: missing output"
        return subprocess.CompletedProcess(cmd, 1, "", out)

    monkeypatch.setattr(binaries.subprocess, "run", run)

    listed, other = Path("/opt/bin/mp4decrypt"), Path("/opt/bin/othertool")
    assert binary_version(listed) == "1.4"
    assert calls == [[str(listed)]]

    calls.clear()
    assert binary_version(other) is None
    assert calls == [[str(other), "--version"], [str(other), "-version"]]


def test_timeout_stops_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(binaries.subprocess, "run", run)

    assert binary_version(Path("/opt/bin/othertool")) is None
    assert len(calls) == 1
