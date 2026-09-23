"""Unit tests for the Control D proxy provider: the DoH answer parser, the loopback forwarder
that turns a resolver into something Python-Requests can use as a proxy, the pool of
region-named profiles, and the resolver form a client sends to a remote server."""

from __future__ import annotations

import base64
import http.server
import re
import socket
import socketserver
import threading
import time
from types import SimpleNamespace
from typing import Iterator

import click
import pytest
import requests

from unshackle.core.proxies import controld
from unshackle.core.proxies.controld import (
    MIN_TTL,
    ControlD,
    IPv4Adapter,
    LocalProxy,
    Resolver,
    build_query,
    local_proxy,
    parse_a_records,
)
from unshackle.core.proxies.resolve import REGION, resolve_proxy

pytestmark = pytest.mark.unit


def dns_response(ip: bytes, ttl: int = 300) -> bytes:
    """A one-question, one-answer A-record response whose answer name is a compression pointer."""
    header = b"\x00\x00\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
    question = b"\x01a\x04test\x00" + b"\x00\x01\x00\x01"
    answer = b"\xc0\x0c" + b"\x00\x01\x00\x01" + ttl.to_bytes(4, "big") + b"\x00\x04" + ip
    return header + question + answer


def test_query_is_a_well_formed_a_record_question() -> None:
    query = build_query("a.test.")
    assert query[4:6] == b"\x00\x01"
    assert query[12:] == b"\x01a\x04test\x00\x00\x01\x00\x01"


def test_parses_the_answer_past_a_compression_pointer() -> None:
    assert parse_a_records(dns_response(bytes([1, 2, 3, 4]))) == [("1.2.3.4", 300)]
    assert parse_a_records(b"") == []


