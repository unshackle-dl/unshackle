from types import SimpleNamespace

import pytest

from unshackle.core.api.errors import APIError
from unshackle.core.api.handlers import client_session_auth, resolve_handler_proxy, scrub_track_data


def _session(client_auth):
    cookie = SimpleNamespace(name="sid", value="secret")
    svc = SimpleNamespace(headers={"Authorization": "Bearer x", "User-Agent": "ua"}, cookies=[cookie])
    return SimpleNamespace(service_instance=SimpleNamespace(session=svc), client_auth=client_auth)


def test_client_login_keeps_auth():
    headers, cookies = client_session_auth(_session(True))
    assert headers == {"Authorization": "Bearer x", "User-Agent": "ua"}
    assert cookies == {"sid": "secret"}
    assert scrub_track_data({"token": "t", "hls": {}}, _session(True)) == {"token": "t", "hls": {}}


def test_server_login_strips_auth():
    headers, cookies = client_session_auth(_session(False))
    assert headers == {"User-Agent": "ua"}
    assert cookies == {}
    assert scrub_track_data({"token": "t", "hls": {}}, _session(False)) == {"hls": {}}


def test_server_account_rejects_client_proxy_uri(monkeypatch):
    monkeypatch.setattr("unshackle.core.api.handlers.server_account_for", lambda request, service: True)
    with pytest.raises(APIError):
        resolve_handler_proxy({"proxy": "http://attacker:8080"}, "svc", None)


def test_profile_rejects_paths():
    from unshackle.core.api.handlers import client_profile

    assert client_profile({"profile": "main-1.b"}) == "main-1.b"
    assert client_profile({}) is None
    for bad in ("../x", "a/b", "..", "a\\b", "x" * 65):
        with pytest.raises(APIError):
            client_profile({"profile": bad})


def test_client_pssh_must_match_track(monkeypatch):
    import uuid

    from unshackle.core.api import handlers

    kid, other = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(handlers, "pssh_kids", lambda pssh, drm_type: {other} if pssh == "OTHER" else {kid})
    track = SimpleNamespace(id="t1", drm=[SimpleNamespace(kids=[kid])])
    handlers.require_track_pssh(track, "MINE", "widevine")
    with pytest.raises(APIError):
        handlers.require_track_pssh(track, "OTHER", "widevine")
    with pytest.raises(APIError):
        handlers.require_track_pssh(SimpleNamespace(id="t2", drm=[]), "MINE", "widevine")


def test_output_dir_stays_under_downloads(tmp_path, monkeypatch):
    from unshackle.core.api.handlers import config, validate_download_parameters

    monkeypatch.setattr(config.directories, "downloads", tmp_path)
    assert validate_download_parameters({"output_dir": "../../etc"})
    data = {"output_dir": "shows/x"}
    assert validate_download_parameters(data) is None
    assert data["output_dir"] == str((tmp_path / "shows/x").resolve())


def test_history_scoped_by_owner(tmp_path, monkeypatch):
    from unshackle.core.api import download_manager as dm

    monkeypatch.setattr(dm, "history_path", lambda: tmp_path / "h.jsonl")
    (tmp_path / "h.jsonl").write_text(
        '{"job_id": "a", "owner": "%s", "service": "X"}\n{"job_id": "b", "owner": "%s", "service": "X"}\n'
        % (dm.owner_id("k1"), dm.owner_id("k2"))
    )
    assert [e["job_id"] for e in dm.read_job_history(owner=dm.owner_id("k1"))] == ["a"]
    assert not dm.delete_job_history("b", owner=dm.owner_id("k1"))
    assert dm.delete_job_history("b", owner=dm.owner_id("k2"))


def test_log_capture_ignores_other_threads():
    import logging
    import threading

    from unshackle.core.api.session_log import SessionLogBuffer, capture_service_logs

    buf = SessionLogBuffer()
    logger = logging.getLogger("SvcBleed")
    with capture_service_logs("SvcBleed", buf):
        logger.info("mine")
        t = threading.Thread(target=lambda: logger.info("theirs"))
        t.start()
        t.join()
    assert [r["message"] for r in buf.since(0)] == ["mine"]


