"""Pin the error replies of the API routes, so a change to the error layers changes no wire byte.

Each case sends one request through the middleware chain of an API-only serve. The snapshot keeps the status,
the body, the state the stats middleware reads and the ``Error ref`` log line, with the random ref and the
timestamp masked. A late case holds the service until the client has the heartbeat headers. To accept a
deliberate change, delete the snapshot file and run the test two times.
"""

from __future__ import annotations

import json
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, Optional

import pytest
from aiohttp import web

from unshackle.core.api import handlers, routes
from unshackle.core.api.compression import compression_middleware
from unshackle.core.api.errors import APIError
from unshackle.core.api.handlers import api_key_authentication, dashboard_authentication
from unshackle.core.api.routes import DASHBOARD_ROUTES, ROUTES, cors_middleware, no_5xx_middleware, setup_routes
from unshackle.core.api.session_store import get_session_store
from unshackle.core.api.stats import stats, stats_middleware
from unshackle.core.config import config
from unshackle.core.remote_protocol import (
    BAD_KEY_ROUTE,
    LICENSE_ROUTE,
    LOGS_ROUTE,
    PROMPT_ROUTE,
    SAMPLES_ROUTE,
    SEARCH_ROUTE,
    SEGMENT_FILTER_ROUTE,
    SEGMENTS_ROUTE,
    SESSION_CREATE_ROUTE,
    TITLES_ROUTE,
    TRACKS_ROUTE,
    APIErrorCode,
    AuthStatus,
)
from unshackle.core.titles.movie import Movie

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("example_service")]

SNAPSHOTS = Path(__file__).parent / "snapshots"
KEY = "test-key"
OTHER_KEY = "other-key"
DASHBOARD_KEY = "dash-key"
SESSION = "pinned-session"
RELEASE = threading.Event()

SEEN: list[dict[str, Any]] = []


def raise_fault(kind: Optional[str]) -> NoReturn:
    if kind == "api":
        raise APIError(APIErrorCode.SERVICE_ERROR, "pinned refusal", details={"pinned": True})
    if kind == "exit":
        raise SystemExit(1)
    raise RuntimeError("pinned failure")


class Faulty:
    """A stand-in whose methods all raise the fault *kind*; a *late* one first waits for RELEASE."""

    def __init__(self, kind: Optional[str] = None, late: bool = False, **attrs: Any) -> None:
        self.kind = kind
        self.late = late
        self.session = SimpleNamespace(headers={}, cookies=[], get=self.fail)
        self.__dict__.update(attrs)

    def fail(self, *args: Any, **kwargs: Any) -> NoReturn:
        if self.late:
            RELEASE.wait(5)
        raise_fault(self.kind)

    get_titles = get_tracks = get_chapters = get_widevine_license = decrypts_samples = fail
    get_pending_cdm_call = submit_response = since = fail
    last_seq = 0

    def cancel(self) -> None:
        """End of the session: nothing to unblock."""


@web.middleware
async def recorder(request: web.Request, handler: Any) -> web.StreamResponse:
    """Keep the state that the stats middleware reads from the response."""
    try:
        response = await handler(request)
    except BaseException:
        SEEN.append({"raised": True})
        raise
    SEEN.append({"error": response.get("error"), "late_status": response.get("late_status")})
    return response


def fill(route: str) -> str:
    return route.format(session_id=SESSION, job_id="no-such-job", login_id="no-such-login")


