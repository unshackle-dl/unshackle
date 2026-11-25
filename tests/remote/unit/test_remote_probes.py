"""The background and probe calls of a remote session go through RemoteClient and so rotate the auth header."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest
import responses

from tests.remote.unit.test_session_reopen import SERVER, make_service
from unshackle.core import remote_channel, remote_service
from unshackle.core.remote_channel import PollingChannel, ServerMessages, WebSocketChannel
from unshackle.core.remote_protocol import ClientErrorKind
from unshackle.core.remote_service import RemoteClient

pytestmark = pytest.mark.unit

KEY = "k"


@pytest.fixture(autouse=True)
def no_reopen_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give up on the first unanswered health probe, so a regression fails fast instead of waiting 30 minutes."""
    monkeypatch.setattr(remote_service, "REOPEN_WAIT", 0.0)


def second_header_only(body: dict, hit: Callable[[], None] = lambda: None) -> Callable[[Any], tuple]:
    """A route that refuses the first candidate header, as a server that reads only X-Api-Key does."""

    def answer(request: Any) -> tuple:
        if request.headers.get("X-Api-Key") != KEY:
            return 401, {}, json.dumps({"status": 401, "message": "Secret Key is Invalid."})
        hit()
        return 200, {"Content-Type": "application/json"}, json.dumps(body)

    return answer


@responses.activate
def test_wait_for_server_moves_to_the_next_auth_header() -> None:
    service = make_service()
    responses.add_callback(responses.GET, f"{SERVER}/api/health", callback=second_header_only({"status": "ok"}))

    assert service.wait_for_server(accepting=True)
    assert [c.response.status_code for c in responses.calls] == [401, 200]


@responses.activate
def test_recover_moves_to_the_next_auth_header() -> None:
    service = make_service()
    responses.add(responses.GET, f"{SERVER}/api/health", json={"status": "ok"})
    responses.add_callback(responses.GET, f"{SERVER}/api/session/s1", callback=second_header_only({"valid": True}))

    assert service.recover("s1", ClientErrorKind.CONNECT)
    assert service._session_id == "s1"


@responses.activate
def test_server_draining_moves_to_the_next_auth_header() -> None:
    service = make_service()
    service.channel = PollingChannel(ServerMessages(service))
    responses.add_callback(responses.GET, f"{SERVER}/api/session/s1", callback=second_header_only({"draining": True}))

    assert service.server_draining()


@responses.activate
def test_keepalive_moves_to_the_next_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_channel, "KEEPALIVE_MIN_INTERVAL", 0.01)
    polling = PollingChannel(ServerMessages(make_service()))
    pinged = Event()
    info = {"valid": True, "expires_in": 0.03}
    responses.add_callback(responses.GET, f"{SERVER}/api/session/s1", callback=second_header_only(info, pinged.set))

    polling.keep_alive()
    try:
        assert pinged.wait(5)
    finally:
        polling.stop_keepalive()
    assert [c.response.status_code for c in responses.calls[:2]] == [401, 200]


@responses.activate
def test_cdm_poller_moves_to_the_next_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    polling = PollingChannel(ServerMessages(make_service()))
    stop = Event()
    answered: list[dict] = []
    monkeypatch.setattr(polling.messages, "answer_cdm", lambda call: (answered.append(call), stop.set()))
    monkeypatch.setattr(polling, "answer", lambda response: None)
    call = {"id": "c1", "op": "open"}
    reply = {"status": "pending_input", "cdm_call": call}
    responses.add_callback(responses.GET, f"{SERVER}/api/session/s1/prompt", callback=second_header_only(reply))

    poller = Thread(target=polling.poll_cdm_calls, args=(stop, 0.01), daemon=True)
    poller.start()
    try:
        assert stop.wait(5)
    finally:
        stop.set()
        poller.join(5)
    assert answered == [call]


@responses.activate
def test_recover_waits_out_a_429_instead_of_reopening(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[int] = []
    monkeypatch.setattr(remote_service, "wait_retry_after", lambda seconds, *args: waits.append(seconds))
    service = make_service()
    responses.add(responses.GET, f"{SERVER}/api/health", json={"status": "ok"})
    url = f"{SERVER}/api/session/s1"
    responses.add(responses.GET, url, status=429, headers={"Retry-After": "5"}, json={})
    responses.add(responses.GET, url, json={"valid": True})

    assert service.recover("s1", ClientErrorKind.CONNECT)
    assert waits == [5]
    assert service._session_id == "s1"


@responses.activate
def test_a_tunnel_copies_the_auth_header_the_server_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    class Tunnel:
        def __init__(self, url: str, headers: dict, proxy: Any) -> None:
            sent.append(headers)

        def start(self) -> str:
            return "t1"

    monkeypatch.setattr(remote_service, "TunnelClient", Tunnel)
    client = RemoteClient(server_url=SERVER, api_key=KEY)
    responses.add_callback(responses.GET, f"{SERVER}/api/health", callback=second_header_only({"status": "ok"}))

    remote_service.start_tunnel(client)

    assert sent[0].get("X-Api-Key") == KEY and "X-Secret-Key" not in sent[0]


def test_a_channel_closes_after_its_loop_has_closed() -> None:
    """A refused upgrade closes the loop of the channel thread before the thread ends."""
    service = make_service()
    service.ctx = SimpleNamespace(obj=None)
    messages = ServerMessages(service)
    channel = WebSocketChannel(messages, PollingChannel(messages))
    loop = asyncio.new_event_loop()
    loop.close()
    channel.loop, channel.task = loop, SimpleNamespace(cancel=lambda: None)
    channel.thread = SimpleNamespace(is_alive=lambda: True, join=lambda timeout=None: None)

    channel.close()
