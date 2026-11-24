"""Unit tests for grow_session_pool, which enlarges a shared requests session's
connection pool before download threads start. The tests open no sockets and check
only the adapter mounts."""

from __future__ import annotations

from copy import copy
from typing import Any
from unittest.mock import patch

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from unshackle.core.service import DEFAULT_TIMEOUT, TimeoutHTTPAdapter, grow_session_pool
from unshackle.core.utils.sslciphers import SSLCiphers

pytestmark = pytest.mark.unit


class FakeRnetSession:
    """Stands in for RnetSession, which is not a requests.Session, so grow_session_pool skips it."""

    def get_adapter(self, url: str) -> None:
        raise AssertionError("grow_session_pool must not touch a non-requests session")


def sent_timeout(adapter: HTTPAdapter) -> Any:
    """The timeout an adapter hands to HTTPAdapter.send when the caller passes none."""
    seen: dict[str, Any] = {}

    def record(self: object, request: object, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "sent"

    with patch.object(HTTPAdapter, "send", record):
        assert adapter.send(object(), timeout=None) == "sent"

    return seen["timeout"]


def test_subclass_adapter_keeps_its_class_and_tls_context() -> None:
    """A service-mounted SSLCiphers must survive the growth with its TLS context.

    CBS, friDay, iP, MY5 and PMTP all mount SSLCiphers on the service session and none
    override get_session(). Replacing it with a plain adapter drops DEFAULT:@SECLEVEL=2 and
    check_hostname=False, so metadata succeeds and every segment then fails at TLS or 403.
    """
    session = requests.Session()
    session.mount("https://", SSLCiphers(security_level=2))
    old = session.get_adapter("https://x")
    context = old._ssl_context
    assert old._pool_maxsize < 64

    grow_session_pool(session, 64)

    grown = session.get_adapter("https://x")
    assert grown is old
    assert isinstance(grown, SSLCiphers)
    assert grown._ssl_context is context
    assert grown.poolmanager.connection_pool_kw["ssl_context"] is context
    assert grown._pool_maxsize == 64
    assert grown._pool_block is True


def test_a_distinct_http_adapter_is_grown_not_overwritten() -> None:
    """Every mounted adapter grows, and no mount overwrites another.

    The https:// adapter must not land on the http:// prefix, and a service that mounts on a
    narrow prefix must be grown as well.
    """
    session = requests.Session()
    secure = SSLCiphers(security_level=2)
    narrow = SSLCiphers(security_level=2)
    plain = TimeoutHTTPAdapter(pool_connections=10, pool_maxsize=10)
    session.mount("https://", secure)
    session.mount("https://cbs.example/", narrow)
    session.mount("http://", plain)

    grow_session_pool(session, 64)

    assert session.get_adapter("https://x") is secure
    assert session.get_adapter("https://cbs.example/a") is narrow
    assert session.get_adapter("http://x") is plain
    assert session.get_adapter("http://x") is not session.get_adapter("https://x")
    for adapter in (secure, narrow, plain):
        assert adapter._pool_maxsize == 64
        assert adapter._pool_connections == 64


def test_shared_adapter_is_grown_in_place_on_both_schemes() -> None:
    session = requests.Session()
    adapter = TimeoutHTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=Retry(total=5))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    retries = adapter.max_retries
    timeout = adapter.default_timeout

    grow_session_pool(session, 64)

    https = session.get_adapter("https://x")
    assert https is adapter
    assert session.get_adapter("http://x") is adapter
    assert type(https) is TimeoutHTTPAdapter
    assert https.max_retries is retries
    assert https.default_timeout == timeout
    assert https._pool_maxsize == 64
    assert https._pool_connections == 64
    assert https._pool_block is True
    assert https.poolmanager.connection_pool_kw["maxsize"] == 64


def test_cached_proxy_manager_is_rebuilt_at_the_new_size() -> None:
    """proxy_manager_for caches one manager per proxy URL, sized when it was first built.

    authenticate() and get_tracks() run before the growth, so a service behind --proxy has
    already filled that cache. Leaving it there keeps every proxied segment on the small pool.
    """
    session = requests.Session()
    adapter = TimeoutHTTPAdapter(pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    proxy = "http://proxy.example:8080"
    stale = adapter.proxy_manager_for(proxy)
    assert stale.connection_pool_kw["maxsize"] == 4

    grow_session_pool(session, 64)

    fresh = adapter.proxy_manager_for(proxy)
    assert fresh.connection_pool_kw["maxsize"] == 64
    assert fresh is not stale


def test_large_enough_pool_is_left_alone() -> None:
    session = requests.Session()
    session.mount("https://", HTTPAdapter(pool_connections=64, pool_maxsize=64))
    old = session.get_adapter("https://x")
    pool = old.poolmanager

    grow_session_pool(session, 64)

    assert session.get_adapter("https://x") is old
    assert old.poolmanager is pool, "an adapter that needs no growth must keep its warm connections"


def test_equal_size_pool_is_left_alone() -> None:
    session = requests.Session()
    session.mount("https://", HTTPAdapter(pool_maxsize=32))
    old = session.get_adapter("https://x")
    pool = old.poolmanager

    grow_session_pool(session, 32)

    assert session.get_adapter("https://x") is old
    assert old.poolmanager is pool


@pytest.mark.parametrize("session", [object(), FakeRnetSession()])
def test_non_requests_session_is_ignored(session: object) -> None:
    grow_session_pool(session, 64)


def test_copied_adapter_keeps_the_default_timeout() -> None:
    """default_timeout must survive copy(), which no_retry_session runs per segment.

    copy() runs HTTPAdapter.__getstate__/__setstate__, which carry only the names in
    __attrs__. Without default_timeout there, the copy quietly falls back to DEFAULT_TIMEOUT
    on the segment path and drops the service timeout.

    The last two lines pin the getattr fallback in send(). A subclass that redefines
    __attrs__ from HTTPAdapter.__attrs__, as SSLCiphers does, drops default_timeout again.
    """
    twin = copy(TimeoutHTTPAdapter(timeout=(3, 300), pool_connections=4, pool_maxsize=4))
    assert twin.default_timeout == (3, 300)
    assert sent_timeout(twin) == (3, 300)

    del twin.default_timeout
    assert sent_timeout(twin) == DEFAULT_TIMEOUT


def test_growing_does_not_revert_a_custom_default_timeout() -> None:
    session = requests.Session()
    session.mount("https://", TimeoutHTTPAdapter(timeout=(1, 5), pool_connections=4, pool_maxsize=4))
    old = session.get_adapter("https://x")

    grow_session_pool(session, 64)

    grown = session.get_adapter("https://x")
    assert grown.default_timeout == (1, 5)
    assert grown is old
    assert copy(grown).default_timeout == (1, 5), "the per-segment copy must keep the service timeout"
