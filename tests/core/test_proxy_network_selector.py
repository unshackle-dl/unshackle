"""Unit tests for the "@" proxy network selector and the SurfsharkVPN pinned server port."""

from __future__ import annotations

import datetime
import re
import socket
import ssl
import threading
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from unshackle.core.proxies import asn, surfsharkvpn
from unshackle.core.proxies.nordvpn import NordVPN
from unshackle.core.proxies.resolve import REGION
from unshackle.core.proxies.surfsharkvpn import SurfsharkVPN
from unshackle.core.proxies.windscribevpn import WindscribeVPN
from unshackle.core.remote_service import resolve_remote_proxy_arg

pytestmark = pytest.mark.unit

DATACAMP = "CDNEXT - Datacamp Limited, GB"
HOSTROYALE = "HOSTROYALE - HostRoyale Technologies Pvt Ltd, IN"
NETWORKS = {"10.0.0.1": DATACAMP, "10.0.0.2": HOSTROYALE, "10.0.0.3": HOSTROYALE}


@pytest.fixture(autouse=True)
def known_networks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asn, "NETWORKS", dict(NETWORKS))
    monkeypatch.setattr(asn, "FAILED", None)


@pytest.mark.parametrize(
    ("value", "is_query"),
    [
        ("nordvpn:ca@datacamp", True),
        ("nordvpn:ca:toronto@-datacamp", True),
        ("windscribevpn:fr@host-royale", True),
        ("fr@data_camp", True),
        ("surfsharkvpn:fr-bod@195.86.24.103", True),
        ("nordvpn:us", True),
        ("fr@", False),
        ("fr@-", False),
        ("user:pass@host:8080", False),
        ("http://user:pass@host:8080", False),
    ],
)
def test_region_grammar_takes_the_selector_but_not_a_uri(value: str, is_query: bool) -> None:
    """dl and search send a value to the proxy providers only when it matches; anything else is an explicit URI."""
    assert bool(re.fullmatch(rf"(?:[a-z]+:)?{REGION}", value, re.IGNORECASE)) is is_query


def test_remote_sends_a_surfshark_selector_unresolved() -> None:
    assert resolve_remote_proxy_arg("surfsharkvpn:fr-bod@datacamp", []) == "surfsharkvpn:fr-bod@datacamp"


@pytest.mark.parametrize(
    ("query", "expected"),
    [("fr", ("fr", None)), ("fr:paris@-datacamp", ("fr:paris", "-datacamp")), ("fr@", ("fr", None))],
)
def test_split_selector(query: str, expected: tuple) -> None:
    assert asn.split_selector(query) == expected


@pytest.mark.parametrize(
    ("selector", "expected"),
    [("datacamp", ["a"]), ("Data-Camp", ["a"]), ("host royale", ["b", "c"]), ("-datacamp", ["b", "c"])],
)
def test_select_includes_or_excludes_a_network(selector: str, expected: list[str]) -> None:
    servers = {"a": "10.0.0.1", "b": "10.0.0.2", "c": "10.0.0.3"}
    assert sorted(asn.select(servers, selector)) == expected


@pytest.mark.parametrize(("selector", "expected"), [("10.9.9.9", ["x"]), ("-10.9.9.9", ["a", "b"])])
def test_select_by_ip_needs_no_network_lookup(
    selector: str, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asn, "lookup", lambda ips: pytest.fail("an IP selector looked up networks"))
    assert sorted(asn.select({"a": "10.0.0.1", "b": "10.0.0.2", "x": "10.9.9.9"}, selector)) == expected


def test_select_drops_a_server_whose_network_is_unknown_even_when_excluding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asn, "lookup", lambda ips: {k: v for k, v in NETWORKS.items() if k in set(ips)})
    assert asn.select({"a": "10.0.0.1", "x": "10.9.9.9"}, "-hostroyale") == ["a"]


def test_select_needs_a_name() -> None:
    with pytest.raises(ValueError, match="needs a network name"):
        asn.select({"a": "10.0.0.1"}, "-")


