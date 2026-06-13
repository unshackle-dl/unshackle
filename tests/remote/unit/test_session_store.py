"""Unit tests for unshackle.core.api.session_store.SessionStore."""

from __future__ import annotations

import asyncio

import pytest

from unshackle.core.api.input_bridge import AuthStatus, InputBridge
from unshackle.core.api.session_store import SessionEntry, SessionStore, get_session_store

pytestmark = pytest.mark.unit


class _FakeService:
    """Minimal stub Service used to fill SessionEntry.service_instance."""

    def __init__(self, tag: str = "TEST") -> None:
        self.tag = tag


@pytest.fixture
def store() -> SessionStore:
    return SessionStore()


async def test_create_returns_entry_with_uuid(store: SessionStore) -> None:
    entry = await store.create("ATV", _FakeService())
    assert isinstance(entry, SessionEntry)
    assert entry.service_tag == "ATV"
    assert entry.session_id and len(entry.session_id) >= 32
    assert store.session_count == 1


async def test_create_with_explicit_session_id(store: SessionStore) -> None:
    entry = await store.create("NF", _FakeService(), session_id="fixed-id")
    assert entry.session_id == "fixed-id"


async def test_get_returns_none_for_missing(store: SessionStore) -> None:
    assert await store.get("nope") is None


async def test_get_touches_last_accessed(store: SessionStore) -> None:
    entry = await store.create("DSNP", _FakeService())
    before = entry.last_accessed
    await asyncio.sleep(0.01)
    fetched = await store.get(entry.session_id)
    assert fetched is entry
    assert fetched.last_accessed > before


async def test_delete_removes_and_cancels_bridge(store: SessionStore) -> None:
    entry = await store.create("CRAV", _FakeService())
    entry.input_bridge = InputBridge()
    assert entry.input_bridge.status is AuthStatus.AUTHENTICATING

    deleted = await store.delete(entry.session_id)
    assert deleted is True
    assert entry.input_bridge.status is AuthStatus.FAILED  # cancelled
    assert store.session_count == 0


async def test_delete_returns_false_when_missing(store: SessionStore) -> None:
    assert await store.delete("missing") is False


async def test_cleanup_expired_drops_old_authenticated(store: SessionStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta, timezone

    entry = await store.create("ATV", _FakeService())
    entry.last_accessed = datetime.now(timezone.utc) - timedelta(seconds=store._ttl + 100)
    removed = await store.cleanup_expired()
    assert removed == 1
    assert store.session_count == 0


async def test_cleanup_expired_keeps_pending_input_under_grace(store: SessionStore) -> None:
    """Sessions awaiting user input get a longer grace period (10 min) than authenticated TTL."""
    entry = await store.create("ATV", _FakeService())
    entry.input_bridge = InputBridge()
    entry.auth_status = AuthStatus.PENDING_INPUT
    removed = await store.cleanup_expired()
    assert removed == 0
    assert store.session_count == 1


async def test_cancel_all_bridges(store: SessionStore) -> None:
    a = await store.create("ATV", _FakeService())
    b = await store.create("NF", _FakeService())
    a.input_bridge = InputBridge()
    b.input_bridge = InputBridge()

    await store.cancel_all_bridges()
    assert a.input_bridge.status is AuthStatus.FAILED
    assert b.input_bridge.status is AuthStatus.FAILED


async def test_get_session_store_returns_singleton() -> None:
    a = get_session_store()
    b = get_session_store()
    assert a is b


async def test_max_sessions_evicts_oldest(store: SessionStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(store), "_max_sessions", property(lambda _: 2))

    await store.create("A", _FakeService(), session_id="a")
    await asyncio.sleep(0.01)
    b = await store.create("B", _FakeService(), session_id="b")
    await asyncio.sleep(0.01)
    c = await store.create("C", _FakeService(), session_id="c")

    assert store.session_count == 2
    assert await store.get("a") is None  # evicted
    assert (await store.get("b")) is b
    assert (await store.get("c")) is c
