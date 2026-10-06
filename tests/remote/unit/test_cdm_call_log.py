"""The CDM call log of `unshackle serve` and its `/api/dashboard/cdm-logs` route."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from click.testing import CliRunner

from unshackle.commands.serve import serve
from unshackle.core.api.stats import SeqRing, cdm_calls, cdm_log_middleware, cdm_sessions, key_id
from unshackle.core.config import config

pytestmark = pytest.mark.unit

USER = {"X-Secret-Key": "tier-key"}
DASH = {"X-Secret-Key": "dash-secret"}
INIT_DATA = "AAAAaW5pdC1kYXRhLW1hcmtlcg=="
CHALLENGE_B64_PREFIX = "Y2hhbGxlbmdl"
LICENCE = "bGljZW5jZS1tYXJrZXI="
CONTENT_KEY = "00112233445566778899aabbccddeeff"


class FakeSession:
    def __init__(self) -> None:
        self.opened_at = time.time()


class Cdm:
    """A stand-in for both libraries' Cdm. The class name keeps the ``_Cdm__sessions`` mangling."""

    MAX_NUM_OF_SESSIONS = 16
    system_id = 1234
    security_level = 3

    def __init__(self) -> None:
        self.__sessions: dict[bytes, FakeSession] = {}

    @classmethod
    def from_device(cls, _device: Any) -> Cdm:
        return cls()

    def open(self) -> bytes:
        session_id = bytes([len(self.__sessions) + 1]) * 16
        self.__sessions[session_id] = FakeSession()
        return session_id

    def close(self, session_id: bytes) -> None:
        del self.__sessions[session_id]

    def get_service_certificate(self, session_id: bytes) -> None:
        return None

    def get_license_challenge(self, session_id: bytes, **_: Any) -> Any:
        return b"challenge-marker" * 40

    def parse_license(self, session_id: bytes, _message: Any) -> None:
        return None

    def age(self, seconds: float) -> None:
        for session in self.__sessions.values():
            session.opened_at -= seconds


class PlayReadyCdm(Cdm):
    SESSION_TIMEOUT = 30

    def get_license_challenge(self, session_id: bytes, **_: Any) -> Any:
        return "challenge-marker" * 40


@pytest.fixture
def serve_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> web.Application:
    """The app a keyed, full-mode `unshackle serve` builds, with both CDM libraries stubbed."""
    from pyplayready.remote import serve as pyplayready_serve
    from pywidevine import serve as pywidevine_serve

    (tmp_path / "chrome_l3.wvd").touch()
    (tmp_path / "sl2000.prd").touch()
    monkeypatch.setattr(config.directories, "wvds", tmp_path)
    monkeypatch.setattr(config.directories, "prds", tmp_path)
    monkeypatch.setattr(
        config,
        "serve",
        {
            "api_secret": "master-key",
            "dashboard": {"key": "dash-secret"},
            "users": {
                "tier-key": {"username": "tier1", "devices": ["chrome_l3"], "playready_devices": ["sl2000"]},
            },
        },
    )
    for module, cdm in ((pywidevine_serve, Cdm), (pyplayready_serve, PlayReadyCdm)):
        monkeypatch.setattr(module, "Cdm", cdm)
        monkeypatch.setattr(module.Device, "load", staticmethod(lambda _path: object()))
    monkeypatch.setattr(pywidevine_serve, "PSSH", lambda data: data)
    monkeypatch.setattr(pyplayready_serve, "PSSH", lambda _data: type("P", (), {"wrm_headers": ["header"]})())

    captured: dict[str, web.Application] = {}
    monkeypatch.setattr(web, "run_app", lambda app, **kwargs: captured.update(app=app))
    result = CliRunner().invoke(serve, [])
    assert result.exit_code == 0, result.output
    cdm_calls.records.clear()
    cdm_calls.seq = 0
    return captured["app"]


async def cdm_log(client: Any, since: int = 0) -> dict[str, Any]:
    resp = await client.get(f"/api/dashboard/cdm-logs?since={since}", headers=DASH)
    assert resp.status == 200
    return await resp.json()