BODIES: dict[str, Any] = {
    SEARCH_ROUTE: {"service": "EXAMPLE", "query": "film"},
    SESSION_CREATE_ROUTE: {"service": "EXAMPLE", "title_id": "title-0001"},
    TRACKS_ROUTE: {"title_id": "title-0001"},
    SEGMENTS_ROUTE: {"track_ids": ["v1"]},
    SEGMENT_FILTER_ROUTE: {"track_id": "v1"},
    LICENSE_ROUTE: {"track_id": "v1", "challenge": "AAAA"},
    BAD_KEY_ROUTE: {"kid": "00" * 16, "key": "11" * 16},
    PROMPT_ROUTE: {"response": "1234"},
    "/api/list-titles": {"service": "EXAMPLE", "title_id": "title-0001"},
    "/api/list-tracks": {"service": "EXAMPLE", "title_id": "title-0001"},
    "/api/download": {"service": "EXAMPLE", "title_id": "title-0001"},
    "/api/download/jobs/{job_id}/input": {"response": "1234"},
    "/api/remote/logins/{login_id}/input": {"response": "1234"},
}
AUTHENTICATED_ROUTES = [TITLES_ROUTE, TRACKS_ROUTE, SEGMENTS_ROUTE, SEGMENT_FILTER_ROUTE, LICENSE_ROUTE, BAD_KEY_ROUTE]
SAMPLES = fill(SAMPLES_ROUTE) + "?track_id=v1&entry=1"


@dataclass
class Case:
    """One request, with the remote session it needs and the module attributes it patches."""

    method: str
    path: str
    body: Any = None
    session: Optional[Faulty] = None
    owner: str = KEY
    status: AuthStatus = AuthStatus.AUTHENTICATED
    patch: dict[str, Any] = field(default_factory=dict)
    key: Optional[str] = KEY


def generic_cases() -> dict[str, Case]:
    """A missing key on every route, a bad body on every JSON route, and the session checks on every session route."""
    cases: dict[str, Case] = {}
    for method, route, _, _ in ROUTES + DASHBOARD_ROUTES:
        path = fill(route)
        cases[f"{method} {route} no key"] = Case(method, path, key=None)
        if method == "POST" and route in BODIES:
            cases[f"{method} {route} not json"] = Case(method, path, "{not json")
            cases[f"{method} {route} not an object"] = Case(method, path, "[1]")
        if "{session_id}" in route and not route.startswith(handlers.DASHBOARD_PREFIX):
            if route == SAMPLES_ROUTE:
                path = SAMPLES
            body = BODIES.get(route) if method == "POST" else None
            cases[f"{method} {route} unknown session"] = Case(method, path, body)
            cases[f"{method} {route} wrong owner"] = Case(method, path, body, session=Faulty(), owner=OTHER_KEY)
            cases[f"{method} {route} plain before the handler's try"] = Case(
                method, path, body, patch={"get_validated_session": Faulty().fail}
            )
            if route in AUTHENTICATED_ROUTES + [SAMPLES_ROUTE]:
                cases[f"{method} {route} not authenticated"] = Case(
                    method, path, body, session=Faulty(), status=AuthStatus.AUTHENTICATING
                )
    return cases


