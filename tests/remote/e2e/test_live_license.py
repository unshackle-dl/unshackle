"""E2E: license acquisition via server_cdm batch mode.

For any fixture service with ``runs_license_test: true``, this test:
  1. Creates a session for the configured target title.
  2. Picks a video track at ``license_quality`` height (default 1080).
  3. Asks the server for keys via ``mode=server_cdm`` with ``drm_type``
     equal to the configured ``license_drm`` (default ``widevine``).
  4. Asserts at least one 32-hex KID + 32-hex KEY pair is returned.

The server uses its own configured CDM (no client CDM required). Services
without ``runs_license_test`` are skipped, so this file is service-neutral.
"""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.live, pytest.mark.slow]


def _wait_titles(http_session, server_url: str, sid: str, timeout: float = 120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = http_session.get(f"{server_url}/api/session/{sid}/titles", timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 400:
            try:
                body = r.json()
            except Exception:
                body = {}
            if (body.get("details") or {}).get("auth_status") in ("authenticating", "pending_input"):
                time.sleep(2.0)
                continue
        return None
    return None


def _pick_target_title(titles, season: int = 1, episode: int = 1):
    for t in titles:
        if t.get("type") == "episode" and t.get("season") == season and t.get("number") == episode:
            return t
    return titles[0] if titles else None


def _pick_track_at_height(video_tracks, target_height: int):
    """Prefer SDR + AVC at the requested height; smallest bitrate wins."""
    same_height = [v for v in video_tracks if v.get("height") == target_height]
    preferred = [v for v in same_height if v.get("codec") == "AVC" and v.get("range") == "SDR"]
    pool = preferred or [v for v in same_height if v.get("range") == "SDR"] or same_height
    return sorted(pool, key=lambda v: v.get("bitrate") or 0)[0] if pool else None


def test_license_server_cdm(http_session, server_url: str, service_case) -> None:
    service, conf = service_case
    if not conf.get("runs_license_test"):
        pytest.skip(f"{service}: license test not enabled (runs_license_test)")

    drm_type = (conf.get("license_drm") or "widevine").lower()
    target_height = int(conf.get("license_quality") or 1080)
    title_input = conf.get("series_url") or conf.get("title_url")
    if not title_input:
        pytest.skip(f"{service}: no title/series in fixture")

    r = http_session.post(
        f"{server_url}/api/session/create",
        json={
            "service": service,
            "title_id": title_input,
            "range_": ["SDR"],
            "vcodec": ["AVC"],
            "best_available": True,
        },
        timeout=120,
    )
    if r.status_code >= 400:
        pytest.skip(f"{service}: session create failed {r.status_code}: {r.text[:200]}")
    sid = r.json()["session_id"]

    try:
        body = _wait_titles(http_session, server_url, sid)
        if not body:
            pytest.skip(f"{service}: titles timeout")
        target = _pick_target_title(
            body.get("titles") or [],
            season=conf.get("target_season", 1),
            episode=conf.get("target_episode", 1),
        )
        if not target:
            pytest.skip(f"{service}: no target title")

        tr = http_session.post(
            f"{server_url}/api/session/{sid}/tracks",
            json={"title_id": target["id"]},
            timeout=240,
        )
        assert tr.status_code == 200, tr.text
        track = _pick_track_at_height(tr.json().get("video") or [], target_height)
        if not track:
            pytest.skip(f"{service}: no track at height={target_height}")

        lic = http_session.post(
            f"{server_url}/api/session/{sid}/license",
            json={"track_ids": [track["id"]], "mode": "server_cdm", "drm_type": drm_type},
            timeout=120,
        )
        assert lic.status_code == 200, lic.text
        payload = lic.json()
        keys = payload.get("keys") or {}
        assert keys, f"no keys returned; payload={payload}"

        track_keys = keys.get(track["id"]) or keys
        assert isinstance(track_keys, dict) and track_keys, f"unexpected keys shape: {keys}"
        for kid, key in track_keys.items():
            assert len(kid) == 32, f"bad kid length: {kid}"
            assert len(key) == 32, f"bad key length: {key}"
    finally:
        http_session.delete(f"{server_url}/api/session/{sid}", timeout=30)