async def test_widevine_exchange_is_recorded_per_call(aiohttp_client, serve_app) -> None:
    client = await aiohttp_client(serve_app)
    opened = await (await client.get("/chrome_l3/open", headers=USER)).json()
    session_id = opened["data"]["session_id"]
    assert (await cdm_log(client))["sessions"] == [
        {"drm": "widevine", "device": "chrome_l3", "key_id": key_id("tier-key"), "user": "tier1", "open": 1, "max": 17}
    ]

    # Accept-Encoding makes compression_middleware gzip the long challenge reply.
    challenge = await client.post(
        "/chrome_l3/get_license_challenge/STREAMING",
        headers={**USER, "Accept-Encoding": "gzip"},
        json={"session_id": session_id.upper(), "init_data": INIT_DATA},
    )
    assert challenge.status == 200, await challenge.text()
    parsed = await client.post(
        "/chrome_l3/parse_license", headers=USER, json={"session_id": session_id, "license_message": LICENCE}
    )
    assert parsed.status == 200, await parsed.text()
    assert (await client.get(f"/chrome_l3/close/{session_id}", headers=USER)).status == 200

    page = await cdm_log(client)
    records = page["records"]
    assert [r["op"] for r in records] == ["open", "get_license_challenge", "parse_license", "close"]
    assert [r["seq"] for r in records] == [1, 2, 3, 4] and page["seq"] == 4
    for record in records:
        assert record["drm"] == "widevine" and record["device"] == "chrome_l3"
        assert record["session_id"] == session_id
        assert record["user"] == "tier1" and record["key_id"] == key_id("tier-key")
        assert record["status"] == 200 and record["ok"] is True and record["error"] is None
        assert isinstance(record["ms"], float) and abs(record["ts"] - time.time()) < 60
        assert set(record) == {
            "seq", "ts", "drm", "op", "device", "key_id", "user", "session_id", "status", "ok", "error", "ms",
        }  # fmt: skip
    assert page["sessions"][0]["open"] == 0

    dump = json.dumps(page)
    for secret in ("tier-key", INIT_DATA, CHALLENGE_B64_PREFIX, "challenge-marker", LICENCE, CONTENT_KEY):
        assert secret not in dump


async def test_playready_sub_app_calls_are_tagged_playready(aiohttp_client, serve_app) -> None:
    client = await aiohttp_client(serve_app)
    opened = await (await client.get("/playready/sl2000/open", headers=USER)).json()
    session_id = opened["data"]["session_id"]
    challenge = await client.post(
        "/playready/sl2000/get_license_challenge", headers=USER, json={"session_id": session_id, "init_data": INIT_DATA}
    )
    assert challenge.status == 200, await challenge.text()

    page = await cdm_log(client)
    assert [(r["drm"], r["op"], r["device"], r["session_id"]) for r in page["records"]] == [
        ("playready", "open", "sl2000", session_id),
        ("playready", "get_license_challenge", "sl2000", session_id),
    ]
    assert page["sessions"] == [
        {"drm": "playready", "device": "sl2000", "key_id": key_id("tier-key"), "user": "tier1", "open": 1, "max": 17}
    ]
    # A session past SESSION_TIMEOUT is one the library drops on its next open.
    serve_app["playready_app"]["cdms"][("tier-key", "sl2000")].age(PlayReadyCdm.SESSION_TIMEOUT + 1)
    assert (await cdm_log(client))["sessions"][0]["open"] == 0


async def test_refusals_are_recorded_with_their_message(aiohttp_client, serve_app) -> None:
    client = await aiohttp_client(serve_app)
    assert (await client.get("/other_device/open", headers=USER)).status == 403
    assert (await client.get("/chrome_l3/open", headers={"X-Secret-Key": "wrong-key"})).status == 401
    assert (await client.get("/chrome_l3/open")).status == 401
    assert (await client.get("/playready/sl2000/open", headers={"X-Secret-Key": "wrong-key"})).status == 401
    unopened = await client.post("/chrome_l3/get_keys/CONTENT", headers=USER, json={"session_id": "AB" * 16})
    assert unopened.status == 400
    assert (await client.post("/chrome_l3/parse_license", headers=USER, json={})).status == 400

    page = await cdm_log(client)
    forbidden, invalid, empty, playready, no_cdm, no_field = page["records"]
    assert forbidden["status"] == 403 and forbidden["ok"] is False and forbidden["user"] == "tier1"
    assert forbidden["device"] == "other_device" and "not authorized" in forbidden["error"]
    assert forbidden["session_id"] is None
    assert invalid["status"] == 401 and invalid["ok"] is False and "Invalid" in invalid["error"]
    assert invalid["user"] == "anonymous" and invalid["key_id"] == "anonymous"
    assert empty["status"] == 401 and "Empty" in empty["error"] and empty["user"] == "anonymous"
    assert playready["drm"] == "playready" and playready["status"] == 401 and "Invalid" in playready["error"]
    assert no_cdm["op"] == "get_keys" and no_cdm["status"] == 400 and no_cdm["session_id"] == "ab" * 16
    assert no_field["op"] == "parse_license" and no_field["session_id"] is None and "session_id" in no_field["error"]
    assert page["sessions"] == [] and "wrong-key" not in json.dumps(page)


