"""Unit tests for unshackle.core.api.routes.setup_routes wiring + CORS + auth gating.

We build small aiohttp apps in-test with setup_routes(), mirroring what
unshackle/commands/serve.py does. We avoid hitting the real handlers by
stubbing the route table for selected paths.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

from unshackle.core.api import routes
from unshackle.core.api.compression import compression_middleware
from unshackle.core.api.errors import APIError, APIErrorCode
from unshackle.core.api.routes import api_handler, cors_middleware, heartbeat, setup_routes

pytestmark = pytest.mark.unit


@pytest.fixture
def make_app():
    """Factory that builds an aiohttp app for tests."""

    def factory(remote_only: bool = False, with_auth_middleware: bool = False):
        middlewares = [cors_middleware, compression_middleware]
        if with_auth_middleware:
            middlewares.insert(1, no_key_required_auth())
        app = web.Application(middlewares=middlewares)
        app["config"] = {"users": {}}
        app["debug_api"] = False
        setup_routes(app, remote_only=remote_only)
        return app

    return factory


def no_key_required_auth():
    """Mirror serve.py's api_key_authentication middleware: required X-Secret-Key
    on every endpoint except /api/health."""

    @web.middleware
    async def mw(request, handler):
        if request.path == "/api/health":
            return await handler(request)
        secret = request.headers.get("X-Secret-Key")
        if not secret:
            return web.json_response({"status": 401, "message": "Secret Key is Empty."}, status=401)
        if secret not in request.app["config"]["users"]:
            return web.json_response({"status": 401, "message": "Secret Key is Invalid."}, status=401)
        return await handler(request)

    return mw


def collect_paths(app: web.Application) -> list[tuple[str, str]]:
    return sorted({(r.method, r.resource.canonical) for r in app.router.routes()})


def test_setup_routes_full_mode_wires_all_endpoints(make_app) -> None:
    app = make_app(remote_only=False)
    paths = collect_paths(app)
    expected = {
        ("GET", "/api/health"),
        ("GET", "/api/services"),
        ("POST", "/api/search"),
        ("POST", "/api/list-titles"),
        ("POST", "/api/list-tracks"),
        ("POST", "/api/download"),
        ("GET", "/api/download/jobs"),
        ("GET", "/api/download/jobs/{job_id}"),
        ("DELETE", "/api/download/jobs/{job_id}"),
        ("POST", "/api/session/create"),
        ("GET", "/api/session/{session_id}/titles"),
        ("POST", "/api/session/{session_id}/tracks"),
        ("POST", "/api/session/{session_id}/segments"),
        ("POST", "/api/session/{session_id}/license"),
        ("GET", "/api/session/{session_id}/logs"),
        ("GET", "/api/session/{session_id}/prompt"),
        ("POST", "/api/session/{session_id}/prompt"),
        ("GET", "/api/session/{session_id}"),
        ("DELETE", "/api/session/{session_id}"),
    }
    assert expected.issubset(set(paths))


def test_setup_routes_remote_only_excludes_list_and_download(make_app) -> None:
    app = make_app(remote_only=True)
    paths = set(collect_paths(app))
    assert ("POST", "/api/list-titles") not in paths
    assert ("POST", "/api/list-tracks") not in paths
    assert ("POST", "/api/download") not in paths
    assert ("GET", "/api/download/jobs") not in paths
    # The full remote subset is still registered after the single-table collapse.
    remote_subset = {
        ("GET", "/api/health"),
        ("GET", "/api/services"),
        ("POST", "/api/search"),
        ("POST", "/api/session/create"),
        ("GET", "/api/session/{session_id}/titles"),
        ("POST", "/api/session/{session_id}/tracks"),
        ("POST", "/api/session/{session_id}/segments"),
        ("POST", "/api/session/{session_id}/license"),
        ("GET", "/api/session/{session_id}/logs"),
        ("GET", "/api/session/{session_id}/prompt"),
        ("POST", "/api/session/{session_id}/prompt"),
        ("GET", "/api/session/{session_id}"),
        ("DELETE", "/api/session/{session_id}"),
    }
    assert remote_subset.issubset(paths)


async def test_api_handler_maps_apierror(aiohttp_client) -> None:
    """The error wrapper turns a raised APIError into the same structured response
    the per-handler except blocks produced (status + code from APIError)."""

    @api_handler
    async def boom(request: web.Request) -> web.Response:
        raise APIError(APIErrorCode.NOT_FOUND, "nope", details={"x": 1})

    app = web.Application()
    app["debug_api"] = False
    app.router.add_get("/boom", boom)
    client = await aiohttp_client(app)

    resp = await client.get("/boom")
    assert resp.status == 404  # NOT_FOUND -> 404 per errors.APIError.default_http_status
    body = await resp.json()
    assert body["status"] == "error"
    assert body["error_code"] == "NOT_FOUND"
    assert body["message"] == "nope"
    assert body["details"] == {"x": 1}


async def test_heartbeat_keeps_slow_request_alive(aiohttp_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fast route answers unchanged; a slow one streams newlines, then its body, and a late error arrives as 200."""
    monkeypatch.setattr(routes, "HEARTBEAT_INTERVAL", 0.05)

    @heartbeat
    async def fast(request: web.Request) -> web.Response:
        return web.json_response({"x": 1}, status=201)

    @heartbeat
    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(0.2)
        return web.json_response({"titles": [1]})

    @heartbeat
    async def slow_fail(request: web.Request) -> web.Response:
        await asyncio.sleep(0.2)
        raise APIError(APIErrorCode.SERVICE_ERROR, "late")

    app = web.Application()
    app["debug_api"] = False
    app.router.add_get("/fast", fast)
    app.router.add_get("/slow", slow)
    app.router.add_get("/slow_fail", slow_fail)
    client = await aiohttp_client(app)

    resp = await client.get("/fast")
    assert resp.status == 201 and await resp.json() == {"x": 1}

    resp = await client.get("/slow")
    text = await resp.text()
    assert resp.status == 200 and text.startswith("\n") and json.loads(text) == {"titles": [1]}
    assert resp.headers["X-Accel-Buffering"] == "no"

    resp = await client.get("/slow_fail")
    body = json.loads(await resp.text())
    assert resp.status == 200 and body["status"] == "error" and body["error_code"] == "SERVICE_ERROR"


