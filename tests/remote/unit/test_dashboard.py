"""Dashboard auth split, stats middleware, log ring buffer and session events."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from aiohttp import web

from unshackle.core.api.events import bus
from unshackle.core.api.handlers import api_key_authentication, dashboard_authentication
from unshackle.core.api.routes import cors_middleware, setup_routes
from unshackle.core.api.session_store import SessionStore
from unshackle.core.api.stats import RATE_LIMIT_WINDOW, RingLogHandler, key_id, stats, stats_middleware
from unshackle.core.config import config

pytestmark = pytest.mark.unit


@pytest.fixture
def dashboard_cfg(monkeypatch):
    monkeypatch.setitem(config.serve, "dashboard", {"key": "dash-secret"})
    monkeypatch.setitem(config.serve, "users", {"tier-key": {"username": "tier1"}})
    stats.requests_total = 0
    stats.requests_rejected = 0
    stats.keys.clear()


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


async def test_key_stats_separate_unnamed_keys_sharing_a_prefix(aiohttp_client, monkeypatch) -> None:
    """mask_key truncates to four characters, so two unnamed keys can share a label."""
    monkeypatch.setitem(config.serve, "dashboard", {"key": "dash-secret"})
    monkeypatch.setitem(config.serve, "users", {"abcd-one": {}, "abcd-two": {}})
    stats.keys.clear()
    client = await aiohttp_client(make_app())
    await client.get("/api/health", headers={"X-Secret-Key": "abcd-one"})
    for _ in range(2):
        await client.get("/api/health", headers={"X-Secret-Key": "abcd-two"})

    # The legacy label-keyed view still merges them; that is why /keys exists.
    assert stats.requests_by_key["abcd…"] == 3
    assert stats.keys[key_id("abcd-one")].requests == 1
    assert stats.keys[key_id("abcd-two")].requests == 2

    resp = await client.get("/api/dashboard/keys", headers={"X-Secret-Key": "dash-secret"})
    rows = {r["id"]: r for r in await resp.json()}
    assert rows[key_id("abcd-one")]["requests"] == 1
    assert rows[key_id("abcd-two")]["requests"] == 2
    assert all(r["rate_limit"] is None for r in rows.values())


def test_rate_limit_window(monkeypatch) -> None:
    monkeypatch.setitem(config.serve, "tiers", {"bot": {"rate_limit": 2}})
    monkeypatch.setitem(config.serve, "users", {"k": {"tier": "bot"}, "free": {}})
    stats.keys.clear()

    assert stats.check_rate_limit("k") is None
    assert stats.check_rate_limit("k") is None
    retry_after = stats.check_rate_limit("k")
    assert retry_after is not None and 0 < retry_after <= RATE_LIMIT_WINDOW

    # A key with no tier and no override is unlimited and never opens a window.
    assert all(stats.check_rate_limit("free") is None for _ in range(5))

    # The window is fixed, not sliding: it reopens once the hour has passed.
    stats.keys[key_id("k")].window_start -= RATE_LIMIT_WINDOW + 1
    assert stats.check_rate_limit("k") is None


def test_rate_limit_per_key_override_beats_tier(monkeypatch) -> None:
    monkeypatch.setitem(config.serve, "tiers", {"bot": {"rate_limit": 2}})
    monkeypatch.setitem(config.serve, "users", {"k": {"tier": "bot", "rate_limit": 5}})
    stats.keys.clear()
    assert [stats.check_rate_limit("k") for _ in range(5)] == [None] * 5
    assert stats.check_rate_limit("k") is not None


async def test_rate_limited_request_returns_429(aiohttp_client, monkeypatch) -> None:
    monkeypatch.setitem(config.serve, "dashboard", {"key": "dash-secret"})
    monkeypatch.setitem(config.serve, "tiers", {"bot": {"rate_limit": 1}})
    monkeypatch.setitem(config.serve, "users", {"tier-key": {"username": "tier1", "tier": "bot"}})
    stats.keys.clear()

    app = web.Application(
        middlewares=[cors_middleware, stats_middleware, dashboard_authentication, api_key_authentication]
    )
    app["config"] = {"users": {"tier-key": {}}}
    app["debug_api"] = False
    setup_routes(app, remote_only=True, dashboard=True)
    client = await aiohttp_client(app)

    assert (await client.get("/api/services", headers={"X-Secret-Key": "tier-key"})).status == 200
    resp = await client.get("/api/services", headers={"X-Secret-Key": "tier-key"})
    assert resp.status == 429 and int(resp.headers["Retry-After"]) > 0
    # The dashboard key leaves the middleware before the limit check, so it always answers.
    assert (await client.get("/api/dashboard/status", headers={"X-Secret-Key": "dash-secret"})).status == 200


async def test_dashboard_session_logs(aiohttp_client, dashboard_cfg) -> None:
    from unshackle.core.api.session_log import SessionLogBuffer
    from unshackle.core.api.session_store import get_session_store

    store = get_session_store()
    entry = await store.create("EXAMPLE", object())
    entry.log_buffer = SessionLogBuffer()
    entry.log_buffer.append(logging.ERROR, "login rejected: MFA required")
    assert entry.summary()["log_seq"] == 1

    client = await aiohttp_client(make_app())
    headers = {"X-Secret-Key": "dash-secret"}
    body = await (await client.get(f"/api/dashboard/sessions/{entry.session_id}/logs", headers=headers)).json()
    assert body["last_seq"] == 1
    assert body["records"][0]["message"] == "login rejected: MFA required"

    # A cursor read, so the client draining the same buffer still gets every record.
    empty = await (await client.get(f"/api/dashboard/sessions/{entry.session_id}/logs?since=1", headers=headers)).json()
    assert empty["records"] == [] and empty["last_seq"] == 1

    assert (await client.get("/api/dashboard/sessions/nope/logs", headers=headers)).status == 404
    await store.delete(entry.session_id)


async def test_dashboard_log_read_does_not_touch_the_session() -> None:
    from unshackle.core.api.session_store import SessionStore

    store = SessionStore()
    entry = await store.create("EXAMPLE", object())
    before = entry.last_accessed
    assert store.peek(entry.session_id) is entry
    assert entry.last_accessed == before
    # get() is the touching read, and is why the dashboard must not use it.
    await store.get(entry.session_id)
    assert entry.last_accessed > before


async def test_dashboard_services_states(aiohttp_client, dashboard_cfg, monkeypatch) -> None:
    from unshackle.core import services as services_module

    tags = services_module.Services.get_tags()
    if not tags:
        pytest.skip("no services installed")
    staged, failed = tags[0], tags[-1]
    monkeypatch.setattr(services_module, "PENDING", {staged})
    monkeypatch.setattr(services_module, "PENDING_SINCE", {staged: 1756908900.0})
    monkeypatch.setattr(services_module, "LOAD_ERRORS", [f"{failed}: failed to import - ImportError: boom"])

    client = await aiohttp_client(make_app())
    rows = {
        r["tag"]: r
        for r in await (await client.get("/api/dashboard/services", headers={"X-Secret-Key": "dash-secret"})).json()
    }
    assert rows[failed]["state"] == "failed" and "boom" in rows[failed]["error"]
    if staged != failed:
        assert rows[staged]["state"] == "staged" and rows[staged]["staged_since"] == 1756908900.0
        assert all(r["state"] == "loaded" for t, r in rows.items() if t not in (staged, failed))


async def test_status_reports_no_session_limit(aiohttp_client, dashboard_cfg, monkeypatch) -> None:
    monkeypatch.setitem(config.serve, "max_sessions", None)
    client = await aiohttp_client(make_app())
    body = await (await client.get("/api/dashboard/status", headers={"X-Secret-Key": "dash-secret"})).json()
    assert body["max_sessions"] is None


async def test_dashboard_health(aiohttp_client, dashboard_cfg) -> None:
    from unshackle.core.api import handlers

    handlers._health_cache.clear()
    client = await aiohttp_client(make_app())
    body = await (await client.get("/api/dashboard/health", headers={"X-Secret-Key": "dash-secret"})).json()
    assert body["status"] in {"ok", "degraded", "failing"}
    assert {"id", "label", "status", "detail", "ms"} <= set(body["checks"][0])
    assert all(c["status"] in {"ok", "warn", "fail"} for c in body["checks"])


async def test_unknown_key_on_open_route_does_not_grow_key_stats(aiohttp_client, monkeypatch) -> None:
    """/api/health answers 200 to any header, so counters keyed by the raw header are a memory leak."""
    monkeypatch.setitem(config.serve, "users", {"real": {}})
    stats.keys.clear()
    client = await aiohttp_client(make_app())
    for i in range(20):
        assert (await client.get("/api/health", headers={"X-Secret-Key": f"garbage-{i}"})).status == 200
    await client.get("/api/health", headers={"X-Secret-Key": "real"})
    assert set(stats.keys) == {"anonymous", key_id("real")}
    assert stats.keys["anonymous"].requests == 20


async def test_refresh_events_never_report_a_failed_reload_as_applied() -> None:
    from unshackle.core.api.events import publish_refresh_events

    queue: asyncio.Queue = bus.subscribe()
    try:
        publish_refresh_events(
            [
                {
                    "spec": "x",
                    "updated": True,
                    "changes": ["~GOOD", "~BAD", "~BUSY"],
                    "deferred": ["BUSY"],
                    "load_errors": ["BAD: failed to import - ImportError: boom"],
                }
            ]
        )
        events = []
        while not queue.empty():
            events.append(queue.get_nowait()["data"])
    finally:
        bus.unsubscribe(queue)
    by_action = {e["action"]: e for e in events}
    assert by_action["applied"]["tags"] == ["GOOD"]
    assert by_action["staged"]["tags"] == ["BUSY"]
    assert by_action["failed"]["tags"] == [] and "boom" in by_action["failed"]["errors"][0]


def test_health_check_masks_vault_secrets() -> None:
    from unshackle.core.api.handlers import health_check

    def probe() -> tuple[str, str]:
        raise ConnectionError("could not connect to https://user:hunter2@vault.example/api?token=hunter2")

    detail = health_check("vault:API", "vault API", probe, ["hunter2"])["detail"]
    assert "hunter2" not in detail and detail.startswith("ConnectionError")


async def test_health_vault_probe_tolerates_api_vault_rejecting_the_probe(aiohttp_server, monkeypatch) -> None:
    """An API vault answers code 3/4 to a dummy service or KID; that proves it is up, not down."""
    from unshackle.core.api.handlers import run_health_checks

    async def reject(request: web.Request) -> web.Response:
        return web.json_response({"code": 4, "message": "bad kid"})

    vault_app = web.Application()
    vault_app.router.add_get("/{tail:.*}", reject)
    server = await aiohttp_server(vault_app)
    monkeypatch.setattr(
        config, "key_vaults", [{"type": "API", "name": "t", "uri": str(server.make_url("/")), "token": "tok"}]
    )
    monkeypatch.setattr(config, "proxy_providers", {})
    checks = {c["id"]: c for c in await asyncio.to_thread(run_health_checks)}
    assert checks["vault:t"]["status"] == "ok", checks["vault:t"]


def test_health_proxy_probe_reports_a_provider_that_fails_to_build(monkeypatch) -> None:
    from unshackle.core.api.handlers import run_health_checks

    monkeypatch.setattr(config, "key_vaults", [])
    monkeypatch.setattr(config, "proxy_providers", {"nordvpn": {"username": "u"}})  # missing password
    checks = {c["id"]: c for c in run_health_checks()}
    assert checks["proxies"]["status"] == "fail" and "TypeError" in checks["proxies"]["detail"]


def test_health_proxy_probe_stays_out_of_the_log_ring(monkeypatch, caplog) -> None:
    """Rendering a provider fetches its server catalog, and the panel refreshes every 30 s."""
    from unshackle.core.api.handlers import run_health_checks

    monkeypatch.setattr(config, "key_vaults", [])
    monkeypatch.setattr(config, "proxy_providers", {"basic": {"us": "http://user:pass@host:8080"}})
    with caplog.at_level(logging.INFO, logger="proxies"):
        checks = {c["id"]: c for c in run_health_checks()}
    assert checks["proxies"]["status"] == "ok" and "Basic" in checks["proxies"]["detail"]
    assert [r.message for r in caplog.records if r.name == "proxies"] == []


async def test_health_vault_checks_are_told_apart_by_name(aiohttp_server, monkeypatch) -> None:
    """Two vaults of one type used to answer under the same id and the same label."""
    from unshackle.core.api.handlers import run_health_checks

    async def reject(request: web.Request) -> web.Response:
        return web.json_response({"code": 4, "message": "bad kid"})

    vault_app = web.Application()
    vault_app.router.add_get("/{tail:.*}", reject)
    server = await aiohttp_server(vault_app)
    uri = str(server.make_url("/"))
    monkeypatch.setattr(
        config,
        "key_vaults",
        [
            {"type": "API", "name": "primary", "uri": uri, "token": "tok"},
            {"type": "API", "name": "backup", "uri": uri, "token": "tok"},
        ],
    )
    monkeypatch.setattr(config, "proxy_providers", {})
    ids = [c["id"] for c in await asyncio.to_thread(run_health_checks) if c["id"].startswith("vault:")]
    assert ids == ["vault:primary", "vault:backup"]


def test_rate_limit_value_must_be_a_positive_whole_number() -> None:
    """key_rate_limit reads anything else as unlimited, so it has to be caught at startup."""
    from unshackle.core.api.stats import key_rate_limit, rate_limit_error

    assert rate_limit_error("serve.tiers.bot", 600) is None
    assert rate_limit_error("serve.tiers.bot", None) is None
    for bad in ("600", 600.0, 0, -1, True):
        assert rate_limit_error("serve.tiers.bot", bad), bad

    with pytest.MonkeyPatch().context() as m:
        m.setitem(config.serve, "tiers", {"bot": {"rate_limit": "600"}})
        m.setitem(config.serve, "users", {"k": {"tier": "bot"}})
        assert key_rate_limit("k") is None


async def test_dashboard_key_gets_its_own_row(aiohttp_client, dashboard_cfg) -> None:
    """configured_key counts the dashboard key, so a row must exist to attribute its polling to."""
    client = await aiohttp_client(make_app())
    headers = {"X-Secret-Key": "dash-secret"}
    await client.get("/api/dashboard/status", headers=headers)
    rows = await (await client.get("/api/dashboard/keys", headers=headers)).json()

    by_id = {r["id"]: r for r in rows}
    assert len(by_id) == len(rows)
    dash = by_id[key_id("dash-secret")]
    assert dash["role"] == "dashboard" and dash["requests"] > 0
    # null would render as "all services" on a key that reaches no service route.
    assert dash["services"] == []
    assert not any((dash["server_cdm"], dash["server_accounts"], dash["server_proxy"]))
    assert by_id[key_id("tier-key")]["role"] == "user"

    # Every counted bucket now has a row to attribute it to; only anonymous is left over.
    labels = {r["label"] for r in rows} | {"anonymous"}
    assert set(stats.requests_by_key) <= labels


async def test_dashboard_key_listed_as_a_user_appears_once(aiohttp_client, monkeypatch) -> None:
    monkeypatch.setitem(config.serve, "dashboard", {"key": "dash-secret"})
    monkeypatch.setitem(config.serve, "users", {"dash-secret": {"username": "ops", "services": ["EXAMPLE"]}})
    monkeypatch.setitem(config.serve, "api_secret", "dash-secret")
    stats.keys.clear()

    client = await aiohttp_client(make_app())
    rows = await (await client.get("/api/dashboard/keys", headers={"X-Secret-Key": "dash-secret"})).json()
    assert len(rows) == 1
    # The dashboard role wins as the most specific, and a real users entry keeps its allowlist.
    assert rows[0]["role"] == "dashboard" and rows[0]["label"] == "ops"
    assert rows[0]["services"] == ["EXAMPLE"]


async def test_admin_row_is_distinguishable(aiohttp_client, monkeypatch) -> None:
    monkeypatch.setitem(config.serve, "dashboard", {"key": "dash-secret"})
    monkeypatch.setitem(config.serve, "users", {"tier-key": {"username": "tier1"}})
    monkeypatch.setitem(config.serve, "api_secret", "master-secret")
    stats.keys.clear()

    client = await aiohttp_client(make_app())
    rows = await (await client.get("/api/dashboard/keys", headers={"X-Secret-Key": "dash-secret"})).json()
    roles = {r["role"]: r for r in rows}
    assert set(roles) == {"user", "admin", "dashboard"}
    # No serve.users entry, so the master secret keeps the implicit access the resolvers give it.
    assert roles["admin"]["services"] is None and roles["admin"]["server_cdm"] is True


def test_health_check_masks_a_pymysql_password_alias() -> None:
    """pymysql accepts passwd and ssl_key_password as well as password; all three reach the driver."""
    from unshackle.core.api.handlers import config_secrets, health_check

    def probe() -> tuple[str, str]:
        raise ConnectionError("connect failed for passwd=hunter2 key=keypass1")

    secrets = config_secrets({"type": "MySQL", "host": "db", "passwd": "hunter2", "ssl_key_password": "keypass1"})
    detail = health_check("vault:x", "vault x", probe, secrets)["detail"]
    assert "hunter2" not in detail and "keypass1" not in detail


def test_health_check_masks_a_nested_config_secret() -> None:
    """An API vault's headers and a MySQL vault's ssl are driver arguments with no fixed names."""
    from unshackle.core.api.handlers import config_secrets, health_check

    def probe() -> tuple[str, str]:
        raise ConnectionError("rejected header nested-token-1 and cert nested-pass-2")

    secrets = config_secrets(
        {"type": "API", "uri": "https://vault.example", "headers": {"X-Api-Key": "nested-token-1"}}
    ) + config_secrets({"type": "MySQL", "ssl": {"key_password": "nested-pass-2"}})
    detail = health_check("vault:x", "vault x", probe, secrets)["detail"]
    assert "nested-token-1" not in detail and "nested-pass-2" not in detail
    # Too short to mask usefully: replacing it would garble the detail without hiding anything.
    assert config_secrets({"token": "ab"}) == []


def test_health_check_detail_is_capped_after_redaction() -> None:
    """A driver can raise with a whole response body; redaction only hides what it recognises."""
    from unshackle.core.api.handlers import HEALTH_DETAIL_MAX, health_check

    def probe() -> tuple[str, str]:
        raise ValueError("API returned an invalid response: " + "hunter2 filler " * 200)

    detail = health_check("vault:x", "vault x", probe, ["hunter2"])["detail"]
    assert len(detail) == HEALTH_DETAIL_MAX + 1 and detail.endswith("…")
    # Capped after masking, so no secret survives in the part that is kept.
    assert "hunter2" not in detail


def test_health_proxy_probe_masks_provider_credentials(monkeypatch) -> None:
    """The proxy check held only by luck: no provider happens to raise with its own password."""
    from unshackle.core.api.handlers import config_secrets, run_health_checks

    providers = {
        "basic": {"us": "http://someone:proxypass1@host:8080"},
        "nordvpn": {"username": "u" * 24, "password": "nordpass1234"},
    }
    secrets = config_secrets(providers)
    # basic maps a country to a whole proxy URI, so the URI itself is the collected secret.
    assert "nordpass1234" in secrets and any("proxypass1" in s for s in secrets)

    monkeypatch.setattr(config, "key_vaults", [])
    monkeypatch.setattr(config, "proxy_providers", providers)
    detail = {c["id"]: c for c in run_health_checks()}["proxies"]["detail"]
    assert "nordpass1234" not in detail and "proxypass1" not in detail


async def test_health_probe_timeout_reports_what_finished(aiohttp_client, dashboard_cfg, monkeypatch) -> None:
    """A vault that accepts the connection then stalls must not hang the panel forever."""
    from unshackle.core.api import handlers

    def stalling_probe(checks=None):
        checks = [] if checks is None else checks
        checks.append({"id": "ffmpeg", "label": "ffmpeg", "status": "ok", "detail": "7.1", "ms": 1.0})
        time.sleep(30)  # the stalled vault; the deadline must fire long before this returns
        checks.append({"id": "never", "label": "never", "status": "ok", "detail": "", "ms": 0.0})
        return checks

    handlers._health_cache.clear()
    monkeypatch.setattr(handlers, "run_health_checks", stalling_probe)
    monkeypatch.setattr(handlers, "HEALTH_PROBE_TIMEOUT", 0.2)

    client = await aiohttp_client(make_app())
    body = await (await client.get("/api/dashboard/health", headers={"X-Secret-Key": "dash-secret"})).json()

    ids = [c["id"] for c in body["checks"]]
    assert "ffmpeg" in ids, "the probe that finished before the deadline must survive"
    assert "never" not in ids
    assert body["status"] == "failing"
    timed_out = next(c for c in body["checks"] if c["id"] == "probe")
    assert timed_out["status"] == "fail" and "timed out" in timed_out["detail"]

    # The timeout is cached for the full TTL, so callers cannot stack up more stalled threads.
    assert (await client.get("/api/dashboard/health", headers={"X-Secret-Key": "dash-secret"})).status == 200
    assert handlers._health_cache["payload"]["status"] == "failing"
