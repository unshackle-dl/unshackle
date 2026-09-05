"""extract_pssh_from_track must answer for the requested DRM system only.

A loose match made the batch candidate loop a no-op: the first candidate always won, so the
loop licensed a PlayReady-only track as Widevine by accident. A Widevine device can still
license PlayReady-only content, but only as a deliberate step in the batch loop.
"""

import json
from types import SimpleNamespace

import pytest

from unshackle.core.api import handlers

pytestmark = pytest.mark.unit


class _WV:
    def __init__(self, b64):
        self._pssh = SimpleNamespace(dumps=lambda: b64)


_WV.__name__ = "Widevine"


class _PR:
    def __init__(self, b64):
        self.data = {"pssh_b64": b64}


_PR.__name__ = "PlayReady"


def test_extractor_is_type_strict():
    track = SimpleNamespace(drm=[_PR("pr")])
    assert handlers.extract_pssh_from_track(track, "playready") == "pr"
    assert handlers.extract_pssh_from_track(track, "widevine") is None

    track = SimpleNamespace(drm=[_WV("wv"), _PR("pr")])
    assert handlers.extract_pssh_from_track(track, "widevine") == "wv"
    assert handlers.extract_pssh_from_track(track, "playready") == "pr"
    assert handlers.extract_pssh_from_track(SimpleNamespace(drm=None), "widevine") is None


async def _run_batch(monkeypatch, tracks, cdm_type, calls):
    session = SimpleNamespace(
        service_tag="EXAMPLE",
        service_instance=SimpleNamespace(),
        tracks=SimpleNamespace(get=tracks.get),
    )

    async def fake_get_session(session_id, request):
        return session

    def fake_single(service, title, track, pssh_str, drm_type, request):
        calls.append((pssh_str, drm_type))
        return {"00" * 16: "key"}

    monkeypatch.setattr(handlers, "get_validated_session", fake_get_session)
    monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request, tag: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "find_title_for_track", lambda tid, session: SimpleNamespace())
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: cdm_type)
    monkeypatch.setattr(handlers, "handle_single_server_cdm", fake_single)
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)
    resp = await handlers.session_license_handler(
        {"mode": "server_cdm", "track_ids": list(tracks), "drm_type": cdm_type}, "sess", None
    )
    return json.loads(resp.body)


async def test_playready_only_track_uses_playready_when_preferred(monkeypatch):
    calls: list = []
    payload = await _run_batch(monkeypatch, {"vid": SimpleNamespace(id="vid", drm=[_PR("pr")])}, "playready", calls)
    assert calls == [("pr", "playready")]
    assert payload["drm_types"] == {"vid": "playready"}


async def test_playready_only_track_converted_for_widevine_device(monkeypatch):
    calls: list = []
    payload = await _run_batch(monkeypatch, {"vid": SimpleNamespace(id="vid", drm=[_PR("pr")])}, "widevine", calls)
    assert calls == [("pr", "widevine")]  # deliberate: Widevine.__init__ converts the PlayReady PSSH
    assert payload["drm_types"] == {"vid": "widevine"}


async def test_widevine_pssh_wins_for_widevine_device(monkeypatch):
    calls: list = []
    await _run_batch(monkeypatch, {"vid": SimpleNamespace(id="vid", drm=[_PR("pr"), _WV("wv")])}, "widevine", calls)
    assert calls == [("wv", "widevine")]
