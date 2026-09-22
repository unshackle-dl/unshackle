"""Unit tests for unshackle.core.api.session_store.SessionStore."""

from __future__ import annotations

import asyncio
import time

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
    entry = await store.create("EXAMPLE", _FakeService())
    assert isinstance(entry, SessionEntry)
    assert entry.service_tag == "EXAMPLE"
    assert entry.session_id and len(entry.session_id) >= 32
    assert store.session_count == 1


async def test_create_with_explicit_session_id(store: SessionStore) -> None:
    entry = await store.create("DEMO", _FakeService(), session_id="fixed-id")
    assert entry.session_id == "fixed-id"


async def test_get_returns_none_for_missing(store: SessionStore) -> None:
    assert await store.get("nope") is None


async def test_get_touches_last_accessed(store: SessionStore) -> None:
    entry = await store.create("DEMO", _FakeService())
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

    entry = await store.create("EXAMPLE", _FakeService())
    entry.last_accessed = datetime.now(timezone.utc) - timedelta(seconds=store.ttl + 100)
    removed = await store.cleanup_expired()
    assert removed == 1
    assert store.session_count == 0


async def test_cleanup_expired_keeps_pending_input_under_grace(store: SessionStore) -> None:
    """Sessions awaiting user input get a longer grace period (10 min) than authenticated TTL."""
    entry = await store.create("EXAMPLE", _FakeService())
    entry.input_bridge = InputBridge()
    entry.auth_status = AuthStatus.PENDING_INPUT
    removed = await store.cleanup_expired()
    assert removed == 0
    assert store.session_count == 1


async def test_cleanup_expired_drops_pending_input_past_grace(store: SessionStore) -> None:
    """The auth-state grace is the single AUTH_INPUT_TIMEOUT rule, not a separate magic number."""
    from datetime import datetime, timedelta, timezone

    from unshackle.core.api.input_bridge import AUTH_INPUT_TIMEOUT

    entry = await store.create("EXAMPLE", _FakeService())
    entry.input_bridge = InputBridge()
    entry.auth_status = AuthStatus.PENDING_INPUT
    entry.last_accessed = datetime.now(timezone.utc) - timedelta(seconds=AUTH_INPUT_TIMEOUT + 1)
    removed = await store.cleanup_expired()
    assert removed == 1
    assert store.session_count == 0


async def test_cancel_all_bridges(store: SessionStore) -> None:
    a = await store.create("EXAMPLE", _FakeService())
    b = await store.create("DEMO", _FakeService())
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
    monkeypatch.setattr(type(store), "max_sessions", property(lambda _: 2))

    await store.create("A", _FakeService(), session_id="a")
    await asyncio.sleep(0.01)
    b = await store.create("B", _FakeService(), session_id="b")
    await asyncio.sleep(0.01)
    c = await store.create("C", _FakeService(), session_id="c")

    assert store.session_count == 2
    assert await store.get("a") is None  # evicted
    assert (await store.get("b")) is b
    assert (await store.get("c")) is c