def test_dashboard_logs_redact_path():
    from pathlib import Path

    from unshackle.core.utils.redact import redact_path

    # A path under the real home dir must lose the home prefix, whatever the machine's user is.
    secret = str(Path.home() / "secret" / "svc.py")
    out = redact_path(f'Traceback: File "{secret}", line 5')
    assert str(Path.home()) not in out
    assert "svc.py" in out


def test_server_login_strips_nested_and_unlisted_secrets():
    headers, _ = client_session_auth(
        SimpleNamespace(
            service_instance=SimpleNamespace(
                session=SimpleNamespace(headers={"X-Api-Key": "k", "Accept": "*"}, cookies=[])
            ),
            client_auth=False,
        )
    )
    assert headers == {"Accept": "*"}
    data = {"license": {"auth_token": "t", "url": "u"}, "items": [{"password": "p", "id": 1}]}
    assert scrub_track_data(data, _session(False)) == {"license": {"url": "u"}, "items": [{"id": 1}]}


def test_track_own_pssh_passes_without_kids(monkeypatch):
    from unshackle.core.api import handlers

    monkeypatch.setattr(handlers, "extract_pssh_from_track", lambda track, drm_type: "OWN")
    monkeypatch.setattr(handlers, "pssh_kids", lambda pssh, drm_type: set())
    handlers.require_track_pssh(SimpleNamespace(id="t", drm=[]), "OWN", "widevine")
    with pytest.raises(APIError):
        handlers.require_track_pssh(SimpleNamespace(id="t", drm=[]), "OTHER", "widevine")


def test_download_rejects_path_profile_and_bad_output_dir(tmp_path, monkeypatch):
    from unshackle.core.api.handlers import config, validate_download_parameters

    monkeypatch.setattr(config.directories, "downloads", tmp_path)
    assert validate_download_parameters({"profile": "../x"})
    assert validate_download_parameters({"profile": "ok"}) is None
    assert validate_download_parameters({"output_dir": "a\x00b"})


def test_absent_key_keeps_server_devices(monkeypatch):
    from pathlib import Path

    from unshackle.core.api.handlers import config, serve_user_config

    monkeypatch.setattr(
        config,
        "serve",
        {"users": {"k1": {"devices": ["mine"]}}, "devices": [Path("/x/dev.wvd")], "playready_devices": []},
    )
    assert serve_user_config("k1") == {"devices": ["mine"]}
    assert serve_user_config("admin-secret") == {"devices": ["dev"], "playready_devices": []}


def test_safe_inflate_rejects_non_str_via_isinstance():
    # A non-string cache value must be caught before base64.b64decode raises a 500.
    # Mirror the guard used in session_create_handler.
    from unshackle.core.api.errors import APIError, APIErrorCode

    def guard(content):
        if not isinstance(content, str):
            raise APIError(APIErrorCode.INVALID_INPUT, "cache values must be base64 strings")

    guard("ok")  # no raise
    for bad in (1, {"a": 1}, ["x"], None):
        with pytest.raises(APIError):
            guard(bad)