def origin(body: bytes) -> http.server.HTTPServer:
    """A local HTTP origin that answers every GET with `body`, and keeps connections alive."""

    class Origin(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def forwarder() -> Iterator[LocalProxy]:
    """A forwarder whose resolver sends every hostname to 127.0.0.1, as Control D sends it to its proxy."""
    proxy = LocalProxy(SimpleNamespace(resolve=lambda host: "127.0.0.1"))
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    yield proxy
    proxy.shutdown()
    proxy.server_close()


def test_forwards_a_request_to_the_resolved_host(forwarder: LocalProxy) -> None:
    pong = origin(b"pong")
    try:
        response = requests.get(
            f"http://unresolvable.test:{pong.server_address[1]}/", proxies={"http": forwarder.uri}, timeout=10
        )
        assert response.status_code == 200
        assert response.content == b"pong"
    finally:
        pong.shutdown()


def test_a_reused_connection_never_carries_a_request_to_the_wrong_host(forwarder: LocalProxy) -> None:
    """A client pools one proxy connection for every plain-HTTP host, and the relay is bound to the first."""
    first, second = origin(b"A"), origin(b"B")
    try:
        with requests.Session() as session:
            session.proxies = {"http": forwarder.uri}
            assert session.get(f"http://a.test:{first.server_address[1]}/", timeout=10).content == b"A"
            assert session.get(f"http://b.test:{second.server_address[1]}/", timeout=10).content == b"B"
    finally:
        first.shutdown()
        second.shutdown()


def test_connect_tunnels_bytes_both_ways(forwarder: LocalProxy) -> None:
    """Every real download is HTTPS, so it all goes through CONNECT rather than absolute-form."""
    echo = socketserver.TCPServer(
        ("127.0.0.1", 0),
        type(
            "Echo",
            (socketserver.BaseRequestHandler,),
            {"handle": lambda self: self.request.sendall(self.request.recv(64)[::-1])},
        ),
    )
    threading.Thread(target=echo.serve_forever, daemon=True).start()

    try:
        with socket.create_connection(forwarder.server_address[:2], timeout=10) as client:
            client.sendall(f"CONNECT unresolvable.test:{echo.server_address[1]} HTTP/1.1\r\n\r\n".encode())
            assert b"200 Connection established" in client.recv(128)
            client.sendall(b"stressed")
            assert client.recv(64) == b"desserts"
    finally:
        echo.shutdown()
        echo.server_close()


@pytest.mark.parametrize(
    ("target", "status"),
    [
        ("1.2.3.4:443", b"502"),  # an IP literal would bypass Control D
        ("[::1]:443", b"502"),
        ("a.test:x", b"400"),
    ],
)
def test_connect_refuses_what_it_cannot_redirect(forwarder: LocalProxy, target: str, status: bytes) -> None:
    with socket.create_connection(forwarder.server_address[:2], timeout=10) as client:
        client.sendall(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
        assert client.recv(128).startswith(b"HTTP/1.1 " + status)


def test_an_unreachable_host_answers_502() -> None:
    def refuse(host: str) -> str:
        raise ConnectionError("no A record")

    proxy = LocalProxy(SimpleNamespace(resolve=refuse))
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        response = requests.get("http://unresolvable.test/", proxies={"http": proxy.uri}, timeout=10)
        assert response.status_code == 502
    finally:
        proxy.shutdown()
        proxy.server_close()


def stub_session(response: bytes, seen: list) -> object:
    class Session:
        @staticmethod
        def get(url: str, params: dict, headers: dict, timeout: int) -> object:
            seen.append((url, params, headers))

            class Answer:
                content = response

                @staticmethod
                def raise_for_status() -> None:
                    pass

            return Answer()

    return Session()


def test_resolve_asks_control_d_in_wireformat_and_caches_it() -> None:
    seen: list = []
    resolver = Resolver("https://dns.controld.com/efgh456")
    resolver.session = stub_session(dns_response(bytes([9, 8, 7, 6]), ttl=1), seen)

    assert resolver.resolve("a.test") == "9.8.7.6"
    url, params, headers = seen[0]
    assert url == "https://dns.controld.com/efgh456"
    assert headers == {"accept": "application/dns-message"}
    assert params["dns"] == base64.urlsafe_b64encode(build_query("a.test")).rstrip(b"=").decode()
    assert "=" not in params["dns"]  # RFC 8484 wants the padding stripped

    assert resolver.resolve("a.test") == "9.8.7.6"
    assert len(seen) == 1
    assert resolver.cache["a.test"][1] > time.monotonic() + MIN_TTL - 1  # a 1s TTL is floored


def test_resolve_raises_when_control_d_has_no_address() -> None:
    resolver = Resolver("https://dns.controld.com/efgh456")
    resolver.session = stub_session(b"\x00\x00\x81\x83" + b"\x00\x00" * 4, [])

    with pytest.raises(ConnectionError):
        resolver.resolve("nowhere.test")


def test_resolve_asks_over_ipv4_and_ignores_environment_proxies() -> None:
    """Control D answers a redirected name only over IPv4, and authorises the address that asked."""
    resolver = Resolver("https://dns.controld.com/efgh456")
    assert resolver.session.trust_env is False
    assert isinstance(resolver.session.get_adapter("https://dns.controld.com/"), IPv4Adapter)


CATALOGUE = [
    {"PK": "YUL", "country": "CA", "city": "Montreal"},
    {"PK": "YYZ", "country": "CA", "city": "Toronto"},
    {"PK": "RES_YYZ", "country": "CA", "city": "Res Toronto", "hidden": True},
    {"PK": "JFK", "country": "US", "city": "New York"},
]


@pytest.fixture
def catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve Control D's location list without going near the network."""
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"body": {"proxies": CATALOGUE}})
    monkeypatch.setattr("unshackle.core.proxies.controld.requests.get", lambda *a, **kw: response)