async def test_gzipped_refusal_is_read_and_capped(aiohttp_client, serve_app) -> None:
    """compression_middleware runs inside the log middleware, so a long refusal arrives as gzip."""
    client = await aiohttp_client(serve_app)
    device = "d" * 400
    resp = await client.get(f"/{device}/open", headers={**USER, "Accept-Encoding": "gzip"})
    assert resp.status == 403 and resp.headers["Content-Encoding"] == "gzip"
    (record,) = (await cdm_log(client))["records"]
    assert record["device"] == device[:255] and len(record["error"]) == 300
    assert record["error"].startswith("Device 'ddd")


async def test_refused_call_leaves_the_request_body_unread(aiohttp_client, serve_app) -> None:
    """A 401 is decided before the handler, so the body stays unread: no cost, and no session id, for an unknown key."""
    client = await aiohttp_client(serve_app)
    body = {"session_id": "ab" * 16, "license_message": LICENCE}
    assert (
        await client.post("/chrome_l3/parse_license", headers={"X-Secret-Key": "wrong-key"}, json=body)
    ).status == 401
    assert (await client.post("/chrome_l3/parse_license", headers=USER, json=body)).status == 400
    refused, unopened = (await cdm_log(client))["records"]
    assert refused["status"] == 401 and refused["session_id"] is None
    assert unopened["status"] == 400 and unopened["session_id"] == "ab" * 16


async def test_raised_http_exception_is_recorded_with_its_status(aiohttp_client) -> None:
    """aiohttp answers a raised ``HTTPException`` itself, so the record carries that status, not 500."""

    async def too_large(_request: web.Request) -> web.Response:
        raise web.HTTPRequestEntityTooLarge(max_size=1, actual_size=2)

    app = web.Application(middlewares=[cdm_log_middleware])
    app.router.add_post("/{device}/parse_license", too_large)
    before = cdm_calls.seq
    client = await aiohttp_client(app)
    resp = await client.post("/chrome_l3/parse_license", json={"session_id": "ab" * 16})
    (record,) = cdm_calls.since(before)
    assert resp.status == 413 and record["status"] == 413 and record["ok"] is False
    assert record["error"] == resp.reason


async def test_since_cursor_and_dashboard_key(aiohttp_client, serve_app) -> None:
    client = await aiohttp_client(serve_app)
    for _ in range(3):
        await client.get("/chrome_l3/open", headers=USER)
    assert (await client.get("/api/dashboard/cdm-logs")).status == 401
    assert (await client.get("/api/dashboard/cdm-logs", headers=USER)).status == 401
    # Only the device routes are CDM calls: not the API, not the ping, not this route.
    await client.get("/api/health")
    await client.get("/", headers=USER)

    page = await cdm_log(client, since=1)
    assert [r["seq"] for r in page["records"]] == [2, 3] and page["seq"] == 3
    assert await cdm_log(client, since=3) == {"seq": 3, "records": [], "sessions": page["sessions"]}
    resp = await client.get("/api/dashboard/cdm-logs?since=junk", headers=DASH)
    assert len((await resp.json())["records"]) == 3
    assert page["sessions"][0]["open"] == 3


async def test_handler_exception_is_recorded_without_its_text(aiohttp_client) -> None:
    """An unhandled error still answers 500, and its text (it can hold the API key) stays out."""

    async def broken(request: web.Request) -> web.Response:
        raise KeyError(request.headers["X-Secret-Key"])

    app = web.Application(middlewares=[cdm_log_middleware])
    app.router.add_get("/{device}/open", broken)
    before = cdm_calls.seq
    client = await aiohttp_client(app)
    assert (await client.get("/chrome_l3/open", headers={"X-Secret-Key": "leaky-key"})).status == 500
    (record,) = cdm_calls.since(before)
    assert record["status"] == 500 and record["ok"] is False and record["error"] == "KeyError"


async def test_recording_failure_never_fails_the_cdm_call(aiohttp_client, monkeypatch) -> None:
    async def opened(_request: web.Request) -> web.Response:
        return web.json_response({"message": "Success"})

    def refuse(_record: dict[str, Any]) -> None:
        raise RuntimeError("log is broken")

    monkeypatch.setattr(cdm_calls, "append", refuse)
    app = web.Application(middlewares=[cdm_log_middleware])
    app.router.add_get("/{device}/open", opened)
    client = await aiohttp_client(app)
    assert (await client.get("/chrome_l3/open")).status == 200


def test_call_log_keeps_the_newest_and_counts_on() -> None:
    log = SeqRing(maxlen=2)
    for op in ("open", "get_keys", "close"):
        log.append({"op": op})
    assert [(r["seq"], r["op"]) for r in log.since()] == [(2, "get_keys"), (3, "close")]
    assert log.since(2) == [{"seq": 3, "op": "close"}] and log.seq == 3


def test_sessions_snapshot_is_empty_without_a_cdm() -> None:
    assert cdm_sessions(web.Application()) == []
