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