def fault_cases() -> dict[str, Case]:
    """Each fault inside the handler of a remote session route, of search and of session create."""
    cases: dict[str, Case] = {}
    batch = {"mode": "server_cdm", "track_ids": ["v1"]}
    for kind in ("api", "plain", "exit"):
        for late in (False, True):
            when = " late" if late else ""
            heartbeat = {"routes.HEARTBEAT_INTERVAL": 0.01} if late else {}
            cases[f"titles {kind}{when}"] = Case("GET", fill(TITLES_ROUTE), session=Faulty(kind, late), patch=heartbeat)
            cases[f"tracks {kind}{when}"] = Case(
                "POST", fill(TRACKS_ROUTE), BODIES[TRACKS_ROUTE], session=Faulty(kind, late), patch=heartbeat
            )
            cases[f"license batch {kind}{when}"] = Case(
                "POST",
                fill(LICENSE_ROUTE),
                batch,
                session=Faulty(),
                patch={"fetch_init_segment": Faulty(kind, late).fail, **heartbeat},
            )
        cases[f"license proxy {kind}"] = Case("POST", fill(LICENSE_ROUTE), BODIES[LICENSE_ROUTE], session=Faulty(kind))
        cases[f"segments {kind}"] = Case(
            "POST",
            fill(SEGMENTS_ROUTE),
            {"track_ids": ["no-such-track"]} if kind == "api" else BODIES[SEGMENTS_ROUTE],
            session=Faulty(),
            patch={} if kind == "api" else {"client_session_auth": Faulty(kind).fail},
        )
        cases[f"segment filter {kind}"] = Case(
            "POST", fill(SEGMENT_FILTER_ROUTE), BODIES[SEGMENT_FILTER_ROUTE], session=Faulty(kind)
        )
        cases[f"samples {kind}"] = Case("POST", SAMPLES, session=Faulty(kind))
        cases[f"bad key {kind}"] = Case(
            "POST",
            fill(BAD_KEY_ROUTE),
            BODIES[BAD_KEY_ROUTE],
            session=Faulty(),
            patch={"load_server_vaults": Faulty(kind).fail},
        )
        cases[f"prompt get {kind}"] = Case("GET", fill(PROMPT_ROUTE), session=Faulty(input_bridge=Faulty(kind)))
        cases[f"prompt submit {kind}"] = Case(
            "POST",
            fill(PROMPT_ROUTE),
            BODIES[PROMPT_ROUTE],
            session=Faulty(input_bridge=Faulty(kind, status=AuthStatus.PENDING_INPUT)),
        )
        cases[f"logs {kind}"] = Case("GET", fill(LOGS_ROUTE), session=Faulty(log_buffer=Faulty(kind)))
        cases[f"search {kind}"] = Case(
            "POST", SEARCH_ROUTE, BODIES[SEARCH_ROUTE], patch={"run_service_search": Faulty(kind).fail}
        )
        cases[f"search remote {kind}"] = Case(
            "POST",
            SEARCH_ROUTE,
            {**BODIES[SEARCH_ROUTE], "remote": "elsewhere"},
            patch={"remote_server": Faulty(kind).fail},
        )
        cases[f"session create {kind}"] = Case(
            "POST",
            SESSION_CREATE_ROUTE,
            BODIES[SESSION_CREATE_ROUTE],
            patch={"resolve_handler_proxy": lambda *args: (None, None), "create_service_instance": Faulty(kind).fail},
        )
    cases["license unhashable track"] = Case(
        "POST", fill(LICENSE_ROUTE), {"track_id": ["v1"], "challenge": "AAAA"}, session=Faulty()
    )
    cases["segment filter unhashable track"] = Case(
        "POST", fill(SEGMENT_FILTER_ROUTE), {"track_id": ["v1"]}, session=Faulty()
    )
    for route in (SEARCH_ROUTE, SESSION_CREATE_ROUTE):
        cases[f"POST {route} plain before the handler's try"] = Case(
            "POST", route, BODIES[route], patch={"validate_service": Faulty().fail}
        )
    cases["services plain"] = Case("GET", "/api/services", patch={"routes.service_catalogue": Faulty().fail})
    cases["search unknown service"] = Case("POST", SEARCH_ROUTE, {"service": "NOPE", "query": "film"})
    cases["search no query"] = Case("POST", SEARCH_ROUTE, {"service": "EXAMPLE"})
    cases["session create unknown service"] = Case(
        "POST", SESSION_CREATE_ROUTE, {"service": "NOPE", "title_id": "title-0001"}
    )
    cases["session create no title"] = Case("POST", SESSION_CREATE_ROUTE, {"service": "EXAMPLE"})
    cases["session create proxy refused"] = Case(
        "POST", SESSION_CREATE_ROUTE, BODIES[SESSION_CREATE_ROUTE], patch={"resolve_handler_proxy": Faulty("api").fail}
    )
    cases["session create proxy failed"] = Case(
        "POST", SESSION_CREATE_ROUTE, BODIES[SESSION_CREATE_ROUTE], patch={"resolve_handler_proxy": Faulty().fail}
    )
    return cases