async def test_get_expiry_removes_cache_dir(store: SessionStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta, timezone

    removed: list = []
    monkeypatch.setattr(SessionStore, "cleanup_cache_dir", staticmethod(removed.append))
    entry = await store.create("EXAMPLE", _FakeService())
    entry.cache_tag = "_sessions/k/s/EXAMPLE"
    entry.last_accessed = datetime.now(timezone.utc) - timedelta(seconds=store.ttl + 100)
    assert await store.get(entry.session_id) is None
    assert removed == ["_sessions/k/s/EXAMPLE"]


async def test_session_end_applies_staged_service_reload(store: SessionStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A staged reload swaps in as soon as the last session on that tag ends, not on the next refresh tick."""
    from unshackle.core import services
    from unshackle.core.api import download_manager

    applied: list[set[str]] = []

    def fake_apply(busy: set[str]) -> list[str]:
        applied.append(busy)
        return sorted(services.PENDING - busy)

    monkeypatch.setattr(services, "PENDING", {"EXAMPLE"})
    monkeypatch.setattr(services, "apply_pending", fake_apply)
    monkeypatch.setattr(download_manager, "publish_service_event", lambda *a, **k: None)

    entry = await store.create("EXAMPLE", _FakeService())
    assert await store.delete(entry.session_id)
    await asyncio.sleep(0.05)
    assert applied == [set()]


def _stage_reload(monkeypatch: pytest.MonkeyPatch, pending: set[str], delay: float = 0.0) -> list[set[str]]:
    """Stage ``pending`` for reload and record every apply_pending call; returns the call log."""
    from unshackle.core import services
    from unshackle.core.api import download_manager

    applied: list[set[str]] = []

    def fake_apply(busy: set[str]) -> list[str]:
        if delay:
            time.sleep(delay)
        applied.append(busy)
        return sorted(services.PENDING - busy)

    monkeypatch.setattr(services, "PENDING", pending)
    monkeypatch.setattr(services, "apply_pending", fake_apply)
    monkeypatch.setattr(download_manager, "publish_service_event", lambda *a, **k: None)
    monkeypatch.setattr(download_manager, "_reload_task", None)
    return applied


async def test_session_end_on_unstaged_tag_skips_reload(store: SessionStore, monkeypatch: pytest.MonkeyPatch) -> None:
    applied = _stage_reload(monkeypatch, {"OTHER"})

    entry = await store.create("EXAMPLE", _FakeService())
    assert await store.delete(entry.session_id)
    await asyncio.sleep(0.05)
    assert applied == []


async def test_concurrent_session_ends_reload_once(store: SessionStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Session ends that overlap an in-flight reload share it instead of each re-importing the tag."""
    applied = _stage_reload(monkeypatch, {"EXAMPLE"}, delay=0.05)

    ids = [(await store.create("EXAMPLE", _FakeService())).session_id for _ in range(10)]
    await asyncio.gather(*(store.delete(sid) for sid in ids))
    await asyncio.sleep(0.3)
    assert len(applied) == 1


async def test_eviction_applies_staged_service_reload(store: SessionStore, monkeypatch: pytest.MonkeyPatch) -> None:
    applied = _stage_reload(monkeypatch, {"EXAMPLE"})
    monkeypatch.setattr(type(store), "max_sessions", property(lambda _: 1))

    await store.create("EXAMPLE", _FakeService(), session_id="a")
    await store.create("OTHER", _FakeService(), session_id="b")
    await asyncio.sleep(0.05)
    assert len(applied) == 1


async def test_inline_expiry_applies_staged_service_reload(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    applied = _stage_reload(monkeypatch, {"EXAMPLE"})

    entry = await store.create("EXAMPLE", _FakeService())
    entry.last_accessed = datetime.now(timezone.utc) - timedelta(seconds=store.ttl + 100)
    assert await store.get(entry.session_id) is None
    await asyncio.sleep(0.05)
    assert applied == [set()]


async def test_cleanup_expired_applies_staged_service_reload_once(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup sweep that drops several sessions on one tag schedules one reload for that tag."""
    from datetime import datetime, timedelta, timezone

    applied = _stage_reload(monkeypatch, {"EXAMPLE"})
    stale = datetime.now(timezone.utc) - timedelta(seconds=store.ttl + 100)
    for sid in ("a", "b"):
        (await store.create("EXAMPLE", _FakeService(), session_id=sid)).last_accessed = stale

    assert await store.cleanup_expired() == 2
    await asyncio.sleep(0.05)
    assert applied == [set()]


async def test_live_session_on_same_tag_keeps_it_busy(store: SessionStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ending one of two sessions on a staged tag reports the tag busy; ending the last one frees it."""
    from unshackle.core.api import download_manager
    from unshackle.core.api import session_store as session_store_module

    applied = _stage_reload(monkeypatch, {"EXAMPLE"})
    monkeypatch.setattr(session_store_module, "session_store", store)
    monkeypatch.setattr(download_manager, "download_manager", None)

    first = await store.create("EXAMPLE", _FakeService())
    second = await store.create("EXAMPLE", _FakeService())
    assert await store.delete(first.session_id)
    await asyncio.sleep(0.05)
    assert applied == [{"EXAMPLE"}]

    assert await store.delete(second.session_id)
    await asyncio.sleep(0.05)
    assert applied == [{"EXAMPLE"}, set()]