async def test_heartbeat_late_error_counts_as_rejected(aiohttp_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stats count a late failure by its real status and the bytes the stream sent (with framing), not as an empty 200."""
    from unshackle.core.api.stats import stats, stats_middleware

    monkeypatch.setattr(routes, "HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(stats, "keys", {})

    @heartbeat
    async def slow_fail(request: web.Request) -> web.Response:
        await asyncio.sleep(0.2)
        raise APIError(APIErrorCode.SERVICE_ERROR, "late")

    app = web.Application(middlewares=[stats_middleware])
    app["debug_api"] = False
    app.router.add_get("/slow_fail", slow_fail)
    client = await aiohttp_client(app)

    resp = await client.get("/slow_fail")
    sent = len(await resp.read())
    (entry,) = stats.keys.values()
    assert resp.status == 200 and entry.rejected == 1 and entry.bytes_out >= sent > 0


async def test_heartbeat_client_disconnect_is_quiet(
    aiohttp_client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A client that leaves during the heartbeat logs no traceback, and the work runs to the end."""
    monkeypatch.setattr(routes, "HEARTBEAT_INTERVAL", 0.02)
    finished = asyncio.Event()

    @heartbeat
    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(0.3)
        finished.set()
        return web.json_response({})

    app = web.Application()
    app["debug_api"] = False
    app.router.add_get("/slow", slow)
    client = await aiohttp_client(app)

    resp = await client.get("/slow")
    await resp.content.read(1)
    resp.close()
    await asyncio.wait_for(finished.wait(), 2)
    await asyncio.sleep(0.1)
    assert "Error handling request" not in caplog.text


async def test_cors_preflight_returns_headers(make_app, aiohttp_client) -> None:
    app = make_app(remote_only=True)
    client = await aiohttp_client(app)
    resp = await client.options("/api/health")
    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "GET" in resp.headers["Access-Control-Allow-Methods"]
    assert "X-Secret-Key" in resp.headers["Access-Control-Allow-Headers"]


async def test_health_endpoint_responds_ok(make_app, aiohttp_client, monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.api import routes as routes_mod

    async def no_update(_):
        return None

    monkeypatch.setattr(routes_mod.UpdateChecker, "check_for_updates", no_update)
    app = make_app(remote_only=True)
    client = await aiohttp_client(app)
    resp = await client.get("/api/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert "version" in body


async def test_health_bypasses_api_key_auth_middleware(make_app, aiohttp_client, monkeypatch) -> None:
    from unshackle.core.api import routes as routes_mod

    async def no_update(_):
        return None

    monkeypatch.setattr(routes_mod.UpdateChecker, "check_for_updates", no_update)

    app = make_app(remote_only=True, with_auth_middleware=True)
    client = await aiohttp_client(app)
    resp = await client.get("/api/health")
    assert resp.status == 200  # health bypasses auth


async def test_auth_middleware_rejects_missing_key(make_app, aiohttp_client) -> None:
    app = make_app(remote_only=True, with_auth_middleware=True)
    client = await aiohttp_client(app)
    resp = await client.get("/api/session/abc")
    assert resp.status == 401
    body = await resp.json()
    assert "Secret Key" in body["message"]


async def test_auth_middleware_rejects_invalid_key(make_app, aiohttp_client) -> None:
    app = make_app(remote_only=True, with_auth_middleware=True)
    client = await aiohttp_client(app)
    resp = await client.get("/api/session/abc", headers={"X-Secret-Key": "wrong"})
    assert resp.status == 401


async def test_auth_middleware_accepts_known_key(make_app, aiohttp_client) -> None:
    app = make_app(remote_only=True, with_auth_middleware=True)
    app["config"]["users"]["good-key"] = {"devices": []}
    client = await aiohttp_client(app)
    resp = await client.get("/api/session/nonexistent", headers={"X-Secret-Key": "good-key"})
    # Auth passed; handler then 404s the session; anything other than 401 is fine here.
    assert resp.status != 401
