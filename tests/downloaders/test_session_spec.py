"""Session spec fidelity across the multiprocess spawn boundary.

``build_session_spec``/``rebuild_session`` are how a spawned child gets the parent's session
state. The cross-process half needs a live CDN to observe, so these tests pin the halves that
can be checked deterministically: the spec is picklable (spawn requires it), and a rebuilt
session carries the parent's cookie domains, query params, and TLS settings. A spec that
flattens cookies to name/value rebuilds every cookie scoped to localhost, so a cookie-authed
CDN sees none of them and 403s only when ``--download-processes`` is above 1. State the child
cannot reproduce (a service-mounted adapter, an unpicklable auth callable) must give None,
which drops the caller back to a single process.
"""

import pickle
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from requests import Session
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth

from unshackle.core.downloaders.requests import build_session_spec, rebuild_session
from unshackle.core.service import Service
from unshackle.core.session import RnetSession
from unshackle.core.session import session as make_session
from unshackle.core.utils.sslciphers import SSLCiphers


def rnet_session_with_cookies() -> RnetSession:
    """RnetSession carrying two domain-scoped cookies plus one set from a plain dict.

    The dict-set cookie lands in the adapter's flat map only, never in its domain map, so it
    is the case a domain-only serialization would silently drop.
    """
    ns = make_session("Chrome131")
    ns.cookies.set("cdn_token", "abc", domain="cdn.example.com")
    ns.cookies.set("api_token", "def", domain="api.example.com")
    ns.cookies.update({"loose": "ghi"})
    return ns


def test_rnet_spec_round_trips_cookie_domains() -> None:
    ns = rnet_session_with_cookies()

    spec = build_session_spec(ns)
    assert spec is not None
    rebuilt = rebuild_session(pickle.loads(pickle.dumps(spec)), 1)
    assert rebuilt is not None

    assert rebuilt.cookies.get_dict_by_domain() == ns.cookies.get_dict_by_domain()
    assert rebuilt.cookies.get_dict() == ns.cookies.get_dict()
    assert rebuilt.cookies.get("cdn_token", domain="cdn.example.com") == "abc"
    assert rebuilt.cookies.get("api_token", domain="api.example.com") == "def"
    assert rebuilt.cookies.get_dict(domain="localhost") == {}


def test_rnet_spec_keeps_dict_set_cookies() -> None:
    ns = rnet_session_with_cookies()

    spec = build_session_spec(ns)
    assert spec is not None
    rebuilt = rebuild_session(spec, 1)
    assert rebuilt is not None

    assert rebuilt.cookies["loose"] == "ghi"


def test_rnet_spec_cookies_carry_domain_information() -> None:
    ns = rnet_session_with_cookies()

    spec = build_session_spec(ns)
    assert spec is not None

    cookies = spec["cookies"]
    assert set(cookies) == {"cdn.example.com", "api.example.com", ""}
    assert all(isinstance(jar, dict) for jar in cookies.values())
    assert cookies["cdn.example.com"] == {"cdn_token": "abc"}


def test_rnet_spec_is_picklable() -> None:
    spec = build_session_spec(rnet_session_with_cookies())
    assert spec is not None

    assert pickle.loads(pickle.dumps(spec)) == spec


def test_rnet_spec_without_impersonate_is_not_rebuildable() -> None:
    assert build_session_spec(RnetSession()) is None


class _CookieHandler(BaseHTTPRequestHandler):
    """Sets one cookie on /set and echoes the Cookie header it receives on /echo."""

    def do_GET(self) -> None:
        body = b""
        self.send_response(200)
        if self.path == "/set":
            self.send_header("Set-Cookie", "sid=server-set; Path=/")
        else:
            body = (self.headers.get("Cookie") or "").encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output quiet
        pass


@pytest.fixture
def cookie_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _CookieHandler)
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    thread.join(timeout=5)


def test_rnet_spec_carries_server_set_cookies(cookie_server) -> None:
    """A server Set-Cookie lands only in the rnet client jar, and the spec has to carry it across."""
    ns = make_session("Chrome131")
    ns.get(f"{cookie_server}/set")

    spec = build_session_spec(ns)
    assert spec is not None
    assert spec["origin_cookies"] == {cookie_server: "sid=server-set"}

    rebuilt = rebuild_session(pickle.loads(pickle.dumps(spec)), 1)
    assert rebuilt is not None

    assert "sid=server-set" in rebuilt.get(f"{cookie_server}/echo").text


