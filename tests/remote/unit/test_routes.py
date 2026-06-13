"""Unit tests for unshackle.core.api.routes.setup_routes wiring + CORS + auth gating.

We build small aiohttp apps in-test with setup_routes(), mirroring what
unshackle/commands/serve.py does. We avoid hitting the real handlers by
stubbing the route table for selected paths.
"""

from __future__ import annotations

import pytest
from aiohttp import web

from unshackle.core.api.compression import compression_middleware
from unshackle.core.api.routes import cors_middleware, setup_routes

pytestmark = pytest.mark.unit


@pytest.fixture
def make_app():
    """Factory that builds an aiohttp app for tests."""

    def _factory(remote_only: bool = False, with_auth_middleware: bool = False):
        middlewares = [cors_middleware, compression_middleware]
        if with_auth_middleware:
            middlewares.insert(1, _no_key_required_auth())
        app = web.Application(middlewares=middlewares)
        app["config"] = {"users": {}}
        app["debug_api"] = False
        setup_routes(app, remote_only=remote_only)
        return app

    return _factory


def _no_key_required_auth():
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


def _collect_paths(app: web.Application) -> list[tuple[str, str]]:
    return sorted({(r.method, r.resource.canonical) for r in app.router.routes()})


def test_setup_routes_full_mode_wires_all_endpoints(make_app) -> None:
    app = make_app(remote_only=False)
    paths = _collect_paths(app)
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
        ("GET", "/api/session/{session_id}/prompt"),
        ("POST", "/api/session/{session_id}/prompt"),
        ("GET", "/api/session/{session_id}"),
        ("DELETE", "/api/session/{session_id}"),
    }
    assert expected.issubset(set(paths))


def test_setup_routes_remote_only_excludes_list_and_download(make_app) -> None:
    app = make_app(remote_only=True)
    paths = set(_collect_paths(app))
    assert ("POST", "/api/list-titles") not in paths
    assert ("POST", "/api/list-tracks") not in paths
    assert ("POST", "/api/download") not in paths
    assert ("GET", "/api/download/jobs") not in paths
    # session endpoints still present
    assert ("POST", "/api/session/create") in paths
    assert ("GET", "/api/session/{session_id}/titles") in paths
    assert ("POST", "/api/session/{session_id}/license") in paths


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

    async def _no_update(_):
        return None

    monkeypatch.setattr(routes_mod.UpdateChecker, "check_for_updates", _no_update)
    app = make_app(remote_only=True)
    client = await aiohttp_client(app)
    resp = await client.get("/api/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert "version" in body


async def test_health_bypasses_api_key_auth_middleware(make_app, aiohttp_client, monkeypatch) -> None:
    from unshackle.core.api import routes as routes_mod

    async def _no_update(_):
        return None

    monkeypatch.setattr(routes_mod.UpdateChecker, "check_for_updates", _no_update)

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
    # Auth passed; handler then 404s the session — anything other than 401 is fine here.
    assert resp.status != 401
