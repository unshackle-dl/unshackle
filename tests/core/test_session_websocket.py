"""A websocket opened through the rnet session: its handshake, its frames and its debug entry."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import rnet
from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.core.test_request_record import closed_port
from tests.core.test_request_record import debug_log as debug_log
from tests.core.test_request_record import records
from unshackle.core.session import RnetSession

pytestmark = pytest.mark.unit

DEADLINE = 10.0
GIVEN = {"User-Agent": "music/9 app/14", "Authorization": "Bearer example"}
SESSION = {"User-Agent": "session-agent", "Connection": "Keep-Alive", "X-Session": "1"}


async def call(function: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking call off the loop, with a deadline, so a regression fails instead of hanging."""
    return await asyncio.wait_for(asyncio.to_thread(function, *args), DEADLINE)


class Seen:
    """What the loopback server got: the raw lines of the handshake, and an event set at the first pong."""

    def __init__(self) -> None:
        self.lines: list[tuple[bytes, bytes]] = []
        self.pong = asyncio.Event()

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        self.lines = list(request.raw_headers)
        socket = web.WebSocketResponse(autoping=False)
        await socket.prepare(request)
        await socket.send_str("first")
        if request.path == "/ping":
            await socket.ping(b"probe")
        async for message in socket:
            if message.type == web.WSMsgType.PONG:
                self.pong.set()
        return socket


async def serve(seen: Seen) -> tuple[TestServer, Callable[[str], str]]:
    app = web.Application()
    app.router.add_get("/{tail:.*}", seen.handle)
    server = TestServer(app)
    await server.start_server()
    return server, lambda path: str(server.make_url(path)).replace("http://", "ws://", 1)


async def test_the_handshake_carries_the_headers_given_and_none_of_the_session() -> None:
    seen = Seen()
    server, url = await serve(seen)
    session = RnetSession()
    session.headers.update(SESSION)

    socket = await call(session.websocket, url("/"), GIVEN)
    first = await call(socket.recv)
    await call(socket.close)
    await server.close()

    names = [name.decode() for name, _ in seen.lines]
    values = {name.decode(): value for name, value in seen.lines}
    assert socket.status == 101 and first.text == "first"
    assert names[:2] == ["User-Agent", "Authorization"]
    assert {"upgrade", "connection", "sec-websocket-key", "sec-websocket-version", "host"} <= {
        name.lower() for name in names
    }
    assert values["User-Agent"] == b"music/9 app/14" and "x-session" not in {name.lower() for name in names}
    assert not set(SESSION.values()) & set(values.values())


async def test_a_ping_comes_to_the_caller_and_a_pong_goes_back() -> None:
    seen = Seen()
    server, url = await serve(seen)

    socket = await call(RnetSession().websocket, url("/ping"), GIVEN)
    first = await call(socket.recv)
    ping = await call(socket.recv)
    await call(socket.send, rnet.Message.from_pong(ping.ping or b""))
    await asyncio.wait_for(seen.pong.wait(), DEADLINE)
    await call(socket.close)
    await server.close()

    assert first.text == "first"
    assert ping.ping == b"probe"


async def test_each_handshake_writes_one_debug_entry(debug_log: Path) -> None:
    server, url = await serve(Seen())
    session = RnetSession()

    socket = await call(session.websocket, url("/"), GIVEN)
    await call(socket.recv)
    await call(socket.close)
    await server.close()
    with pytest.raises(rnet.ConnectionError):
        await call(session.websocket, closed_port().replace("http://", "ws://", 1), GIVEN)

    assert [
        (entry["request"]["method"], entry["response"]["status"], entry["failure"]) for entry in records(debug_log)
    ] == [
        ("GET", 101, None),
        ("GET", None, "ConnectionError"),
    ]
