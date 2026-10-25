"""The reverse tunnel: a server-side SOCKS5 proxy whose connections the client dials and relays."""

from __future__ import annotations

import asyncio
import http.server
import socket
import socketserver
import threading
import time
from typing import Callable, Iterator

import aiohttp
import click
import pytest
import requests
import responses
from aiohttp import web

from unshackle.core import tunnel
from unshackle.core.api.routes import tunnel as tunnel_route
from unshackle.core.api.routes import tunnel_check
from unshackle.core.tunnel import (
    CHECK_ROUTE,
    OPEN,
    ROUTE,
    TUNNELS,
    TunnelClient,
    connect_public,
    frame,
    is_fake_ip,
    is_public_ip,
    tunnel_proxy,
)


class Hello(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self) -> None:
        Hello.hits += 1
        body = b"hello through the tunnel"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture
def target() -> Iterator[int]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Hello)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def server() -> Iterator[str]:
    loop = asyncio.new_event_loop()
    app = web.Application()
    app.router.add_get(ROUTE, tunnel_route)
    app.router.add_get(CHECK_ROUTE, tunnel_check)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    threading.Thread(target=loop.run_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    asyncio.run_coroutine_threadsafe(tunnel.close_tunnels(app), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)


def test_requests_through_the_tunnel_reach_the_client_side_target(
    server: str, target: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tunnel, "is_public_ip", lambda address: True)  # the test target is on loopback
    client = TunnelClient(server, {"X-Secret-Key": "alice"})
    tunnel_id = client.start()

    proxy = tunnel_proxy(tunnel_id, "alice")
    assert proxy and proxy.startswith(f"socks5h://{tunnel_id}:") and "@127.0.0.1:" in proxy
    for _ in range(3):
        resp = requests.get(f"http://127.0.0.1:{target}/", proxies={"all": proxy}, timeout=10)
        assert resp.text == "hello through the tunnel"
    client.stop()


def test_a_tunnel_is_only_usable_by_its_owner(server: str) -> None:
    client = TunnelClient(server, {"X-Secret-Key": "alice"})
    tunnel_id = client.start()
    assert tunnel_proxy(tunnel_id, "mallory") is None
    assert tunnel_proxy("unknown", "alice") is None
    client.stop()


def test_the_client_refuses_a_target_on_its_own_network(server: str, target: int) -> None:
    client = TunnelClient(server, {"X-Secret-Key": "alice"})
    proxy = tunnel_proxy(client.start(), "alice")
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get(f"http://127.0.0.1:{target}/", proxies={"all": proxy}, timeout=10)
    client.stop()


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("127.0.0.1", False),
        ("192.168.1.10", False),
        ("10.0.0.1", False),
        ("::1", False),
        ("198.18.2.36", False),
    ],
)
def test_is_public_ip(address: str, public: bool) -> None:
    assert is_public_ip(address) is public


@pytest.mark.parametrize(
    ("address", "fake"),
    [
        ("198.18.2.36", True),
        ("198.19.255.1", True),
        ("198.20.0.1", False),
        ("::ffff:198.18.0.1", False),
        ("2001:2::1", True),
        ("2001:2:1::1", False),
        ("fdfe:dcba:9876::1", False),
    ],
)
def test_is_fake_ip(address: str, fake: bool) -> None:
    assert is_fake_ip(address) is fake


