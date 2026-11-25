"""The real remote client against the real server routes, over HTTP on a loopback port."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from threading import Event, Semaphore, Thread
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional

import aiohttp
import click
import pytest
import requests
from aiohttp import web
from langcodes import Language

from tests.remote.unit.test_tunnel import wait_until
from unshackle.core import remote_channel, remote_service
from unshackle.core.api import handlers, routes, session_store
from unshackle.core.api.errors import APIError
from unshackle.core.api.handlers import api_key_authentication
from unshackle.core.api.routes import cors_middleware, no_5xx_middleware, setup_routes
from unshackle.core.cdm import client_relay
from unshackle.core.config import config
from unshackle.core.remote_channel import PollingChannel, WebSocketChannel
from unshackle.core.remote_protocol import CHANNEL_ROUTE, LOGS_ROUTE, PROMPT_ROUTE, SESSION_ROUTE, APIErrorCode
from unshackle.core.remote_service import RemoteError, RemoteService
from unshackle.core.titles.movie import Movie, Movies
from unshackle.core.tracks import Audio, Chapters, Tracks
from unshackle.core.tracks.track import Track

pytestmark = pytest.mark.unit

KEY = "loopback-key"
CODE = "1234"


class StubService:
    """The server side of the remote session: a login that asks for a code, three titles, one audio track each.

    A test puts extra service work in ``hooks``: "titles" runs inside get_titles, on the service thread.
    """

    logins = 0
    hooks: dict[str, Callable[["StubService"], None]] = {}

    def __init__(self, beats: Semaphore) -> None:
        self.beats = beats
        self.log = logging.getLogger("stub-server")
        self.session = requests.Session()
        self.cache = None
        self._input_bridge: Any = None
        StubService.logins += 1
        self.login = StubService.logins

    def authenticate(self, cookies: Any, credential: Any) -> None:
        if self._input_bridge.request_input("Enter the EXAMPLE code") != CODE:
            raise ValueError("wrong code")
        self.log.info(f"Logged in to EXAMPLE, login {self.login}")

    def get_titles(self) -> Movies:
        if hook := self.hooks.get("titles"):
            hook(self)
        return Movies(
            [Movie(id_=f"example-{n}", service=StubService, name=f"Example {n}", year=2024) for n in (1, 2, 3)]
        )

    def get_tracks(self, title: Movie) -> Tracks:
        """Answer after the route sent its first heartbeat, so every tracks call outlasts one interval."""
        assert self.beats.acquire(timeout=10), "the tracks route sent no heartbeat"
        if title.id == "example-3":
            raise ValueError("No tracks for OTHER region")
        audio = Audio(
            url=f"https://cdn.example/{title.id}.m4a",
            language=Language.get("en"),
            descriptor=Track.Descriptor.URL,
            codec=Audio.Codec.AAC,
            bitrate=128_000,
            channels=2,
            id_=f"{title.id}-login{self.login}",
        )
        return Tracks([audio])

    def get_chapters(self, title: Movie) -> Chapters:
        return Chapters()

    def decrypts_samples(self, track: Any) -> bool:
        return False


@pytest.fixture
def server(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[SimpleNamespace]:
    """The real route table and API key middleware on a loopback port, with the stub as the only service.

    A test parametrized with ``server=False`` gets a gateway in front of it that answers the channel upgrade with 502.
    """
    refuse = not getattr(request, "param", True)
    beats = Semaphore(0)
    seen: list[tuple[str, str, int]] = []
    bodies: list[tuple[str, bytes]] = []

    class CountedHeartbeat(web.StreamResponse):
        """Count the first heartbeat of each tracks request; aiohttp calls prepare again when the handler returns."""

        async def prepare(self, request: web.BaseRequest) -> Any:
            first = not self.prepared
            writer = await super().prepare(request)
            if first and request.path.endswith("/tracks"):
                beats.release()
            return writer

    @web.middleware
    async def record(request: web.Request, handler: Any) -> web.StreamResponse:
        if refuse and request.path.endswith("/channel"):
            seen.append((request.method, request.path, 502))
            return web.Response(status=502, text="Bad gateway")
        try:
            response = await handler(request)
        except web.HTTPException as e:
            seen.append((request.method, request.path, e.status))
            raise
        seen.append((request.method, request.path, response.status))
        if isinstance(response, web.Response) and response.body is not None:
            bodies.append((request.path, bytes(response.body)))  # type: ignore[arg-type]
        return response

    StubService.logins = 0
    monkeypatch.setattr(StubService, "hooks", {})
    monkeypatch.setattr(routes, "HEARTBEAT_INTERVAL", 0.02)
    monkeypatch.setattr(routes.web, "StreamResponse", CountedHeartbeat)
    monkeypatch.setattr(remote_channel, "KEEPALIVE_MIN_INTERVAL", 0.05)
    monkeypatch.setattr(remote_channel, "AUTH_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(remote_service, "REOPEN_WAIT", 5.0)
    monkeypatch.setattr(session_store, "session_store", None)
    monkeypatch.setattr(config, "serve", {"session_ttl": 1.5})
    monkeypatch.setattr(config, "credentials", {})
    monkeypatch.setattr(config.directories, "cache", tmp_path / "cache")
    monkeypatch.setattr(config.directories, "cookies", tmp_path / "cookies")
    monkeypatch.setattr(handlers, "validate_service", lambda service, request=None: service)
    monkeypatch.setattr(handlers, "resolve_handler_proxy", lambda *args: (None, []))
    monkeypatch.setattr(handlers, "server_account_for", lambda *args: False)
    monkeypatch.setattr(handlers.Services, "load", lambda service: StubService)
    monkeypatch.setattr(handlers, "create_service_instance", lambda *a, **k: (StubService(beats), None, None))

    app = web.Application(middlewares=[record, cors_middleware, no_5xx_middleware, api_key_authentication])
    app["config"] = {"users": [KEY]}
    setup_routes(app, remote_only=True)
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    thread = Thread(target=loop.run_forever, daemon=True)
    thread.start()
    host, port = runner.addresses[0][:2]
    try:
        yield SimpleNamespace(url=f"http://{host}:{port}", loop=loop, seen=seen, bodies=bodies)
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        loop.close()


def make_client(url: str, cdm: Any = None) -> RemoteService:
    """A client whose first candidate header is not the one the server reads, so it must move to the second."""
    obj = SimpleNamespace(cdm=cdm, cdm_entry=False, proxy_providers=[])
    parent = click.Context(click.Command("dl"), obj=obj)
    parent.params = {"no_proxy": True}
    ctx = click.Context(click.Command("EXAMPLE"), parent=parent, obj=obj)
    service = RemoteService(ctx, "EXAMPLE", "example-1", url, KEY, {"_auth_headers": ["X-Api-Key", "X-Secret-Key"]})
    service._input_bridge = SimpleNamespace(request_input=lambda prompt: CODE)
    return service


def forget(server: SimpleNamespace, session_id: str) -> None:
    """Drop the session on the server, as a restart does."""
    store = session_store.get_session_store()
    asyncio.run_coroutine_threadsafe(store.delete(session_id), server.loop).result(10)


def on_loop(server: SimpleNamespace, coroutine: Any) -> Any:
    return asyncio.run_coroutine_threadsafe(coroutine, server.loop).result(10)


def count(server: SimpleNamespace, method: str, route: str, session_id: Optional[str] = None) -> int:
    """The requests the server saw for one route; without a session id, for any session."""
    if session_id is None:
        prefix, _, suffix = route.partition("{session_id}")
        return len(
            [
                s
                for s in server.seen
                if s[0] == method
                and s[1].startswith(prefix)
                and s[1].endswith(suffix)
                and "/" not in s[1][len(prefix) : len(s[1]) - len(suffix) or None]
            ]
        )
    return len([s for s in server.seen if s[:2] == (method, route.format(session_id=session_id))])


def example_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the client logged as the service's own lines, which the server relayed."""
    return [r.getMessage() for r in caplog.records if r.name == "EXAMPLE"]