def test_lookup_parses_cymru_bulk_whois(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = (
        b"Bulk mode; whois.cymru.com [2026-10-22 18:57:29 +0000]\n"
        b"212238  | 195.86.24.103    | CDNEXT - Datacamp Limited, GB\n"
        b"NA      | 10.1.1.1         | NA\n"
    )

    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.recv.side_effect = [answer, b""]
    monkeypatch.setattr(asn.socket, "create_connection", lambda *args, **kwargs: conn)
    assert asn.lookup(["195.86.24.103", "10.1.1.1", "10.0.0.1"]) == {
        "195.86.24.103": DATACAMP,
        "10.0.0.1": DATACAMP,
    }
    conn.sendall.assert_called_once_with(b"begin\n195.86.24.103\n10.1.1.1\nend\n")


def test_lookup_failure_raises_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("refused")

    monkeypatch.setattr(asn.socket, "create_connection", refuse)
    with pytest.raises(ConnectionError, match="whois"):
        asn.lookup(["10.9.9.9"])

    monkeypatch.setattr(asn.socket, "create_connection", lambda *args, **kwargs: pytest.fail("whois asked twice"))
    with pytest.raises(ConnectionError):
        asn.lookup(["10.9.9.8"])
    assert asn.lookup(["10.0.0.1"]) == {"10.0.0.1": DATACAMP}


def test_describe_names_the_network() -> None:
    assert asn.describe("fr-028.example", "10.0.0.1") == "fr-028.example (10.0.0.1, Datacamp Limited)"


def test_describe_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def unresolvable(host: str) -> str:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(asn.socket, "gethostbyname", unresolvable)
    assert asn.describe("gone.example") == "gone.example"


@pytest.fixture
def nordvpn(monkeypatch: pytest.MonkeyPatch) -> NordVPN:
    servers = [
        {"hostname": "ca1.nordvpn.com", "station": "10.0.0.1"},
        {"hostname": "ca2.nordvpn.com", "station": "10.0.0.2"},
    ]
    countries = [{"id": 38, "code": "CA", "name": "Canada", "serverCount": 2, "cities": []}]
    monkeypatch.setattr(NordVPN, "get_countries", staticmethod(lambda: countries))
    monkeypatch.setattr(NordVPN, "get_recommended_servers", staticmethod(lambda c, city=None, limit=None: servers))
    unrecommended = [{"hostname": "ca3.nordvpn.com", "station": "10.0.0.9"}]
    monkeypatch.setattr(NordVPN, "get_all_servers", staticmethod(lambda c: servers + unrecommended))
    return NordVPN("a" * 24, "b" * 24, server_map={"ca": 9})


@pytest.mark.parametrize(
    ("query", "host"),
    [("ca@datacamp", "ca1"), ("ca@-datacamp", "ca2"), ("ca", "ca9"), ("ca@10.0.0.9", "ca3"), ("ca@-10.0.0.1", "ca2")],
)
def test_nordvpn_network_selector_skips_the_server_map(nordvpn: NordVPN, query: str, host: str) -> None:
    uri = nordvpn.get_proxy(query)
    assert uri is not None and uri.endswith(f"@{host}.proxy.nordvpn.com:89")
    assert nordvpn.last_hostname == f"{host}.proxy.nordvpn.com"


def test_nordvpn_selector_with_no_match_raises(nordvpn: NordVPN) -> None:
    with pytest.raises(ValueError, match="No NordVPN server in ca"):
        nordvpn.get_proxy("ca@m247")


def test_nordvpn_specific_server_takes_no_selector(nordvpn: NordVPN) -> None:
    with pytest.raises(ValueError, match="already one NordVPN server"):
        nordvpn.get_proxy("ca1@datacamp")


@pytest.fixture
def windscribe(monkeypatch: pytest.MonkeyPatch) -> WindscribeVPN:
    countries = [
        {
            "country_code": "FR",
            "groups": [
                {"city": "Paris", "hosts": [{"hostname": "fr-001.example"}, {"hostname": "fr-002.example"}]},
                {"city": "Bordeaux", "hosts": [{"hostname": "fr-003.example"}]},
            ],
        }
    ]
    ips = {"fr-001.example": "10.0.0.2", "fr-002.example": "10.0.0.3", "fr-003.example": "10.0.0.1"}
    monkeypatch.setattr(WindscribeVPN, "get_countries", staticmethod(lambda: countries))
    monkeypatch.setattr("unshackle.core.proxies.windscribevpn.resolve_all", lambda hosts: {x: ips[x] for x in hosts})
    return WindscribeVPN("user", "pass")


@pytest.mark.parametrize(
    ("query", "hosts"),
    [
        ("fr@datacamp", {"fr-003.example"}),
        ("fr@-datacamp", {"fr-001.example", "fr-002.example"}),
        ("fr:paris@hostroyale", {"fr-001.example", "fr-002.example"}),
        ("fr@10.0.0.3", {"fr-002.example"}),
        ("fr@-10.0.0.3", {"fr-001.example", "fr-003.example"}),
    ],
)
def test_windscribe_network_selector(windscribe: WindscribeVPN, query: str, hosts: set[str]) -> None:
    uri = windscribe.get_proxy(query)
    assert uri is not None and uri.rsplit("@", 1)[1].removesuffix(":443") in hosts


def test_windscribe_selector_with_no_match_raises(windscribe: WindscribeVPN) -> None:
    with pytest.raises(ValueError, match="No WindscribeVPN server in fr"):
        windscribe.get_proxy("fr:paris@datacamp")


@pytest.fixture
def surfshark(monkeypatch: pytest.MonkeyPatch) -> SurfsharkVPN:
    clusters = [{"countryCode": "FR", "connectionName": "fr-bod.prod.surfshark.com", "location": "Bordeaux"}]
    monkeypatch.setattr(SurfsharkVPN, "get_countries", staticmethod(lambda: clusters))
    monkeypatch.setattr(SurfsharkVPN, "get_pool", staticmethod(lambda hostname: list(NETWORKS)))
    monkeypatch.setattr(surfsharkvpn, "pinned", lambda ip, hostname: 4000 + int(ip.rsplit(".", 1)[1]))
    return SurfsharkVPN("a" * 24, "b" * 24)


@pytest.mark.parametrize(
    ("query", "ports"),
    [
        ("fr-bod@datacamp", {4001}),
        ("fr@-datacamp", {4002, 4003}),
        ("fr-bod@10.0.0.3", {4003}),
        ("fr-bod@-10.0.0.3", {4001, 4002}),
    ],
)
def test_surfshark_selector_pins_a_server_ip(surfshark: SurfsharkVPN, query: str, ports: set[int]) -> None:
    uri = surfshark.get_proxy(query)
    assert uri is not None and uri.startswith(f"http://{'a' * 24}:{'b' * 24}@127.0.0.1:")
    assert int(uri.rsplit(":", 1)[1]) in ports
    assert surfshark.last_server is not None and surfshark.last_server[0] == "fr-bod.prod.surfshark.com"


def test_surfshark_without_selector_is_unchanged(surfshark: SurfsharkVPN) -> None:
    assert surfshark.get_proxy("fr") == f"https://{'a' * 24}:{'b' * 24}@fr-bod.prod.surfshark.com:443"
    assert surfshark.last_connection_display() is None


@pytest.fixture
def tls_echo(tmp_path: Path) -> Iterator[tuple[int, ssl.SSLContext]]:
    """A TLS echo server for *.prod.surfshark.com on loopback, and a client context that trusts it."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "*.prod.surfshark.com")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("*.prod.surfshark.com")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    (tmp_path / "cert.pem").write_bytes(pem)
    (tmp_path / "key.pem").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(tmp_path / "cert.pem", tmp_path / "key.pem")

    listener = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                with server_tls.wrap_socket(conn, server_side=True) as tls:
                    while data := tls.recv(65536):
                        tls.sendall(data)
            except (OSError, ssl.SSLError):
                pass

    threading.Thread(target=serve, daemon=True).start()
    yield listener.getsockname()[1], ssl.create_default_context(cadata=pem.decode())
    listener.close()


def test_pinned_port_relays_over_verified_tls(
    tls_echo: tuple[int, ssl.SSLContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, client_tls = tls_echo
    monkeypatch.setattr(surfsharkvpn, "PORT", port)
    monkeypatch.setattr(surfsharkvpn, "TLS", client_tls)
    monkeypatch.setattr(surfsharkvpn, "PINS", {})

    with socket.create_connection(("127.0.0.1", surfsharkvpn.pinned("127.0.0.1", "fr-bod.prod.surfshark.com"))) as c:
        c.settimeout(5)
        c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        assert c.recv(65536) == b"CONNECT example.com:443 HTTP/1.1\r\n\r\n"

    # a hostname the certificate does not name fails the TLS check, and the client sees the connection close
    with socket.create_connection(("127.0.0.1", surfsharkvpn.pinned("127.0.0.1", "evil.example.com"))) as c:
        c.settimeout(5)
        c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        assert c.recv(65536) == b""
