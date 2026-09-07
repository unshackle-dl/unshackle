"""Removing a header or a cookie from an RnetSession must stop it reaching the wire.

``client.update(headers=...)`` merges into the rnet client default headers and has no
removal, and the client keeps its own cookie store. These tests read what a real local
server received, because the session dicts pass even with the leak in place.
"""

from __future__ import annotations

import threading
from http.cookiejar import Cookie, CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

import pytest
import rnet

from unshackle.core.session import RnetCookieAdapter, RnetSession

pytestmark = pytest.mark.unit


class Recorder(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list[dict[str, str]] = []
    cookies_sent: list[str] = []

    def do_GET(self) -> None:
        type(self).received.append({k.lower(): v for k, v in self.headers.items()})
        # rnet sends one Cookie header per cookie on HTTP/1.1, so a dict of the headers
        # keeps only the last one and hides a cookie that leaked.
        type(self).cookies_sent.append("; ".join(self.headers.get_all("Cookie") or []))
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        if self.path == "/login":
            self.send_header("Set-Cookie", "sid=abc123; Path=/")
        elif self.path.startswith("/set/"):
            self.send_header("Set-Cookie", f"{self.path[len('/set/') :]}; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    Recorder.received = []
    Recorder.cookies_sent = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def make_cookie(name: str, value: str, domain: str) -> Cookie:
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path="/",
        path_specified=True,
        secure=False,
        expires=None,
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
    )


def test_popped_header_stops_being_sent(server: str) -> None:
    session = RnetSession()
    session.headers.update({"X-Auth": "secret", "X-Keep": "yes"})

    session.get(f"{server}/one")
    assert Recorder.received[-1]["x-auth"] == "secret"

    session.headers.pop("X-Auth", None)
    session.get(f"{server}/two")
    assert "x-auth" not in Recorder.received[-1]
    assert Recorder.received[-1]["x-keep"] == "yes"


def test_deleted_header_stops_being_sent(server: str) -> None:
    session = RnetSession()
    session.headers["X-Auth"] = "secret"

    session.get(f"{server}/one")
    assert "x-auth" in Recorder.received[-1]

    del session.headers["X-Auth"]
    session.get(f"{server}/two")
    assert "x-auth" not in Recorder.received[-1]


def test_clear_removes_every_header(server: str) -> None:
    session = RnetSession()
    session.headers.update({"X-Auth": "secret", "X-Trace": "on"})

    session.get(f"{server}/one")
    assert "x-auth" in Recorder.received[-1]

    session.headers.clear()
    session.get(f"{server}/two")
    assert "x-auth" not in Recorder.received[-1]
    assert "x-trace" not in Recorder.received[-1]


def test_header_removal_keeps_cookies(server: str) -> None:
    """A removed header must not cost the session the cookies it has already earned."""
    session = RnetSession()
    session.headers["X-Auth"] = "secret"

    session.get(f"{server}/login")
    session.get(f"{server}/before")
    assert Recorder.received[-1].get("cookie") == "sid=abc123"

    session.headers.pop("X-Auth", None)
    session.get(f"{server}/after")
    assert Recorder.received[-1].get("cookie") == "sid=abc123"


def test_surviving_headers_keep_the_impersonate_order(server: str) -> None:
    """A rebuilt client must overlay the preset headers, not replace them."""
    session = RnetSession(impersonate=rnet.Impersonate.Chrome131)
    session.headers.update({"X-Auth": "secret", "X-Keep": "yes"})

    session.get(f"{server}/one")
    before = [k for k in Recorder.received[-1] if k != "x-auth"]

    session.headers.pop("X-Auth", None)
    session.get(f"{server}/two")
    assert [k for k in Recorder.received[-1]] == before


def test_header_removal_keeps_cookies_for_a_host_not_yet_visited(server: str) -> None:
    """A cookie unshackle set itself must survive the rebuild.

    ``rebuild_client`` can only read the rnet cookie jar per origin, and it knows only the
    origins the session has requested. Cookies loaded from a cookie file, or set through
    ``session.cookies``, are for a host the session often reaches later.
    """
    session = RnetSession()
    session.headers["X-Auth"] = "secret"
    session.get(f"{server}/one")

    session.cookies.set("sid", "abc123", domain="example.test")
    session.headers.pop("X-Auth", None)

    raw = session._client.get_cookies("https://example.test")
    assert raw is not None and b"sid=abc123" in raw


def test_rebuild_survives_an_origin_arriving_mid_rebuild(server: str, monkeypatch) -> None:
    """``request`` records origins from the download threads while the rebuild reads them.

    Iterating the live set raises ``RuntimeError: Set changed size during iteration`` out of the
    header removal. Adding an origin from inside the copy stands in for that race.
    """
    session = RnetSession()
    session.headers["X-Auth"] = "secret"
    session.get(f"{server}/one")

    original = RnetSession.__dict__["copy_cookies"].__func__

    def racing_copy(source, target, origin: str) -> None:
        session._origins.add(f"https://late-{len(session._origins)}.test")
        original(source, target, origin)

    monkeypatch.setattr(RnetSession, "copy_cookies", staticmethod(racing_copy))

    session.headers.pop("X-Auth", None)

    session.get(f"{server}/two")
    assert "x-auth" not in Recorder.received[-1]


def test_clear_drops_a_server_set_cookie(server: str) -> None:
    """``cookies.clear()`` must reach the rnet cookie store, not only the local dicts."""
    session = RnetSession()

    session.get(f"{server}/set/sid=abc123")
    session.get(f"{server}/before")
    assert Recorder.cookies_sent[-1] == "sid=abc123"

    session.cookies.clear()
    session.get(f"{server}/after")
    assert Recorder.cookies_sent[-1] == ""


def test_clear_leaves_the_session_able_to_take_new_cookies(server: str) -> None:
    """A cleared session must still hold the next cookie, and only that one."""
    session = RnetSession()

    session.get(f"{server}/set/sid=abc123")
    session.cookies.clear()

    session.get(f"{server}/set/fresh=1")
    session.get(f"{server}/after")
    assert Recorder.cookies_sent[-1] == "fresh=1"


def test_delete_drops_only_the_named_server_set_cookie(server: str) -> None:
    """``del cookies[name]`` must drop that name from the store and leave the rest."""
    session = RnetSession()

    session.get(f"{server}/set/sid=abc123")
    session.get(f"{server}/set/other=keep")

    del session.cookies["sid"]

    session.get(f"{server}/after")
    assert Recorder.cookies_sent[-1] == "other=keep"


def test_clear_by_name_drops_a_server_set_cookie(server: str) -> None:
    """``clear(name=...)`` is the requests spelling a service reaches for."""
    session = RnetSession()

    session.get(f"{server}/set/sid=abc123")
    session.get(f"{server}/before")
    assert Recorder.cookies_sent[-1] == "sid=abc123"

    session.cookies.clear(name="sid")
    session.get(f"{server}/after")
    assert Recorder.cookies_sent[-1] == ""


def test_clear_before_the_client_exists_empties_the_jar_and_sends_nothing(server: str) -> None:
    """``clear()`` on a session that has made no request must empty every cookie view.

    ``dl.save_cookies`` writes ``cookies.jar`` back to the cookie file and only ever merges,
    so a cleared cookie left in the jar returns to disk.
    """
    session = RnetSession()
    jar = CookieJar()
    jar.set_cookie(make_cookie("sid", "abc123", "127.0.0.1"))
    session.cookies.update(jar)
    session.cookies.set("other", "keep", domain="127.0.0.1")

    session.cookies.clear()

    assert dict(session.cookies) == {}
    assert session.cookies.get_dict() == {}
    assert list(session.cookies.jar) == []

    session.get(f"{server}/after")
    assert Recorder.cookies_sent[-1] == ""


def test_a_later_header_rebuild_does_not_resurrect_cleared_cookies(server: str) -> None:
    """``rebuild_client`` replays the buffered cookies and copies the old jar per origin.

    A ``clear()`` that left either populated hands the cookie back on the next header removal.
    """
    session = RnetSession()
    session.headers["X-Auth"] = "secret"

    session.get(f"{server}/set/sid=abc123")
    session.cookies.clear()

    session.headers.pop("X-Auth", None)

    session.get(f"{server}/after")
    assert Recorder.cookies_sent[-1] == ""


def test_a_request_during_the_rebuild_still_carries_headers_and_cookies(server: str, monkeypatch) -> None:
    """A request from another thread must never see a client with neither headers nor cookies.

    ``rebuild_client`` publishes the new client before it carries the headers and the cookies,
    so a request in that window goes out unauthenticated.
    """
    session = RnetSession()
    session.headers.update({"X-Auth": "secret", "X-Trace": "on"})

    session.get(f"{server}/set/sid=abc123")
    session.get(f"{server}/before")
    assert Recorder.cookies_sent[-1] == "sid=abc123"

    original = RnetCookieAdapter.flush_to_client
    rebuilding: list[bool] = []

    def flush_with_a_request(self) -> None:
        if rebuilding:
            session.get(f"{server}/mid")
        original(self)

    monkeypatch.setattr(RnetCookieAdapter, "flush_to_client", flush_with_a_request)
    rebuilding.append(True)
    session.headers.pop("X-Trace", None)

    assert Recorder.received[-1].get("x-auth") == "secret"
    assert Recorder.cookies_sent[-1] == "sid=abc123"


def test_the_rebuilt_client_is_published_only_when_it_is_ready(server: str, monkeypatch) -> None:
    """The new client must go on ``_client`` last, after the headers, the flush and the copy."""
    session = RnetSession()
    session.headers["X-Auth"] = "secret"
    session.get(f"{server}/set/sid=abc123")

    old = session._client
    seen: list[object] = []
    rebuilding: list[bool] = []

    original_flush = RnetCookieAdapter.flush_to_client
    original_copy = RnetSession.__dict__["copy_cookies"].__func__

    def recording_flush(self) -> None:
        if rebuilding:
            seen.append(session._client)
        original_flush(self)

    def recording_copy(source, target, origin: str) -> None:
        seen.append(session._client)
        original_copy(source, target, origin)

    monkeypatch.setattr(RnetCookieAdapter, "flush_to_client", recording_flush)
    monkeypatch.setattr(RnetSession, "copy_cookies", staticmethod(recording_copy))
    rebuilding.append(True)
    session.headers.pop("X-Auth", None)

    assert seen and all(client is old for client in seen)
    assert session._client is not old