def idle_seconds(entry: Any) -> float:
    return (datetime.now(timezone.utc) - entry.last_accessed).total_seconds()


@pytest.mark.parametrize(
    "server,channel",
    [(True, "none"), (False, "refused"), (True, "failed")],
    ids=["old-client", "refused-upgrade", "failed-connect"],
    indirect=["server"],
)
def test_a_remote_session_runs_end_to_end_over_http(
    server: SimpleNamespace, channel: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that does not open the channel, or whose upgrade is refused or fails, polls with the same replies."""
    caplog.set_level(logging.DEBUG)
    if channel == "none":
        monkeypatch.setattr(WebSocketChannel, "open", lambda self, timeout=0: False)
    if channel == "failed":

        async def unreachable(self: Any, url: str, **kwargs: Any) -> Any:
            raise aiohttp.ClientConnectionError("EXAMPLE connect failed")

        monkeypatch.setattr(aiohttp.ClientSession, "_ws_connect", unreachable)
    service = make_client(server.url)
    try:
        service.authenticate()
        first = service._session_id
        assert first
        assert server.seen[0] == ("POST", "/api/session/create", 401)
        assert ("GET", f"/api/session/{first}/prompt", 200) in server.seen
        assert isinstance(service.channel, PollingChannel)
        assert count(server, "GET", CHANNEL_ROUTE, first) == int(channel == "refused")
        channel_lines = [r for r in caplog.records if r.name == "remote_channel"]
        assert all(r.levelno == logging.DEBUG for r in channel_lines)
        assert bool(channel_lines) == (channel != "none")
        assert any(r.name == "EXAMPLE" and r.message == "Logged in to EXAMPLE, login 1" for r in caplog.records)

        info = SESSION_ROUTE.format(session_id=first)
        assert wait_until(lambda: len([s for s in server.seen if s[:2] == ("GET", info)]) >= 2, 10)

        titles = service.get_titles()
        assert [str(t.id) for t in titles] == ["example-1", "example-2", "example-3"]
        m1, m2, m3 = titles
        assert [str(t.id) for t in service.get_tracks(m1).audio] == ["example-1-login1"]

        forget(server, first)
        assert [str(t.id) for t in service.get_tracks(m2).audio] == ["example-2-login2"]
        assert service._session_id not in (None, first)
        assert "The remote session was lost, opening a new one" in caplog.messages

        with pytest.raises(RemoteError) as late:
            service.get_tracks(m3)
        assert late.value.kind not in (APIErrorCode.SESSION_NOT_FOUND, "UNKNOWN")
        assert not late.value.session_broke
        assert [s for s in server.seen if s[1].endswith("/tracks")][-1][2] == 200
    finally:
        second = service._session_id
        service.close()
    assert service.channel is None
    assert ("DELETE", SESSION_ROUTE.format(session_id=second), 200) in server.seen
    assert not session_store.get_session_store()._sessions
    prompts = {body for path, body in server.bodies if path.endswith("/prompt")}
    assert prompts <= {
        b'{"status": "authenticating"}',
        b'{"status": "pending_input", "prompt": "Enter the EXAMPLE code"}',
        b'{"status": "authenticated"}',
        b'{"status": "accepted"}',
    }
    assert b'{"status": "pending_input", "prompt": "Enter the EXAMPLE code"}' in prompts
    logs = [json.loads(body) for path, body in server.bodies if path.endswith("/logs")]
    assert logs and all(list(reply) == ["session_id", "logs", "last_seq"] for reply in logs)
    assert [list(r) for reply in logs for r in reply["logs"]][:1] == [["seq", "level", "message", "ts"]]


def test_the_session_channel_carries_what_the_server_starts(
    server: SimpleNamespace, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(client_relay, "answer_cdm_call", lambda cdm, call, sessions: {"op": call["op"]})
    answers: list[Any] = []
    release = Event()

    def titles(stub: StubService) -> None:
        stub.log.info("Listing EXAMPLE titles")
        assert release.wait(10)
        answers.append(stub._input_bridge.request_cdm({"op": "close", "session": "EXAMPLE"}))
        answers.append(stub._input_bridge.request_input("Pick an EXAMPLE profile"))

    StubService.hooks["titles"] = titles
    service = make_client(server.url, cdm=SimpleNamespace())
    store = session_store.get_session_store()
    try:
        service.authenticate()
        first = service._session_id
        assert first
        assert isinstance(service.channel, WebSocketChannel) and service.channel.connected.is_set()
        assert "Logged in to EXAMPLE, login 1" in example_messages(caplog)

        listed: list[Any] = []
        listing = Thread(target=lambda: listed.append(service.get_titles()), daemon=True)
        listing.start()
        assert wait_until(lambda: "Listing EXAMPLE titles" in example_messages(caplog), 10)
        assert listing.is_alive(), "the log record must arrive while its call still runs"
        release.set()
        listing.join(10)
        assert [str(t.id) for t in listed[0]] == ["example-1", "example-2", "example-3"]
        assert answers == [{"op": "close"}, CODE]
        assert service.get_tracks(listed[0][0]).audio
        assert count(server, "GET", PROMPT_ROUTE) == count(server, "GET", LOGS_ROUTE) == 0

        forget(server, first)
        assert wait_until(lambda: not service.channel.connected.is_set(), 5)
        assert [str(t.id) for t in service.get_tracks(listed[0][1]).audio] == ["example-2-login2"]
        second = service._session_id
        assert second not in (None, first)
        assert isinstance(service.channel, WebSocketChannel) and service.channel.connected.is_set()
        assert wait_until(lambda: store.peek(second).channel.attached, 5)

        info = count(server, "GET", SESSION_ROUTE, second)
        server.loop.call_soon_threadsafe(store.start_drain)
        assert wait_until(lambda: service.channel.draining_now, 5)
        assert count(server, "GET", SESSION_ROUTE, second) == info
        assert wait_until(lambda: session_store.DRAIN_WARNING in example_messages(caplog), 5)
        assert service.server_draining()
        server.loop.call_soon_threadsafe(store.stop_drain)
        assert wait_until(lambda: not store.draining, 5)
        assert not service.server_draining()
        assert count(server, "GET", PROMPT_ROUTE) == count(server, "GET", LOGS_ROUTE) == 0
    finally:
        release.set()
        service.close()


def test_an_open_channel_keeps_the_session_alive(server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    ttl = 0.3
    monkeypatch.setitem(config.serve, "session_ttl", ttl)
    service = make_client(server.url)
    store = session_store.get_session_store()
    try:
        service.authenticate()
        session_id = service._session_id
        entry = store.peek(session_id)
        assert wait_until(lambda: entry.channel.attached, 5)
        assert wait_until(lambda: idle_seconds(entry) > 3 * ttl, 10)
        assert on_loop(server, store.cleanup_expired()) == 0
        assert store.peek(session_id) is entry
        assert count(server, "GET", SESSION_ROUTE, session_id) == 0
        assert count(server, "GET", LOGS_ROUTE) == count(server, "GET", PROMPT_ROUTE) == 0

        service.close_channel()
        assert wait_until(lambda: not entry.channel.attached, 5)
        assert wait_until(lambda: idle_seconds(entry) > 2 * ttl, 10)
        assert on_loop(server, store.cleanup_expired()) == 1
        assert store.peek(session_id) is None
        assert count(server, "GET", SESSION_ROUTE, session_id) == 0
    finally:
        service.close()


def gate_channel(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Make the channel route refuse while the returned event is set, as a gateway that drops WebSockets does."""
    monkeypatch.setattr(remote_channel, "RECONNECT_DELAY", 0.05)
    refuse = threading.Event()
    channel_handler = routes.session_channel_handler

    async def gated(session_id: str, request: web.Request) -> web.StreamResponse:
        if refuse.is_set():
            raise APIError(APIErrorCode.SERVICE_UNAVAILABLE, "The EXAMPLE channel is down")
        return await channel_handler(session_id, request)

    monkeypatch.setattr(routes, "session_channel_handler", gated)
    return refuse


def test_a_dropped_channel_falls_back_to_polling_until_it_reconnects(
    server: SimpleNamespace, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO)
    refuse = gate_channel(monkeypatch)
    StubService.hooks["titles"] = lambda stub: stub.log.info("Listing EXAMPLE titles")
    service = make_client(server.url)
    store = session_store.get_session_store()
    try:
        service.authenticate()
        session_id = service._session_id
        entry = store.peek(session_id)
        channel = service.channel
        assert isinstance(channel, WebSocketChannel)
        assert wait_until(lambda: entry.channel.attached, 5)

        refuse.set()
        on_loop(server, entry.channel.ws.close())
        assert wait_until(lambda: not channel.connected.is_set(), 5)
        titles = service.get_titles()
        assert count(server, "GET", LOGS_ROUTE, session_id) >= 1
        assert "Listing EXAMPLE titles" in example_messages(caplog)
        assert wait_until(lambda: count(server, "GET", SESSION_ROUTE, session_id) >= 1, 5), "no keepalive took over"

        refuse.clear()
        assert wait_until(lambda: channel.connected.is_set() and entry.channel.attached, 10)
        assert wait_until(lambda: channel.fallback.keepalive_stop.is_set(), 5)
        logs = count(server, "GET", LOGS_ROUTE, session_id)
        assert service.get_tracks(titles[0]).audio
        assert count(server, "GET", LOGS_ROUTE, session_id) == logs
        assert example_messages(caplog).count("Listing EXAMPLE titles") == 1
    finally:
        service.close()


def test_a_cdm_call_made_while_the_channel_is_down_goes_through_polling(
    server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    refuse = gate_channel(monkeypatch)
    monkeypatch.setattr(client_relay, "answer_cdm_call", lambda cdm, call, sessions: {"op": call["op"]})
    service = make_client(server.url, cdm=SimpleNamespace())
    store = session_store.get_session_store()
    answers: list[Any] = []

    def titles(stub: StubService) -> None:
        refuse.set()
        entry = store.peek(service._session_id)
        asyncio.run_coroutine_threadsafe(entry.channel.ws.close(), server.loop).result(5)
        assert wait_until(lambda: not service.channel.connected.is_set() and not entry.channel.attached, 5)
        answers.append(stub._input_bridge.request_cdm({"op": "close", "session": "EXAMPLE"}))

    StubService.hooks["titles"] = titles
    try:
        service.authenticate()
        assert isinstance(service.channel, WebSocketChannel)
        service.get_titles()
        assert answers == [{"op": "close"}]
        assert count(server, "GET", PROMPT_ROUTE, service._session_id) >= 1
    finally:
        service.close()


def test_a_prompt_the_client_cannot_answer_fails_the_login_at_once(server: SimpleNamespace) -> None:
    def refuse(prompt: str) -> str:
        raise RuntimeError("the EXAMPLE caller left")

    service = make_client(server.url)
    service._input_bridge = SimpleNamespace(request_input=refuse)
    failed: list[BaseException] = []

    def login() -> None:
        try:
            service.authenticate()
        except BaseException as e:
            failed.append(e)

    try:
        thread = Thread(target=login, daemon=True)
        thread.start()
        thread.join(10)
        assert not thread.is_alive(), "the login waited for an answer that cannot come"
        assert isinstance(failed[0], RemoteError)
        assert "the EXAMPLE caller left" in failed[0].message
    finally:
        service.close()
