"""Suite-wide test setup."""

from __future__ import annotations

import socket
import socketserver
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.update_checker import UpdateChecker

# shutdown() blocks until the serve loop polls again, and the 0.5s default is paid by every
# test server at teardown. The default changes for the whole process, servers in core included.
socketserver.BaseServer.serve_forever.__defaults__ = (0.01,)


@pytest.fixture(autouse=True)
def loopback_only(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail a name lookup for any host but loopback, so a stray request cannot hang a unit test."""
    if request.node.get_closest_marker("live") or request.node.get_closest_marker("slow"):
        return
    resolve = socket.getaddrinfo

    def offline(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in (None, "", "localhost", "127.0.0.1", "::1"):
            raise socket.gaierror(socket.EAI_NONAME, f"unit tests are offline: {host!r} is not loopback")
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", offline)


@pytest.fixture(autouse=True)
def no_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the release lookup of the health route off the network."""

    monkeypatch.setattr(UpdateChecker, "check_for_updates_sync", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def default_muxing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mux with the default options, whatever the muxing block of the local unshackle.yaml sets."""
    monkeypatch.setattr(config, "muxing", {})


@pytest.fixture(autouse=True)
def default_codec_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name codecs with the built-in labels, whatever codec_names the local unshackle.yaml sets."""
    monkeypatch.setattr(config, "codec_names", {})


@pytest.fixture
def mux_args(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[str]]:
    """Run Tracks.mux up to mkvmerge and return its arguments; ``ids`` is what the track ID probe reports."""

    class Stop(Exception):
        pass

    def run(tracks: Any, ids: Optional[dict[str, int]] = None, **kwargs: Any) -> list[str]:
        captured: list[list[str]] = []

        def fake_popen(args: list[str], **_: object) -> None:
            captured.append(args)
            raise Stop

        monkeypatch.setattr("unshackle.core.tracks.tracks.subprocess.Popen", fake_popen)
        monkeypatch.setattr("unshackle.core.tracks.tracks.file_track_ids", lambda path: ids or {"video": 0})
        monkeypatch.setattr(binaries, "MKVToolNix", Path("mkvmerge"))
        with pytest.raises(Stop):
            tracks.mux("Title", delete=False, **kwargs)
        return captured[0]

    return run