def test_rebuilt_requests_session_pool_is_sized_to_worker_count() -> None:
    """A child rebuilding a requests session must size its pool, not inherit urllib3's default 10."""
    spec = build_session_spec(Session())
    assert spec is not None

    rebuilt = rebuild_session(spec, 24)
    assert rebuilt is not None

    for scheme in ("https://example.com", "http://example.com"):
        adapter = rebuilt.get_adapter(scheme)
        assert adapter._pool_maxsize == 24
        assert adapter._pool_connections == 24
        assert adapter._pool_block is True
        assert adapter.poolmanager.connection_pool_kw["maxsize"] == 24


def test_rebuilt_rnet_session_has_no_adapters() -> None:
    """rnet pools internally; the sizing fix must not reach into that path."""
    spec = build_session_spec(rnet_session_with_cookies())
    assert spec is not None

    rebuilt = rebuild_session(spec, 24)
    assert rebuilt is not None

    assert not isinstance(rebuilt, Session)
    assert not hasattr(rebuilt, "adapters")


def test_unrebuildable_spec_stays_none() -> None:
    assert rebuild_session({"kind": "none"}, 24) is None


def test_requests_spec_round_trips_cookie_domains() -> None:
    rs = Session()
    rs.cookies.set("cdn_token", "abc", domain="cdn.example.com")
    rs.cookies.set("api_token", "def", domain="api.example.com")

    spec = build_session_spec(rs)
    assert spec is not None
    rebuilt = rebuild_session(pickle.loads(pickle.dumps(spec)), 1)
    assert rebuilt is not None

    assert rebuilt.cookies.get_dict(domain="cdn.example.com") == {"cdn_token": "abc"}
    assert rebuilt.cookies.get_dict(domain="api.example.com") == {"api_token": "def"}


def test_requests_spec_carries_verify() -> None:
    """A service that turns certificate verification off must not get it turned back on."""
    rs = Session()
    rs.verify = False

    spec = build_session_spec(rs)
    assert spec is not None
    rebuilt = rebuild_session(pickle.loads(pickle.dumps(spec)), 1)
    assert rebuilt is not None

    assert rebuilt.verify is False


def test_requests_spec_carries_params() -> None:
    """Services hold an auth token in session.params; without it every segment request is unauthed."""
    rs = Session()
    rs.params = {"at": "auth-token"}

    spec = build_session_spec(rs)
    assert spec is not None
    rebuilt = rebuild_session(pickle.loads(pickle.dumps(spec)), 1)
    assert rebuilt is not None

    assert dict(rebuilt.params) == {"at": "auth-token"}


def test_requests_spec_carries_cert_and_auth() -> None:
    rs = Session()
    rs.cert = ("/tmp/client.pem", "/tmp/client.key")
    rs.auth = HTTPBasicAuth("user", "pass")

    spec = build_session_spec(rs)
    assert spec is not None
    rebuilt = rebuild_session(pickle.loads(pickle.dumps(spec)), 1)
    assert rebuilt is not None

    assert rebuilt.cert == ("/tmp/client.pem", "/tmp/client.key")
    assert rebuilt.auth == HTTPBasicAuth("user", "pass")


def test_requests_spec_is_picklable() -> None:
    rs = Session()
    rs.verify = False
    rs.params = {"at": "auth-token"}
    rs.cookies.set("cdn_token", "abc", domain="cdn.example.com")

    spec = build_session_spec(rs)
    assert spec is not None

    assert pickle.loads(pickle.dumps(spec))["params"] == {"at": "auth-token"}


def test_requests_spec_with_unpicklable_auth_is_not_rebuildable() -> None:
    """auth takes any callable. One that spawn cannot pickle has to fall back, not kill the child."""
    rs = Session()
    rs.auth = lambda request: request

    assert build_session_spec(rs) is None


def test_service_session_stays_rebuildable() -> None:
    """The framework default adapter is reproducible. Failing it closed would disable the fan-out for every service."""
    spec = build_session_spec(Service.get_session())

    assert spec is not None


def test_mounted_ssl_ciphers_is_not_rebuildable() -> None:
    """SSLCiphers swaps the SSL context, so a child mounting a plain adapter negotiates a different cipher list."""
    rs = Session()
    rs.mount("https://cdn.example.com", SSLCiphers(security_level=2))

    assert build_session_spec(rs) is None


def test_rebuilt_requests_session_mounts_a_plain_adapter() -> None:
    """The spec only ever describes a plain adapter, so the child must not appear to hold more than that."""
    spec = build_session_spec(Session())
    assert spec is not None
    rebuilt = rebuild_session(spec, 4)
    assert rebuilt is not None

    assert type(rebuilt.get_adapter("https://example.com")) is HTTPAdapter
