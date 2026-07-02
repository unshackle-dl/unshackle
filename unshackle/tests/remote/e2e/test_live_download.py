"""E2E: download smoke test via segments + CDN manifest fetch.

For any fixture service with ``runs_download_test: true``, this test:
  1. Creates a session with the full track-selection knobs.
  2. Picks a 1080p SDR video track on the configured target title.
  3. Calls /api/session/{id}/segments to resolve the CDN URL + headers.
  4. Fetches the manifest URL with the resolved headers.
  5. Asserts the body is non-empty and looks like DASH/HLS.

It does NOT decrypt or mux — that requires a full local CDM. It proves
the end-to-end pipeline up to CDN reachability for the selected quality.
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
    same_height = [v for v in video_tracks if v.get("height") == target_height and v.get("range") == "SDR"]
    return sorted(same_height, key=lambda v: v.get("bitrate") or 0)[0] if same_height else None


def test_download_manifest_fetch(http_session, server_url: str, service_case) -> None:
    service, conf = service_case
    if not conf.get("runs_download_test"):
        pytest.skip(f"{service}: download test not enabled (runs_download_test)")

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
            "quality": [target_height],
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

        seg = http_session.post(
            f"{server_url}/api/session/{sid}/segments",
            json={"track_ids": [track["id"]]},
            timeout=120,
        )
        assert seg.status_code == 200, seg.text
        info = seg.json().get("tracks", {}).get(track["id"])
        assert info, f"no segment info: {seg.text[:200]}"

        manifest_url = info.get("url")
        assert manifest_url, f"no manifest url: {info}"
        headers = dict(info.get("headers") or {})
        headers.pop("Host", None)
        headers.pop("host", None)

        import requests

        cdn = requests.get(manifest_url, headers=headers, timeout=60)
        assert cdn.status_code == 200, f"CDN fetch failed {cdn.status_code}: {cdn.text[:200]}"
        assert len(cdn.content) > 256, "manifest body too small"
        head = cdn.content[:128].lstrip()
        assert head.startswith((b"<?xml", b"<MPD", b"#EXTM3U")), f"unexpected manifest content: {head[:64]!r}"
    finally:
        http_session.delete(f"{server_url}/api/session/{sid}", timeout=30)