class Account:
    """A fake Control D account API: holds endpoints, records every call, and answers them."""

    def __init__(self) -> None:
        self.devices: list[dict] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_device = False

    def device(self, name: str, profile: str) -> None:
        self.devices.append(
            {"PK": f"d-{name}", "name": name, "profile": {"PK": profile}, "resolvers": {"uid": f"r{profile}"}}
        )

    def request(self, method: str, url: str, data: dict, headers: dict, timeout: int) -> SimpleNamespace:
        assert headers == {"authorization": "Bearer secret"}
        path = url.removeprefix("https://api.controld.com")
        self.calls.append((method, path, data))
        body: dict = {}
        if (method, path) == ("GET", "/devices"):
            body = {"devices": [dict(x) for x in self.devices]}
        elif (method, path) == ("POST", "/profiles"):
            body = {"profiles": [{"PK": f"p{data['name'].replace('-', '')}"}]}
        elif (method, path) == ("POST", "/devices"):
            if self.fail_device:
                raise requests.HTTPError("endpoint limit")
            self.device(data["name"], data["profile_id"])
            body = self.devices[-1]
        elif method == "PUT" and path.startswith("/devices/"):
            next(x for x in self.devices if x["PK"] == path.split("/")[2])["name"] = data["name"]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"body": body})

    def moves(self) -> list[tuple[str, str]]:
        """The (profile, location) of every default-rule change, in order."""
        return [(path.split("/")[2], data["via"]) for method, path, data in self.calls if path.endswith("/default")]


