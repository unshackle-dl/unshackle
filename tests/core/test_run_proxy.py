"""This file stands in for service code: its directory is the watched service directory."""

import logging
import os
import sys

import pytest
import requests

from unshackle.core.session import RnetSession
from unshackle.core.utils import run_proxy

PROXY = "https://u:p@proxy.example:89"


class Svc:
    def __init__(self, proxy=None):
        self.session = requests.Session()
        if proxy:
            self.session.proxies["all"] = proxy
        self.log = logging.getLogger("Svc")

    def lookup(self, host: str) -> None:
        lookup(host)

    def fallback(self) -> requests.Session:
        return run_proxy.fallback_session()


@pytest.fixture(autouse=True)
def watching(monkeypatch):
    monkeypatch.setattr(run_proxy, "watched", {})
    monkeypatch.setattr(run_proxy, "proxy_hosts", set())
    monkeypatch.setattr(run_proxy, "warned", set())
    run_proxy.watch(Svc, PROXY)


def lookup(host: str) -> None:
    sys.audit("socket.getaddrinfo", host, 443, 0, 0, 0)


def run_as(file: str, source: str, out: dict | None = None, **names) -> None:
    """Run ``source`` in a frame whose file name is ``file``; ``out`` receives the names it defines."""
    namespace = {"sys": sys, **names}
    exec(compile(source, file, "exec"), namespace)  # nosec B102 - sets the frame file name
    if out is not None:
        out.update(namespace)


def leaks(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "did not go through the proxy" in r.getMessage()]


def test_direct_lookup_from_service_code_warns_once(caplog):
    with caplog.at_level(logging.WARNING):
        lookup("api.example")
        lookup("api.example")
    (message,) = leaks(caplog)
    assert "A request to api.example did not go through the proxy" in message
    assert f"core{os.sep}test_run_proxy.py:" in message


@pytest.mark.parametrize(
    "host",
    [
        "proxy.example",
        "localhost",
        "localhost.",
        "localhost.localdomain",
        "127.0.0.1",
        "0.0.0.0",  # nosec B104 - a host name to classify, not a bind address
        "::ffff:127.0.0.1",
        "10.0.0.5",
        "172.17.0.2",
        "192.168.1.50",
        "fe80::1",
        "nas.local",
        "node.lan",
    ],
)
def test_proxy_and_local_hosts_do_not_warn(caplog, host):
    with caplog.at_level(logging.WARNING):
        lookup(host)
    assert leaks(caplog) == []


@pytest.mark.parametrize("host", ["8.8.8.8", "2606:4700:4700::1111", "api.example"])
def test_public_hosts_warn(caplog, host):
    with caplog.at_level(logging.WARNING):
        lookup(host)
    assert len(leaks(caplog)) == 1


@pytest.mark.parametrize(("address", "warns"), [(("ws.example", 443), True), (("203.0.113.9", 443), False)])
def test_raw_socket_connect_to_a_host_name_warns(caplog, address, warns):
    """A connect to a host name looks it up in C, with no getaddrinfo event; an IP address had its lookup."""
    with caplog.at_level(logging.WARNING):
        sys.audit("socket.connect", object(), address)
    assert bool(leaks(caplog)) is warns


def test_core_frame_ends_the_walk(caplog):
    with caplog.at_level(logging.WARNING):
        source = "sys.audit('socket.getaddrinfo', 'vault.example', 443, 0, 0, 0)"
        run_as(os.path.join(run_proxy.CORE_DIR, "core", "vaults.py"), source)
    assert leaks(caplog) == []


def test_socks_lookup_does_not_warn(caplog):
    """A socks5:// proxy looks up the target host locally, but the request still goes through the proxy."""
    with caplog.at_level(logging.WARNING):
        run_as("/site-packages/socks.py", "sys.audit('socket.getaddrinfo', 'api.example', 443, 0, 0, 0)")
    assert leaks(caplog) == []


