"""E2E: track quality probing per service.

Sends a session_create with the full track-selection knob set (range_,
vcodec, best_available) so the server returns every available track.
Picks S01E01 for series, first title for movies, then asserts that the
discovered video tracks meet the expected_quality limits declared in
fixtures.yaml. If no expected block is present, the test prints what was
discovered and skips so you can copy values back into the fixture.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import pytest

pytestmark = [pytest.mark.live, pytest.mark.slow]


def _create_session(http_session, server_url: str, service: str, title_id: str) -> str:
    payload = {
        "service": service,
        "title_id": title_id,
        # Quality knobs — send enum names the server's session_create parser accepts.
        "range_": ["SDR", "HDR10", "DV"],
        "vcodec": ["AVC", "HEVC"],
        "best_available": True,
    }
    r = http_session.post(f"{server_url}/api/session/create", json=payload, timeout=120)
    if r.status_code >= 400:
        pytest.skip(f"{service}: session create failed {r.status_code}: {r.text[:200]}")
    return r.json()["session_id"]


def _wait_for_titles(http_session, server_url: str, sid: str, timeout: float = 120.0):
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
            if (body.get("details") or {}).get("auth_status") in ("authenticating", "pending_input"):
                time.sleep(2.0)
                continue
        return r.status_code, r.json() if r.text else {}
    return 408, {"message": "auth timeout"}


def _pick_target_title(titles: list[dict[str, Any]], season: int = 1, episode: int = 1) -> Optional[dict[str, Any]]:
    """Pick the configured target episode; fall back to the first title."""
    for t in titles:
        if t.get("type") == "episode" and t.get("season") == season and t.get("number") == episode:
            return t
    return titles[0] if titles else None


def _summarize(video_tracks: list[dict[str, Any]]) -> dict[str, Any]:
    max_height = max((v.get("height") or 0 for v in video_tracks), default=0)
    max_width = max((v.get("width") or 0 for v in video_tracks), default=0)
    codecs = sorted({v.get("codec") for v in video_tracks if v.get("codec")})
    ranges = sorted({v.get("range") for v in video_tracks if v.get("range")})
    bitrates = sorted({v.get("bitrate") for v in video_tracks if v.get("bitrate")})
    return {
        "track_count": len(video_tracks),
        "max_width": max_width,
        "max_height": max_height,
        "codecs": codecs,
        "ranges": ranges,
        "bitrate_min_kbps": min(bitrates) if bitrates else None,
        "bitrate_max_kbps": max(bitrates) if bitrates else None,
    }


def _resolve_title_id(conf: dict) -> Optional[str]:
    """Prefer series_url so S01E01 logic kicks in; fall back to movie title_url."""
    return conf.get("series_url") or conf.get("title_url")


def test_track_quality_meets_expected(http_session, server_url: str, service_case, capsys) -> None:
    service, conf = service_case
    title_input = _resolve_title_id(conf)
    if not title_input:
        pytest.skip(f"{service}: no title_url/series_url in fixture")

    sid = _create_session(http_session, server_url, service, title_input)
    try:
        code, body = _wait_for_titles(http_session, server_url, sid)
        if code != 200:
            pytest.skip(f"{service}: titles fetch failed {code}: {str(body)[:200]}")

        titles = body.get("titles") or []
        target = _pick_target_title(titles, season=conf.get("target_season", 1), episode=conf.get("target_episode", 1))
        if not target:
            pytest.skip(f"{service}: no titles returned")

        # Movies appear with type=='movie'; episodes with type=='episode'.
        kind = target.get("type")
        title_id = target.get("id")
        if not title_id:
            pytest.skip(f"{service}: target title has no id")

        r = http_session.post(
            f"{server_url}/api/session/{sid}/tracks",
            json={"title_id": title_id},
            timeout=300,
        )
        if r.status_code != 200:
            pytest.skip(f"{service}: tracks fetch failed {r.status_code}: {r.text[:200]}")

        tracks = r.json()
        video = tracks.get("video") or []
        summary = _summarize(video)
        # Print summary even when test passes — discovery aid.
        with capsys.disabled():
            print(f"\n[{service}] target={kind} '{target.get('name')}' -> {summary}")

        expected = conf.get("expected_quality") or {}
        if not expected:
            pytest.skip(f"{service}: no expected_quality in fixtures.yaml — discovered={summary}")

        if "min_height" in expected:
            assert summary["max_height"] >= expected["min_height"], (
                f"{service}: max height {summary['max_height']} < expected min {expected['min_height']}"
            )
        if "min_codecs" in expected:
            missing = set(expected["min_codecs"]) - set(summary["codecs"])
            assert not missing, f"{service}: missing codecs {missing}, got {summary['codecs']}"
        if "min_ranges" in expected:
            missing = set(expected["min_ranges"]) - set(summary["ranges"])
            assert not missing, f"{service}: missing ranges {missing}, got {summary['ranges']}"
        if "min_track_count" in expected:
            assert summary["track_count"] >= expected["min_track_count"], (
                f"{service}: only {summary['track_count']} video tracks, expected >= {expected['min_track_count']}"
            )
    finally:
        http_session.delete(f"{server_url}/api/session/{sid}", timeout=30)
