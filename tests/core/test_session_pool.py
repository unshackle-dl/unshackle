"""Unit tests for grow_session_pool, which enlarges a shared requests session's
connection pool before download threads start. The tests open no sockets and check
only the adapter mounts."""

from __future__ import annotations

import pytest
import requests
from requests.adapters import HTTPAdapter

from unshackle.core.service import TimeoutHTTPAdapter, grow_session_pool

pytestmark = pytest.mark.unit


class FakeRnetSession:
    """Stands in for RnetSession, which is not a requests.Session, so grow_session_pool skips it."""

    def get_adapter(self, url: str) -> None:
        raise AssertionError("grow_session_pool must not touch a non-requests session")


def test_small_pool_is_remounted_on_both_schemes() -> None:
    session = requests.Session()
    old = session.get_adapter("https://x")
    assert old._pool_maxsize < 64

    grow_session_pool(session, 64)

    https = session.get_adapter("https://x")
    http = session.get_adapter("http://x")
    assert isinstance(https, TimeoutHTTPAdapter)
    assert https is http
    assert https is not old
    assert https._pool_maxsize == 64
    assert https._pool_block is True
    assert https.max_retries is old.max_retries


def test_large_enough_pool_is_left_alone() -> None:
    session = requests.Session()
    session.mount("https://", HTTPAdapter(pool_connections=64, pool_maxsize=64))
    old = session.get_adapter("https://x")

    grow_session_pool(session, 64)

    assert session.get_adapter("https://x") is old


def test_equal_size_pool_is_left_alone() -> None:
    session = requests.Session()
    session.mount("https://", HTTPAdapter(pool_maxsize=32))
    old = session.get_adapter("https://x")

    grow_session_pool(session, 32)

    assert session.get_adapter("https://x") is old


@pytest.mark.parametrize("session", [object(), FakeRnetSession()])
def test_non_requests_session_is_ignored(session: object) -> None:
    grow_session_pool(session, 64)