async def open_session(stand_in: Faulty, owner: str, status: AuthStatus) -> None:
    session = await get_session_store().create("EXAMPLE", stand_in, session_id=SESSION, owner_key=owner)
    session.auth_status = status
    session.title_map["title-0001"] = Movie(id_="title-0001", service=object, name="Film", year=2024, language=None)
    track = SimpleNamespace(
        id="v1",
        descriptor=SimpleNamespace(name="HLS"),
        url="https://example.invalid/v1.m3u8",
        drm=None,
        data={},
        OnSegmentFilter=lambda segment: False,
    )
    session.tracks["v1"] = track  # type: ignore[assignment]
    session.tracks_by_title["title-0001"] = {"v1": track}  # type: ignore[dict-item]
    session.served_keys["00" * 16] = ("11" * 16, "pinned-vault")
    session.input_bridge = getattr(stand_in, "input_bridge", None)
    session.log_buffer = getattr(stand_in, "log_buffer", None)


def masked(value: Any, ref: Optional[str]) -> Any:
    """*value* with the error ref and the timestamp replaced, at any depth.

    The longer Python 3.14 text of an unhashable key error reads as the 3.11 text.
    """
    if isinstance(value, dict):
        return {k: "TIMESTAMP" if k == "timestamp" else masked(v, ref) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v, ref) for v in value]
    if isinstance(value, str):
        value = value.replace("cannot use 'list' as a dict key (unhashable type: 'list')", "unhashable type: 'list'")
        return value.replace(ref, "REF") if ref else value
    return value


async def send(client: Any, item: Case, caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    """The masked reply to *item*."""
    SEEN.clear()
    caplog.clear()
    body = item.body
    data = body if isinstance(body, str) or body is None else json.dumps(body)
    headers = {"X-Secret-Key": item.key} if item.key else {}
    RELEASE.clear()
    resp = await client.request(item.method, item.path, data=data, headers=headers)
    RELEASE.set()
    text = await resp.text()
    try:
        reply: Any = json.loads(text)
    except ValueError:
        reply = text
    seen = SEEN[0] if SEEN else None
    error = (seen or {}).get("error") or {}
    ref = error.get("ref") or (reply.get("ref") if isinstance(reply, dict) else None)
    log_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Error ref ")]
    return masked(
        {
            "status": resp.status,
            "content_type": resp.content_type,
            "leading_newlines": len(text) - len(text.lstrip("\n")) > 0,
            "body": reply,
            "stats": seen,
            "log": log_lines,
        },
        ref,
    )


def matches_snapshot(name: str, text: str) -> None:
    path = SNAPSHOTS / name
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        pytest.fail(f"Wrote the missing snapshot {path.name}: run the test again")
    assert text == path.read_text(encoding="utf-8")


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> web.Application:
    monkeypatch.setitem(config.serve, "users", {KEY: {"server_cdm": True}, OTHER_KEY: {}})
    monkeypatch.setitem(config.serve, "dashboard", {"key": DASHBOARD_KEY})
    monkeypatch.setattr(stats, "keys", {})
    monkeypatch.setattr(stats, "errors", defaultdict(Counter))
    app = web.Application(
        middlewares=[
            cors_middleware,
            no_5xx_middleware,
            recorder,
            stats_middleware,
            dashboard_authentication,
            api_key_authentication,
            compression_middleware,
        ]
    )
    app["config"] = {"users": {KEY: {}, OTHER_KEY: {}}}
    app["debug_api"] = False
    setup_routes(app, dashboard=True)
    return app


async def test_error_replies_match_the_snapshot(
    app: web.Application, aiohttp_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    client = await aiohttp_client(app)
    store = get_session_store()
    replies = {}
    for name, item in sorted({**generic_cases(), **fault_cases()}.items()):
        with pytest.MonkeyPatch.context() as mp:
            for target, value in item.patch.items():
                module, _, attr = target.rpartition(".")
                mp.setattr(routes if module == "routes" else handlers, attr, value)
            if item.session is not None:
                await open_session(item.session, item.owner, item.status)
            try:
                replies[name] = await send(client, item, caplog)
            finally:
                await store.delete(SESSION)
    matches_snapshot("route_error_replies.json", json.dumps(replies, indent=1, sort_keys=True) + "\n")
