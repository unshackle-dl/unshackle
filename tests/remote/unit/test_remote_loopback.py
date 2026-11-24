"""The real remote client against the real server routes, over HTTP on a loopback port."""

from __future__ import annotations

import asyncio
import logging
from threading import Semaphore, Thread
from types import SimpleNamespace
from typing import Any, Iterator

import click
import pytest
import requests
from aiohttp import web
from langcodes import Language

from tests.remote.unit.test_tunnel import wait_until
from unshackle.core import remote_service
from unshackle.core.api import handlers, routes, session_store
from unshackle.core.api.handlers import api_key_authentication
from unshackle.core.api.routes import cors_middleware, no_5xx_middleware, setup_routes
from unshackle.core.config import config
from unshackle.core.remote_protocol import SESSION_ROUTE, APIErrorCode
from unshackle.core.remote_service import RemoteError, RemoteService
from unshackle.core.titles.movie import Movie, Movies
from unshackle.core.tracks import Audio, Chapters, Tracks
from unshackle.core.tracks.track import Track

pytestmark = pytest.mark.unit

KEY = "loopback-key"
CODE = "1234"


class StubService:
    """The server side of the remote session: a login that asks for a code, three titles, one audio track each."""

    logins = 0

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
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[SimpleNamespace]:
    """The real route table and API key middleware on a loopback port, with the stub as the only service."""
    beats = Semaphore(0)
    seen: list[tuple[str, str, int]] = []

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
        response = await handler(request)
        seen.append((request.method, request.path, response.status))
        return response

    StubService.logins = 0
    monkeypatch.setattr(routes, "HEARTBEAT_INTERVAL", 0.02)
    monkeypatch.setattr(routes.web, "StreamResponse", CountedHeartbeat)
    monkeypatch.setattr(remote_service, "KEEPALIVE_MIN_INTERVAL", 0.05)
    monkeypatch.setattr(remote_service, "REOPEN_WAIT", 5.0)
    poll_auth_completion = RemoteService.poll_auth_completion
    monkeypatch.setattr(
        RemoteService, "poll_auth_completion", lambda self: poll_auth_completion(self, poll_interval=0.01)
    )
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
        yield SimpleNamespace(url=f"http://{host}:{port}", loop=loop, seen=seen)
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        loop.close()


def make_client(url: str) -> RemoteService:
    """A client whose first candidate header is not the one the server reads, so it must move to the second."""
    obj = SimpleNamespace(cdm=None, cdm_entry=False, proxy_providers=[])
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


def test_a_remote_session_runs_end_to_end_over_http(server: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    service = make_client(server.url)
    try:
        service.authenticate()
        first = service._session_id
        assert first
        assert server.seen[0] == ("POST", "/api/session/create", 401)
        assert ("GET", f"/api/session/{first}/prompt", 200) in server.seen
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
    assert service._keepalive_stop.is_set()
    assert ("DELETE", SESSION_ROUTE.format(session_id=second), 200) in server.seen
    assert not session_store.get_session_store()._sessions