def test_a_name_that_resolves_to_a_fake_ip_is_dialled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(self: object, host: str, port: int, **_: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.2.36", port))]

    dialled: list[str] = []

    async def open_connection(address: str, port: int) -> tuple:
        dialled.append(address)
        return None, None

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    asyncio.run(connect_public("api.example", 443))
    assert dialled == ["198.18.2.36"]


def test_localhost_is_refused() -> None:
    with pytest.raises(PermissionError):
        asyncio.run(connect_public("localhost", 443))


@pytest.mark.parametrize(("host", "address"), [("rebind.example", "192.168.1.1"), ("198.18.0.1", "198.18.0.1")])
def test_a_target_that_resolves_to_a_local_address_is_refused(
    monkeypatch: pytest.MonkeyPatch, host: str, address: str
) -> None:
    async def resolve(self: object, host: str, port: int, **_: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    with pytest.raises(PermissionError):
        asyncio.run(connect_public(host, 443))


def local_proxy_ctx(params: dict, typed: set) -> click.Context:
    from click.core import ParameterSource

    ctx = click.Context(click.Command("dl"))
    ctx.params = {"local_proxy": True, **params}
    for key in params:
        ctx.set_parameter_source(key, ParameterSource.COMMANDLINE if key in typed else ParameterSource.DEFAULT_MAP)
    return ctx


def test_local_proxy_works_with_no_other_proxy_option() -> None:
    from unshackle.core.proxies.resolve import require_alone_local_proxy

    with pytest.raises(click.UsageError, match="--proxy, --proxy-download"):
        require_alone_local_proxy(
            local_proxy_ctx({"remote": True, "proxy": "us", "proxy_download": "us"}, {"proxy", "proxy_download"})
        )
    with pytest.raises(click.UsageError, match="needs --remote"):
        require_alone_local_proxy(local_proxy_ctx({"remote": False}, set()))


def test_local_proxy_turns_off_a_proxy_from_the_config_defaults() -> None:
    from unshackle.core.proxies.resolve import require_alone_local_proxy

    ctx = local_proxy_ctx({"remote": True, "proxy": "us", "no_proxy_download": True}, set())
    require_alone_local_proxy(ctx)
    assert ctx.params["proxy"] is None and ctx.params["no_proxy_download"] is False


def test_the_tunnel_port_refuses_a_proxy_uri_without_the_tunnel_credentials(
    server: str, target: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tunnel, "is_public_ip", lambda address: True)
    client = TunnelClient(server, {"X-Secret-Key": "alice"})
    tunnel_id = client.start()
    port = TUNNELS[tunnel_id].port
    for uri in (f"socks5h://127.0.0.1:{port}", f"socks5h://{tunnel_id}:wrong@127.0.0.1:{port}"):
        with pytest.raises(requests.exceptions.ConnectionError):
            requests.get(f"http://127.0.0.1:{target}/", proxies={"all": uri}, timeout=10)
    client.stop()


def test_the_server_never_dials_for_a_client(server: str, target: int) -> None:
    """An OPEN from the client must not make the server connect anywhere: only the client dials."""

    async def send_open() -> None:
        async with aiohttp.ClientSession(headers={"X-Secret-Key": "mallory"}) as http:
            async with http.ws_connect(server + ROUTE) as ws:
                await ws.receive_json()
                await ws.send_bytes(frame(1, OPEN, target.to_bytes(2, "big") + b"127.0.0.1"))

    Hello.hits = 0
    asyncio.run(send_open())
    assert Hello.hits == 0


def test_the_check_route_is_only_for_the_tunnel_owner(server: str) -> None:
    client = TunnelClient(server, {"X-Secret-Key": "alice"})
    tunnel_id = client.start()
    resp = requests.get(f"{server}{CHECK_ROUTE}?id={tunnel_id}", headers={"X-Secret-Key": "mallory"}, timeout=10)
    assert resp.status_code == 404
    client.stop()


@pytest.mark.parametrize(
    ("uri", "line"),
    [
        ("socks5://127.0.0.1:1080", "Using a local proxy on port 1080"),
        ("http://localhost:8888", "Using a local proxy on port 8888"),
        ("http://1.2.3.4:8080", "Using explicit Proxy: http://1.2.3.4:8080"),
        ("http://localhost", "Using a local proxy"),
        ("http://127.0.0.1:99999", "Using a local proxy"),
    ],
)
def test_a_local_proxy_logs_only_its_port(uri: str, line: str) -> None:
    from unshackle.core.proxies.resolve import describe_explicit_proxy

    assert describe_explicit_proxy(uri) == line


def test_another_api_key_cannot_reattach_to_a_tunnel(server: str) -> None:
    client = TunnelClient(server, {"X-Secret-Key": "alice"})
    tunnel_id = client.start()

    async def reattach() -> None:
        async with aiohttp.ClientSession(headers={"X-Secret-Key": "mallory"}) as http:
            async with http.ws_connect(f"{server}{ROUTE}?id={tunnel_id}"):
                pass

    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        asyncio.run(reattach())
    assert e.value.status == 404
    client.stop()


def test_a_request_that_is_not_a_websocket_opens_no_tunnel(server: str) -> None:
    before = len(TUNNELS)
    resp = requests.get(server + ROUTE, headers={"X-Secret-Key": "alice"}, timeout=10)
    assert resp.status_code == 400
    assert len(TUNNELS) == before


def test_a_malformed_frame_still_leaves_the_tunnel_to_expire(server: str) -> None:
    async def send_short_frame() -> str:
        async with aiohttp.ClientSession(headers={"X-Secret-Key": "mallory"}) as http:
            async with http.ws_connect(server + ROUTE) as ws:
                tunnel_id = (await ws.receive_json())["id"]
                await ws.send_bytes(b"\x01")
        return str(tunnel_id)

    tunnel_id = asyncio.run(send_short_frame())
    deadline = time.monotonic() + 5
    while TUNNELS[tunnel_id].expiry is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert TUNNELS[tunnel_id].expiry is not None


def test_an_api_key_has_a_tunnel_limit(server: str) -> None:
    clients = [TunnelClient(server, {"X-Secret-Key": "bob"}) for _ in range(tunnel.MAX_TUNNELS)]
    for client in clients:
        client.start()
    with pytest.raises(ConnectionError, match="429"):
        TunnelClient(server, {"X-Secret-Key": "bob"}).start()
    for client in clients:
        client.stop()


def test_a_connection_that_stops_reading_is_dropped() -> None:
    sent: list = []

    class FakeWS:
        closed = False

        async def send_bytes(self, data: bytes) -> None:
            sent.append(data)

    async def flood() -> tunnel.Link:
        link = tunnel.Link(FakeWS())
        link.streams[7] = tunnel.new_queue()
        for _ in range(tunnel.BACKLOG + 5):
            await link.feed(7, b"x")
        return link

    link = asyncio.run(flood())
    assert 7 not in link.streams
    assert sent == [frame(7, tunnel.CLOSE)]


def test_a_download_job_refuses_a_tunnel_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.api import handlers
    from unshackle.core.api.errors import APIError

    monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request, service: True)
    monkeypatch.setattr(handlers, "server_cdm_max_height", lambda request, service: None)
    with pytest.raises(APIError, match="tunnel"):
        handlers.enforce_download_gates({"service": "EXAMPLE", "proxy": "tunnel:abc"}, None)


@responses.activate
def test_a_server_error_after_the_tunnel_closed_names_the_tunnel() -> None:
    from types import SimpleNamespace

    from unshackle.core.remote_service import RemoteClient, RemoteError

    responses.add(
        responses.GET,
        "http://srv:8786/api/session/x/titles",
        json={"status": "error", "error_code": "SERVICE_ERROR", "message": "proxy refused"},
        status=422,
    )
    client = RemoteClient("http://srv:8786", "")
    client.tunnel = SimpleNamespace(lost=True)  # type: ignore[assignment]
    with pytest.raises(RemoteError) as e:
        client.get("/api/session/x/titles")
    assert e.value.kind == "TUNNEL_CLOSED"


def spy_links(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record every Link the client makes, so a test can look at its open streams."""
    links: list = []

    class SpyLink(tunnel.Link):
        def __init__(self, ws: object) -> None:
            super().__init__(ws)
            links.append(self)

    monkeypatch.setattr(tunnel, "Link", SpyLink)
    return links


def wait_until(check: "Callable[[], bool]", seconds: float = 3) -> bool:
    deadline = time.monotonic() + seconds
    while not check() and time.monotonic() < deadline:
        time.sleep(0.05)
    return check()


def test_a_dial_the_server_gave_up_on_is_dropped_by_the_client(
    server: str, target: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the server times out an OPEN, the client must not keep the connection it makes later."""
    monkeypatch.setattr(tunnel, "is_public_ip", lambda address: True)
    monkeypatch.setattr(tunnel, "OPEN_TIMEOUT", 0.3)
    real_connect = tunnel.connect_public

    async def slow_connect(host: str, port: int) -> tuple:
        await asyncio.sleep(1)
        return await real_connect(host, port)

    monkeypatch.setattr(tunnel, "connect_public", slow_connect)
    links = spy_links(monkeypatch)
    client = TunnelClient(server, {"X-Secret-Key": "alice"})
    proxy = tunnel_proxy(client.start(), "alice")
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get(f"http://127.0.0.1:{target}/", proxies={"all": proxy}, timeout=10)
    assert wait_until(lambda: not links[-1].streams and not client.relays)
    client.stop()


def socks5_connect(proxy: str, host: bytes) -> int:
    """Send one SOCKS5 CONNECT for ``host`` with the credentials in ``proxy`` and return the reply code."""
    from urllib.parse import urlsplit

    parts = urlsplit(proxy)
    user, password = (parts.username or "").encode(), (parts.password or "").encode()
    with socket.create_connection(("127.0.0.1", parts.port or 0), timeout=10) as sock:
        sock.sendall(b"\x05\x01\x02")
        assert sock.recv(2) == b"\x05\x02"
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
        assert sock.recv(2) == b"\x01\x00"
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (443).to_bytes(2, "big"))
        return sock.recv(10)[1]


def test_a_hostname_the_resolver_rejects_closes_its_stream(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(self: object, host: str, port: int, **_: object) -> list:
        host.encode("idna")  # as socket.getaddrinfo does, so a long label raises UnicodeError
        return []

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    monkeypatch.setattr(tunnel, "OPEN_TIMEOUT", 1)
    links = spy_links(monkeypatch)
    client = TunnelClient(server, {"X-Secret-Key": "alice"})
    proxy = tunnel_proxy(client.start(), "alice")
    assert proxy
    assert socks5_connect(proxy, b"a" * 64 + b".example") == 5
    assert wait_until(lambda: not links[-1].streams)
    client.stop()


def test_a_failed_start_leaves_no_thread_behind() -> None:
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    try:
        client = TunnelClient(f"http://127.0.0.1:{silent.getsockname()[1]}", {"X-Secret-Key": "alice"})
        with pytest.raises(ConnectionError, match="timed out"):
            client.start(timeout=0.5)
        assert not client.thread.is_alive()
    finally:
        silent.close()