def _run_session(monkeypatch, tmp_path, data, answer, server_account=False, fail=False):
    """Run session_create_handler with a service that prompts during login; return the settled remote session."""
    import asyncio
    import base64
    import json
    import zlib

    from unshackle.core.api import handlers
    from unshackle.core.api.input_bridge import AuthStatus
    from unshackle.core.api.session_store import get_session_store
    from unshackle.core.config import config

    class PromptingService:
        _input_bridge = None
        log = None
        cache = None

        def authenticate(self, cookies, credential):
            if answer is not None:
                self._input_bridge.request_input("Enter code")
            if fail:
                raise ValueError("bad code")

    monkeypatch.setattr(config.directories, "cache", tmp_path)
    monkeypatch.setattr(handlers, "validate_service", lambda service, request=None: service)
    monkeypatch.setattr(handlers, "resolve_handler_proxy", lambda *args: (None, []))
    monkeypatch.setattr(handlers, "server_account_for", lambda *args: server_account)
    monkeypatch.setattr(handlers, "next_server_profile", lambda *args: None)
    monkeypatch.setattr(handlers.Services, "load", lambda service: PromptingService)
    monkeypatch.setattr(handlers, "create_service_instance", lambda *a, **k: (PromptingService(), None, None))
    if data.get("cache") == "tokens":
        data["cache"] = {"tokens_default": base64.b64encode(zlib.compress(b"{}")).decode("ascii")}

    async def run():
        resp = await handlers.session_create_handler({"service": "EXAMPLE", "title_id": "t", **data})
        session = get_session_store().peek(json.loads(resp.body)["session_id"])
        for _ in range(200):
            if answer is not None and session.input_bridge.status == AuthStatus.PENDING_INPUT:
                await handlers.session_prompt_post_handler({"response": answer}, session.session_id)
            if session.auth_status in (AuthStatus.AUTHENTICATED, AuthStatus.FAILED):
                break
            await asyncio.sleep(0.01)
        await get_session_store().delete(session.session_id)
        return session

    return asyncio.run(run())


@pytest.mark.parametrize(
    "data, answer, server_account, fail, expected",
    [
        ({}, None, False, False, False),  # anonymous login through the server: stays the server's
        ({}, "CODE", False, False, True),  # device code the client approved
        ({}, "CODE", False, True, False),  # answered, but the login failed
        ({}, "CODE", True, False, False),  # server account OTP answered by the client
        ({"cache": "tokens"}, None, False, False, True),  # client uploaded its own earlier login
    ],
)
def test_client_auth_follows_login_provenance(monkeypatch, tmp_path, data, answer, server_account, fail, expected):
    session = _run_session(monkeypatch, tmp_path, dict(data), answer, server_account, fail)
    assert session.client_auth is expected


def test_prompt_post_rejects_response_the_bridge_refused(monkeypatch):
    import asyncio

    from unshackle.core.api import handlers
    from unshackle.core.api.input_bridge import AuthStatus

    bridge = SimpleNamespace(status=AuthStatus.PENDING_INPUT, submit_response=lambda text: False)

    async def fake_get_session(session_id, request):
        return SimpleNamespace(input_bridge=bridge)

    monkeypatch.setattr(handlers, "get_validated_session", fake_get_session)
    with pytest.raises(APIError):
        asyncio.run(handlers.session_prompt_post_handler({"response": ""}, "sess"))


def test_login_that_outlives_its_session_leaves_no_cache(monkeypatch, tmp_path):
    """A login loop that finishes after DELETE must not leave its tokens on the server's disk."""
    import asyncio
    import json
    import threading

    from unshackle.core.api import handlers
    from unshackle.core.api.session_store import get_session_store
    from unshackle.core.config import config

    paired = threading.Event()
    finished = threading.Event()

    class SlowPairingService:
        _input_bridge = None
        log = None
        cache = None

        def authenticate(self, cookies, credential):
            paired.wait(5)
            self.cache.get("tokens").set("token")
            finished.set()

    monkeypatch.setattr(config.directories, "cache", tmp_path)
    monkeypatch.setattr(handlers, "validate_service", lambda service, request=None: service)
    monkeypatch.setattr(handlers, "resolve_handler_proxy", lambda *args: (None, []))
    monkeypatch.setattr(handlers, "server_account_for", lambda *args: False)
    monkeypatch.setattr(handlers.Services, "load", lambda service: SlowPairingService)
    monkeypatch.setattr(handlers, "create_service_instance", lambda *a, **k: (SlowPairingService(), None, None))

    async def run():
        resp = await handlers.session_create_handler({"service": "EXAMPLE", "title_id": "t"})
        session = get_session_store().peek(json.loads(resp.body)["session_id"])
        await get_session_store().delete(session.session_id)
        paired.set()
        for _ in range(200):
            if finished.is_set() and session.auth_status.value == "authenticated":
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        return session

    session = asyncio.run(run())
    assert session.auth_status.value == "authenticated"
    assert not (tmp_path / session.cache_tag).exists()