@pytest.fixture
def account(catalogue: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[Account]:
    """A fake account, on a fresh process state whose forwarders are shut down when the test ends."""
    made = Account()
    monkeypatch.setattr("unshackle.core.proxies.controld.requests.request", made.request)
    controld.FORWARDERS.clear()
    controld.REGIONS.clear()
    yield made
    for server in controld.FORWARDERS.values():
        server.shutdown()
        server.server_close()
    controld.FORWARDERS.clear()
    controld.REGIONS.clear()


def test_locations_come_from_control_d(account: Account) -> None:
    provider = ControlD(token="secret")
    assert provider.location("ca") in ("YUL", "YYZ")  # never the hidden residential one
    assert provider.location("res_yyz") == "RES_YYZ"  # unless asked for by name
    assert provider.location("jp") is None
    assert repr(provider) == "2 Countries (4 Servers)"


def test_a_query_steers_the_profile_default_rule(account: Account) -> None:
    provider = ControlD(token="secret", profile="abcd123", resolver="efgh456", max_profiles=1)

    assert provider.get_proxy("jp") is None
    assert not account.moves()

    uri = provider.get_proxy("jfk")
    assert uri and uri.startswith("http://127.0.0.1:")
    assert ("PUT", "/profiles/abcd123/default", {"do": "3", "via": "JFK", "status": "1"}) in account.calls

    assert provider.get_proxy("JFK") == uri
    assert len(account.moves()) == 1

    # one profile only: a second country moves it and must clear its cache
    cache = controld.FORWARDERS["https://dns.controld.com/efgh456"].resolver.cache
    cache["cached.test"] = ("1.2.3.4", float("inf"))
    assert provider.get_proxy("yul") == uri
    assert account.moves()[-1] == ("abcd123", "YUL")
    assert cache == {}


def test_each_region_gets_its_own_profile_and_forwarder(account: Account) -> None:
    """--proxy and --proxy-download in two countries must not move each other."""
    provider = ControlD(
        token="secret", profile="one", resolver="r1", profiles=[{"profile": "two", "resolver": "r2"}], max_profiles=2
    )
    us = provider.get_proxy("jfk")
    ca = provider.get_proxy("yul")
    assert us != ca
    assert account.moves() == [("one", "JFK"), ("two", "YUL")]

    # out of profiles: the oldest claim moves
    assert provider.get_proxy("yyz") == us
    assert account.moves()[-1] == ("one", "YYZ")
    assert provider.get_proxy("yul") == ca
    assert len(account.moves()) == 3


def test_a_region_uses_the_endpoint_named_after_it_or_creates_one(account: Account) -> None:
    account.device("unshackle-us", "kept")
    account.device("Phone", "theirs")
    account.device("unshackle-gb", "theirs")  # shares the user's own profile, so never touched
    provider = ControlD(token="secret")

    us = provider.get_proxy("us")
    ca = provider.get_proxy("ca")
    assert us != ca
    assert ("POST", "/profiles", {"name": "unshackle-ca"}) in account.calls
    assert [x[0] for x in account.moves()] == ["kept", "punshackleca"]
    assert "theirs" not in provider.resolvers

    # the REST API makes a provider per request; it must find the endpoint by name
    assert ControlD(token="secret").get_proxy("ca") == ca
    assert len(account.moves()) == 2


def test_a_moved_profile_is_renamed_to_its_new_region(account: Account) -> None:
    account.device("unshackle-us", "kept")
    provider = ControlD(token="secret", max_profiles=1)

    provider.get_proxy("us")
    provider.get_proxy("ca")
    assert ("PUT", "/profiles/kept", {"name": "unshackle-ca"}) in account.calls
    assert account.devices[0]["name"] == "unshackle-ca"
    assert account.moves()[-1][0] == "kept"


def test_a_failed_endpoint_leaves_no_profile_behind(account: Account) -> None:
    account.fail_device = True
    with pytest.raises(requests.HTTPError):
        ControlD(token="secret").get_proxy("us")
    assert ("DELETE", "/profiles/punshackleus", None) in [(m, p, d or None) for m, p, d in account.calls]


def test_a_bad_config_is_refused(catalogue: None) -> None:
    with pytest.raises(ValueError):
        ControlD(token="secret", profile="one")
    with pytest.raises(ValueError):
        ControlD(token="secret", max_profiles=0)
    with pytest.raises(ValueError):
        ControlD(token="secret", profile="one", resolver="bad id?")


def test_a_server_runs_a_forwarder_for_a_resolver_a_client_sends(account: Account) -> None:
    provider = ControlD(token="secret", profile="abcd123", resolver="efgh456")
    sent = provider.get_remote_proxy("jfk")
    assert sent == "controld://efgh456@dns.controld.com"

    uri = resolve_proxy(sent, [])
    assert uri and uri.startswith("http://127.0.0.1:")
    assert local_proxy(sent) == uri  # one forwarder per resolver
    with pytest.raises(ValueError):
        local_proxy("controld://evil.example/x@dns.controld.com")


def test_remote_sends_the_resolver_and_a_bare_region_skips_control_d(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unshackle.core import remote_service

    provider = ControlD(token="secret", profile="abcd123", resolver="efgh456")
    monkeypatch.setattr("unshackle.core.proxies.resolve.initialize_proxy_providers", lambda: [provider])

    assert remote_service.resolve_remote_proxy_arg("controld:us") == "controld://efgh456@dns.controld.com"
    with pytest.raises(click.ClickException):
        remote_service.resolve_remote_proxy_arg("us")  # a loopback forwarder is useless to a server


@pytest.mark.parametrize("query", ["ca", "us:seattle", "us-ny", "uk1", "yul", "jfk", "res_yyz", "nordvpn:ca"])
def test_a_region_query_matches(query: str) -> None:
    assert re.fullmatch(rf"(?:[a-z]+:)?{REGION}", query, re.IGNORECASE)
    assert re.fullmatch(rf"(?:[a-z]+:){{0,2}}{REGION}", f"gluetun:{query}", re.IGNORECASE)


@pytest.mark.parametrize("uri", ["nas:3128", "localhost:8080", "proxy.example.com:8080", "1.2.3.4:8080", "us:1080"])
def test_a_host_and_port_is_never_a_region_query(uri: str) -> None:
    assert not re.fullmatch(rf"(?:[a-z]+:){{0,2}}{REGION}", uri, re.IGNORECASE)