@pytest.mark.parametrize("exempt", [False, True])
def test_an_exempt_http_session_does_not_warn(caplog, exempt):
    """The requests frame between the lookup and the service code holds the HTTP session that sends it."""
    session = requests.Session()
    if exempt:
        session.proxy_exempt = True  # type: ignore[attr-defined]
    found: dict = {}
    source = "def send(self, host):\n    sys.audit('socket.getaddrinfo', host, 443, 0, 0, 0)\n"
    run_as("/site-packages/requests/sessions.py", source, out=found)
    with caplog.at_level(logging.WARNING):
        found["send"](session, "api.example")
    assert bool(leaks(caplog)) is not exempt


def test_service_instance_uses_the_proxy_of_its_own_run(caplog):
    with caplog.at_level(logging.WARNING):
        Svc().lookup("direct.example")  # a run with no proxy
        first, second = Svc(PROXY), Svc(PROXY)
        first.lookup("leak.example")
        first.lookup("leak.example")
        second.lookup("leak.example")  # a second run warns again
    assert leaks(caplog) == [leaks(caplog)[0]] * 2
    assert "leak.example" in leaks(caplog)[0]
    assert {r.name for r in caplog.records if r.getMessage() in leaks(caplog)} == {"Svc"}


def test_fallback_session_takes_the_proxy_of_the_calling_run():
    session = Svc("http://own.example:1").fallback()
    assert session.proxies == {"all": "http://own.example:1"}
    assert session.trust_env is False
    assert run_proxy.fallback_session().proxies == {"all": PROXY}  # this module, with one watched run


def test_fallback_session_without_a_service_frame(monkeypatch):
    def from_core():
        source = "result.append(run_proxy.fallback_session())"
        found: list = []
        run_as(os.path.join(run_proxy.CORE_DIR, "core", "x.py"), source, run_proxy=run_proxy, result=found)
        return found[0]

    monkeypatch.setattr(Svc, "__module__", "elsewhere")  # no frame in this file is service code now
    run_proxy.watched[next(iter(run_proxy.watched))].directory = "/nowhere/"
    assert from_core().proxies == {"all": PROXY}  # one watched run, as in a CLI download thread
    run_proxy.watch(Svc, "http://second.example:1")
    run_proxy.watched["/nowhere/"] = run_proxy.watched.popitem()[1]
    run_proxy.watched["/nowhere/"].proxies.append("http://second.example:1")
    assert from_core().proxies == {}  # two runs: no way to tell which one


def test_pin_keeps_the_env_ca_bundle(monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/certs/mitm.pem")
    session = requests.Session()
    run_proxy.pin(session)
    assert (session.trust_env, session.verify) == (False, "/certs/mitm.pem")
    session = requests.Session()
    session.verify = False
    run_proxy.pin(session)
    assert session.verify is False


class Sent(Exception):
    pass


class NoNetwork:
    def __getattr__(self, name):
        raise Sent


@pytest.mark.parametrize(
    ("kwargs", "exempt", "warns"), [({}, False, True), ({"proxy": PROXY}, False, False), ({}, True, False)]
)
def test_rnet_session_without_proxy_warns(caplog, kwargs, exempt, warns):
    session = RnetSession(**kwargs)
    session.proxy_exempt = exempt
    assert dict(session.proxies) == ({"all": PROXY} if kwargs else {})
    session.ensure_client = NoNetwork  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING), pytest.raises(Sent):
        session.get("https://api.example/x", max_retries=0)
    assert bool(leaks(caplog)) is warns


def test_a_failing_check_never_fails_the_request(monkeypatch):
    def broken(*args):
        raise RuntimeError("dictionary changed size during iteration")

    monkeypatch.setattr(run_proxy, "check", broken)
    session = RnetSession()
    session.ensure_client = NoNetwork  # type: ignore[method-assign]
    with pytest.raises(Sent):
        session.get("https://api.example/x", max_retries=0)


def test_core_parser_without_a_session_uses_the_run_proxy(monkeypatch):
    from unshackle.core.manifests import DASH

    seen: dict = {}

    def get(self, url, **kwargs):
        seen.update(self.proxies, trust_env=self.trust_env)
        raise Sent

    monkeypatch.setattr(requests.Session, "get", get)
    with pytest.raises(Sent):
        DASH.from_url("https://cdn.example/manifest.mpd")
    assert seen == {"all": PROXY, "trust_env": False}
