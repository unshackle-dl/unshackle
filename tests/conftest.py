"""Suite-wide test setup."""

from __future__ import annotations

import socket
import socketserver
from typing import Any

import pytest

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

    async def no_release(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(UpdateChecker, "check_for_updates", no_release)
