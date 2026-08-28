"""Dashboard auth split, stats middleware, log ring buffer and session events."""

from __future__ import annotations

import asyncio
import logging

import pytest
from aiohttp import web

from unshackle.core.api.events import bus
from unshackle.core.api.handlers import dashboard_authentication
from unshackle.core.api.routes import cors_middleware, setup_routes
from unshackle.core.api.session_store import SessionStore
from unshackle.core.api.stats import RingLogHandler, stats, stats_middleware
from unshackle.core.config import config

pytestmark = pytest.mark.unit


@pytest.fixture
def dashboard_cfg(monkeypatch):
    monkeypatch.setitem(config.serve, "dashboard", {"key": "dash-secret"})
    monkeypatch.setitem(config.serve, "users", {"tier-key": {"username": "tier1"}})
    stats.requests_total = 0
    stats.requests_by_key.clear()


def make_app(dashboard: bool = True) -> web.Application:
    app = web.Application(middlewares=[cors_middleware, stats_middleware, dashboard_authentication])
    app["config"] = {"users": {}}
    app["debug_api"] = False
    setup_routes(app, remote_only=True, dashboard=dashboard)
    return app


async def test_dashboard_key_split(aiohttp_client, dashboard_cfg) -> None:
    client = await aiohttp_client(make_app())
    assert (await client.get("/api/dashboard/status", headers={"X-Secret-Key": "tier-key"})).status == 401
    assert (await client.get("/api/dashboard/status")).status == 401
    resp = await client.get("/api/dashboard/status", headers={"X-Secret-Key": "dash-secret"})
    assert resp.status == 200
    body = await resp.json()
    assert body["mode"] and "uptime_seconds" in body and body["sessions"] == 0
    # rejected requests count only as rejected; accepted ones are keyed by masked label
    assert stats.requests_total == 3 and stats.requests_rejected == 2 and stats.requests_by_key == {"dash…": 1}
    await client.get("/api/health", headers={"X-Secret-Key": "tier-key"})
    assert stats.requests_by_key["tier1"] == 1


async def test_dashboard_routes_absent_without_key(aiohttp_client, dashboard_cfg) -> None:
    client = await aiohttp_client(make_app(dashboard=False))
    assert (await client.get("/api/dashboard/status", headers={"X-Secret-Key": "dash-secret"})).status == 404


def test_ring_since_and_level() -> None:
    ring = RingLogHandler(maxlen=3)
    ring.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test.ring")
    logger.propagate = False
    logger.addHandler(ring)
    logger.setLevel(logging.DEBUG)
    for i in range(5):
        (logger.warning if i % 2 else logger.info)(f"m{i}")
    assert [r["msg"] for r in ring.records] == ["m2", "m3", "m4"]
    assert ring.seq == 5
    assert [r["msg"] for r in ring.since(3)] == ["m3", "m4"]
    assert [r["msg"] for r in ring.since(0, "warning")] == ["m3"]
    assert ring.since(0, logger="test") and not ring.since(0, logger="other")


async def test_session_events() -> None:
    queue = bus.subscribe()
    try:
        store = SessionStore()
        entry = await store.create("EXAMPLE", object())
        assert [e.session_id for e in store.list()] == [entry.session_id]
        await store.delete(entry.session_id)
        create, delete = queue.get_nowait(), queue.get_nowait()
        assert create["event"] == "session" and create["data"]["action"] == "create"
        assert delete["data"]["action"] == "delete" and delete["data"]["id"] == entry.session_id
        assert "owner_key" not in create["data"]
    finally:
        bus.unsubscribe(queue)
    await asyncio.sleep(0)


async def test_events_burst_mode(aiohttp_client, dashboard_cfg) -> None:
    client = await aiohttp_client(make_app())
    mark = bus.seq
    bus.publish("session", {"action": "create", "id": "s1"})
    bus.publish("session", {"action": "delete", "id": "s1"})
    resp = await client.get(f"/api/dashboard/events?since={mark}", headers={"X-Secret-Key": "dash-secret"})
    assert resp.status == 200
    body = await resp.json()
    assert body["seq"] == bus.seq and "stats" in body
    assert [e["data"]["action"] for e in body["events"] if e["event"] == "session"] == ["create", "delete"]


async def test_session_action_log() -> None:
    store = SessionStore()
    entry = await store.create("EXAMPLE", object(), owner_key="k")
    queue = bus.subscribe()
    try:
        store.record_action(entry.session_id, {"action": "titles", "status": 200})
        store.record_action("missing", {"action": "titles", "status": 200})
        assert entry.summary()["actions"] == [{"action": "titles", "status": 200}]
        assert queue.get_nowait()["data"]["action"] == "update" and queue.empty()
    finally:
        bus.unsubscribe(queue)
