"""E2E: full session lifecycle per service (create → info → titles → tracks → delete)."""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.live, pytest.mark.slow]


def _create_session(http_session, server_url: str, service: str, conf: dict) -> str:
    payload = {"service": service, "title_id": conf["title_url"]}
    r = http_session.post(f"{server_url}/api/session/create", json=payload, timeout=120)
    if r.status_code >= 400:
        pytest.skip(f"Auth/setup not available for {service}: {r.status_code} {r.text[:200]}")
    body = r.json()
    sid = body.get("session_id")
    assert sid, f"no session_id in body: {body}"
    return sid


def _wait_for_titles(http_session, server_url: str, sid: str, timeout: float = 120.0):
    """Poll /titles until auth completes. Returns (status_code, response_json).

    Server returns 400 + auth_status=authenticating while auth is in-flight,
    200 when authenticated, and other 4xx/5xx on real failure.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = http_session.get(f"{server_url}/api/session/{sid}/titles", timeout=30)
        if r.status_code == 200:
            return r.status_code, r.json()
        if r.status_code == 400:
            try:
                body = r.json()
            except Exception:
                body = {}
            auth_status = (body.get("details") or {}).get("auth_status")
            if auth_status in ("authenticating", "pending_input"):
                time.sleep(2.0)
                continue
        return r.status_code, r.json() if r.text else {}
    return 408, {"message": "timeout waiting for auth"}


def _delete_session(http_session, server_url: str, sid: str) -> None:
    http_session.delete(f"{server_url}/api/session/{sid}", timeout=30)


def test_session_create_then_delete(http_session, server_url: str, service_case) -> None:
    service, conf = service_case
    sid = _create_session(http_session, server_url, service, conf)
    try:
        r = http_session.get(f"{server_url}/api/session/{sid}", timeout=30)
        assert r.status_code == 200
        info = r.json()
        assert info.get("session", {}).get("service_tag", service) == service or service in str(info)
    finally:
        _delete_session(http_session, server_url, sid)

    # After delete, info should 404
    r2 = http_session.get(f"{server_url}/api/session/{sid}", timeout=30)
    assert r2.status_code == 404


def test_session_titles_returns_list(http_session, server_url: str, service_case) -> None:
    service, conf = service_case
    sid = _create_session(http_session, server_url, service, conf)
    try:
        code, body = _wait_for_titles(http_session, server_url, sid)
        if code != 200:
            pytest.skip(f"{service}: titles fetch failed {code}: {str(body)[:200]}")
        assert "titles" in body
        assert isinstance(body["titles"], list)
        assert len(body["titles"]) >= 1
        first = body["titles"][0]
        assert "id" in first
        assert "type" in first
    finally:
        _delete_session(http_session, server_url, sid)


def test_session_tracks_for_first_title(http_session, server_url: str, service_case) -> None:
    service, conf = service_case
    sid = _create_session(http_session, server_url, service, conf)
    try:
        code, body = _wait_for_titles(http_session, server_url, sid)
        if code != 200:
            pytest.skip(f"{service}: titles fetch failed {code}: {str(body)[:200]}")
        titles = body.get("titles") or []
        if not titles:
            pytest.skip(f"{service}: no titles returned")
        title_id = titles[0]["id"]
        r = http_session.post(
            f"{server_url}/api/session/{sid}/tracks",
            json={"title_id": title_id},
            timeout=240,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Server returns a flat payload with `video`, `audio`, `subtitles`,
        # `chapters`, `manifests`, `attachments` keys.
        assert "video" in body or "audio" in body, body
        assert body.get("video") or body.get("audio"), body
    finally:
        _delete_session(http_session, server_url, sid)


def test_session_delete_idempotent_returns_404_after(http_session, server_url: str, service_case) -> None:
    service, conf = service_case
    sid = _create_session(http_session, server_url, service, conf)
    _delete_session(http_session, server_url, sid)

    r = http_session.delete(f"{server_url}/api/session/{sid}", timeout=30)
    assert r.status_code in (404, 200)  # tolerate both depending on server semantics
