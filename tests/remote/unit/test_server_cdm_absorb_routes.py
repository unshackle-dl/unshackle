"""The server CDM must reach every WRM header on the routes that bypass the batch loop.

Three regressions found by review of the absorb fix: the single-track route stripped the
track's other PlayReady objects before the absorb could see them, the batch cache keyed on
the first PSSH alone and so handed one track another track's narrower key set, and a track
with no readable PSSH told the client nothing.
"""

import base64
import json
from types import SimpleNamespace
from uuid import UUID

import pyplayready.system.pssh as pr_pssh_mod
import pytest

import unshackle.core.cdm as cdm_mod
import unshackle.core.cdm.detect as detect_mod
import unshackle.core.drm as drm_mod
from unshackle.core.api import handlers

pytestmark = pytest.mark.unit

KID_A = UUID("161219ec3df64e0eadbb3827c0ccc46d")
KID_B = UUID("43de497c6d1945f49acc2951696ec628")
KID_C = UUID("75982fac2cd8412a990a59c0ef2d4bb1")


class _FakePRPSSH:
    def __init__(self, data):
        self.data = data


class _FakePlayReady:
    def __init__(self, pssh=None, pssh_b64=None, kid=None, **kwargs):
        self.kids = [kid] if kid else [KID_A]
        self.content_keys: dict = {}
        self.pssh_b64 = pssh_b64
        self.data = {"pssh_b64": pssh_b64} if pssh_b64 else {}

    def absorb(self, *others):
        for other in others:
            if other is self:
                continue
            for kid in other.kids:
                if kid not in self.kids:
                    self.kids.append(kid)

    def get_content_keys(self, cdm, certificate, licence):
        self.content_keys = {kid: f"key_{kid.hex[:4]}" for kid in self.kids}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(pr_pssh_mod, "PSSH", _FakePRPSSH)
    monkeypatch.setattr(drm_mod, "PlayReady", _FakePlayReady)
    monkeypatch.setitem(cdm_mod.__dict__, "load_cdm", lambda *a, **k: object())
    monkeypatch.setattr(detect_mod, "is_playready_cdm", lambda cdm: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers, "check_vaults", lambda kids, name: None)
    monkeypatch.setattr(handlers, "cache_to_vaults", lambda keys, name: None)
    monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request, tag: True)
    monkeypatch.setattr(handlers, "find_title_for_track", lambda tid, session: SimpleNamespace())
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)


_FakePlayReady.__name__ = "PlayReady"  # the single-track route matches on class name, not type

PSSH = base64.b64encode(b"A").decode()


async def test_single_track_path_keeps_siblings(env, monkeypatch):
    """`proxy_license` always sends `pssh`, so this route must not discard the other headers."""
    track = SimpleNamespace(
        id="vid",
        drm=[_FakePlayReady(pssh_b64=PSSH, kid=KID_A), _FakePlayReady(pssh_b64=PSSH, kid=KID_B)],
    )
    session = SimpleNamespace(
        service_tag="EX",
        service_instance=SimpleNamespace(),
        tracks=SimpleNamespace(get={"vid": track}.get),
        log_buffer=None,
    )

    async def fake_get_session(sid, req):
        return session

    monkeypatch.setattr(handlers, "get_validated_session", fake_get_session)

    resp = await handlers.session_license_handler(
        {"track_id": "vid", "mode": "server_cdm", "drm_type": "playready", "pssh": PSSH},
        "sess",
        None,
    )
    keys = json.loads(resp.body)["keys"]
    assert set(keys) == {KID_A.hex, KID_B.hex}, f"lost sibling headers: {sorted(keys)}"


async def test_batch_cache_does_not_starve_second_track(env, monkeypatch):
    """Two tracks sharing a first PSSH but holding different headers need separate licences."""
    vid = SimpleNamespace(id="vid", drm=[_FakePlayReady(pssh_b64=PSSH, kid=KID_A)])
    aud = SimpleNamespace(
        id="aud",
        drm=[
            _FakePlayReady(pssh_b64=PSSH, kid=KID_A),
            _FakePlayReady(pssh_b64=PSSH, kid=KID_B),
            _FakePlayReady(pssh_b64=PSSH, kid=KID_C),
        ],
    )
    tracks = {"vid": vid, "aud": aud}
    session = SimpleNamespace(
        service_tag="EX",
        service_instance=SimpleNamespace(),
        tracks=SimpleNamespace(get=tracks.get),
        log_buffer=None,
    )

    async def fake_get_session(sid, req):
        return session

    monkeypatch.setattr(handlers, "get_validated_session", fake_get_session)
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: "playready")
    monkeypatch.setattr(handlers, "drm_preference_name", lambda track: None)
    monkeypatch.setattr(handlers, "fetch_init_segment", lambda track, sess: None)

    resp = await handlers.session_license_handler(
        {"mode": "server_cdm", "track_ids": ["vid", "aud"], "drm_type": "playready"},
        "sess",
        None,
    )
    keys = json.loads(resp.body)["keys"]
    assert set(keys["aud"]) == {KID_A.hex, KID_B.hex, KID_C.hex}, f"audio starved: {sorted(keys['aud'])}"


async def test_no_pssh_tells_the_client_why(env, monkeypatch):
    """A track with DRM but no readable PSSH must give the client a reason, not silence."""
    from unshackle.core.api.session_log import SessionLogBuffer

    buf = SessionLogBuffer()
    track = SimpleNamespace(id="vid", drm=[object()])
    session = SimpleNamespace(
        service_tag="EX",
        service_instance=SimpleNamespace(),
        tracks=SimpleNamespace(get={"vid": track}.get),
        log_buffer=buf,
    )

    async def fake_get_session(sid, req):
        return session

    monkeypatch.setattr(handlers, "get_validated_session", fake_get_session)
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: "playready")
    monkeypatch.setattr(handlers, "drm_preference_name", lambda track: None)
    monkeypatch.setattr(handlers, "fetch_init_segment", lambda track, sess: None)
    monkeypatch.setattr(handlers, "extract_pssh_from_track", lambda track, drm: None)

    await handlers.session_license_handler(
        {"mode": "server_cdm", "track_ids": ["vid"], "drm_type": "playready"}, "sess", None
    )
    assert buf.since(0), "client gets no reason why the track produced no keys"
