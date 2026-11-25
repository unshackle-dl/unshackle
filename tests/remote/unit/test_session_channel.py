"""The server side of the remote session channel: what it sends on attach, and how it lets go of a socket."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import pytest

from tests.remote.unit.test_tunnel import wait_until
from unshackle.core.api.input_bridge import InputBridge
from unshackle.core.api.session_log import SessionLogBuffer
from unshackle.core.api.session_store import SessionEntry

pytestmark = pytest.mark.unit


class FakeSocket:
    """A server WebSocket that records what it sends, and ends its read loop when it closes."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = asyncio.Event()

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000, message: bytes = b"") -> bool:
        self.closed.set()
        return True

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> Any:
        await self.closed.wait()
        raise StopAsyncIteration


def entry_with_logs(count: int) -> SessionEntry:
    entry = SessionEntry("s", "EXAMPLE", None, log_buffer=SessionLogBuffer(), input_bridge=InputBridge())
    for n in range(count):
        entry.log_buffer.append(logging.INFO, f"EXAMPLE line {n + 1}")
    return entry


def log_seqs(socket: FakeSocket) -> list[int]:
    return [m["seq"] for m in socket.sent if m["type"] == "log"]


async def test_the_backlog_goes_before_a_record_logged_while_the_old_socket_closes() -> None:
    entry = entry_with_logs(4)
    old = FakeSocket()

    async def close_slowly(code: int = 1000, message: bytes = b"") -> bool:
        entry.log_buffer.append(logging.INFO, "EXAMPLE line 5")
        await asyncio.sleep(0)
        old.closed.set()
        return True

    old.close = close_slowly  # type: ignore[method-assign]
    entry.channel.ws = old  # type: ignore[assignment]
    new = FakeSocket()
    serving = asyncio.create_task(entry.channel.serve(new, entry, 2, False))  # type: ignore[arg-type]
    for _ in range(200):
        if len(log_seqs(new)) >= 3:
            break
        await asyncio.sleep(0.01)
    assert log_seqs(new) == [3, 4, 5]
    assert {"type": "status", "status": "authenticating"} in new.sent
    await new.close()
    await serving
    assert not entry.channel.attached


async def test_a_socket_that_cannot_send_is_closed_and_detached() -> None:
    entry = entry_with_logs(1)
    broken = FakeSocket()

    async def fail(message: dict[str, Any]) -> None:
        raise RuntimeError("EXAMPLE socket gone")

    broken.send_json = fail  # type: ignore[method-assign]
    await asyncio.wait_for(entry.channel.serve(broken, entry, 0, False), 5)  # type: ignore[arg-type]
    assert broken.closed.is_set()
    assert not entry.channel.attached


async def test_a_detached_channel_hands_its_waiting_asks_back_to_the_prompt_routes() -> None:
    entry = entry_with_logs(0)
    bridge = entry.input_bridge
    assert bridge is not None
    socket = FakeSocket()
    serving = asyncio.create_task(entry.channel.serve(socket, entry, 0, False))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    answers: list[Any] = []
    worker = threading.Thread(target=lambda: answers.append(bridge.request_cdm({"op": "close"}, timeout=5)))
    worker.start()
    for _ in range(200):
        if any(m["type"] == "cdm_call" for m in socket.sent):
            break
        await asyncio.sleep(0.01)
    assert bridge.get_pending_cdm_call() is None, "the channel carries the call, so polling must not see it"
    await socket.close()
    await serving
    assert bridge.get_pending_cdm_call() == {"op": "close"}
    assert bridge.submit_response('{"op": "close"}')
    worker.join(5)
    assert answers == [{"op": "close"}]


def test_a_prompt_the_client_refuses_stops_the_service_at_once() -> None:
    bridge = InputBridge()
    offered: list[Any] = []
    bridge.channel = type("Attached", (), {"attached": True, "offer": lambda self, ask: offered.append(ask) or True})()
    failed: list[BaseException] = []

    def ask() -> None:
        try:
            bridge.request_input("Enter the EXAMPLE code", timeout=30)
        except RuntimeError as e:
            failed.append(e)

    worker = threading.Thread(target=ask)
    worker.start()
    assert wait_until(lambda: bool(offered), 5)
    assert bridge.answer(offered[0].id, "", "stdin is closed")
    worker.join(5)
    assert not worker.is_alive()
    assert "stdin is closed" in str(failed[0])
