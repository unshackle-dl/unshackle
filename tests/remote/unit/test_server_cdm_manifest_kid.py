"""The server CDM must keep a KID that only the manifest names, and tell the client when a key is missing.

Three regressions found by a second review of the absorb fix: the server rebuilt a
`PlayReady` from the PSSH alone, so a header whose KID lives only in the manifest (a DASH
`<kid>` or `default_KID`, an init-segment `tenc` box) raised `KIDNotFound` before the absorb
could add it; the batch cache keyed on headers only, so two tracks that shared a header but
named different KIDs got one licence; and a KID the licence did not cover was logged on the
server alone.
"""

import base64
import json
from types import SimpleNamespace
from uuid import UUID

import pyplayready.system.pssh as pr_pssh_mod
import pytest
from pyplayready.system.pssh import PSSH as RealPSSH

import unshackle.core.cdm as cdm_mod
import unshackle.core.cdm.detect as detect_mod
import unshackle.core.drm as drm_mod
from unshackle.core.api import handlers
from unshackle.core.api.session_log import SessionLogBuffer
from unshackle.core.drm import PlayReady

pytestmark = pytest.mark.unit

KID_A = UUID("161219ec3df64e0eadbb3827c0ccc46d")
KID_B = UUID("43de497c6d1945f49acc2951696ec628")


def _kidless_pro_b64() -> str:
    """A one-record PlayReady Object whose v4.2 header names no KID."""
    xml = (
        '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="4.2.0.0">'
        "<DATA><PROTECTINFO></PROTECTINFO><LA_URL>https://example.invalid/</LA_URL></DATA></WRMHEADER>"
    )
    record = xml.encode("utf-16-le")
    pro = RealPSSH.PlayreadyHeader.build(
        {"length": 10 + len(record), "records": [{"type": 1, "length": len(record), "data": record}]}
    )
    return base64.b64encode(pro).decode()


class _FakePRPSSH:
    def __init__(self, data):
        self.data = data


class _FakePlayReady:
    def __init__(self, pssh=None, pssh_b64=None, kid=None, **kwargs):
        self.kids = [kid] if kid else [KID_A]
        self.content_keys: dict = {}
        self.pssh_b64 = pssh_b64
        self.data = {"pssh_b64": pssh_b64} if pssh_b64 else {}

    @property
    def kid(self):
        return self.kids[0]

    def absorb(self, *others):
        for other in others:
            if other is self:
                continue
            for kid in other.kids:
                if kid not in self.kids:
                    self.kids.append(kid)

    def get_content_keys(self, cdm, certificate, licence):
        self.content_keys = {kid: f"key_{kid.hex[:4]}" for kid in self.kids}


_FakePlayReady.__name__ = "PlayReady"

PSSH = base64.b64encode(b"A").decode()


@pytest.fixture
def server_env(monkeypatch):
    monkeypatch.setitem(cdm_mod.__dict__, "load_cdm", lambda *a, **k: object())
    monkeypatch.setattr(detect_mod, "is_playready_cdm", lambda cdm: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers, "check_vaults", lambda kids, name: None)
    monkeypatch.setattr(handlers, "cache_to_vaults", lambda keys, name: None)
    monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request, tag: True)
    monkeypatch.setattr(handlers, "find_title_for_track", lambda tid, session: SimpleNamespace())
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: "playready")
    monkeypatch.setattr(handlers, "drm_preference_name", lambda track: None)
    monkeypatch.setattr(handlers, "fetch_init_segment", lambda track, sess: None)
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)


@pytest.fixture
def fake_playready(monkeypatch):
    monkeypatch.setattr(pr_pssh_mod, "PSSH", _FakePRPSSH)
    monkeypatch.setattr(drm_mod, "PlayReady", _FakePlayReady)


def _session(tracks: dict, buf=None):
    session = SimpleNamespace(
        service_tag="EX",
        service_instance=SimpleNamespace(),
        served_keys={},
        tracks=SimpleNamespace(get=tracks.get),
        log_buffer=buf,
    )

    async def fake_get_session(sid, req):
        return session

    return fake_get_session


def test_manifest_kid_survives_the_rebuild(server_env, monkeypatch):
    """A DASH `<kid>` reaches the local object through `kid=`; the server rebuild must not lose it."""
    monkeypatch.setattr(
        PlayReady,
        "get_content_keys",
        lambda self, cdm, certificate, licence: self.content_keys.update({k: "k" for k in self.kids}),
    )
    pro_b64 = _kidless_pro_b64()
    track = SimpleNamespace(
        id="vid", drm=[PlayReady(pssh=RealPSSH(base64.b64decode(pro_b64)), kid=KID_A, pssh_b64=pro_b64)]
    )

    keys = handlers.handle_single_server_cdm(SimpleNamespace(), SimpleNamespace(), track, pro_b64, "playready", None)

    assert set(keys) == {KID_A.hex}


async def test_batch_cache_splits_on_manifest_kid(server_env, fake_playready, monkeypatch):
    """Two tracks with one shared header but their own manifest KIDs need their own licences."""
    tracks = {
        "vid": SimpleNamespace(id="vid", drm=[_FakePlayReady(pssh_b64=PSSH, kid=KID_A)]),
        "aud": SimpleNamespace(id="aud", drm=[_FakePlayReady(pssh_b64=PSSH, kid=KID_B)]),
    }
    monkeypatch.setattr(handlers, "get_validated_session", _session(tracks))

    resp = await handlers.session_license_handler(
        {"mode": "server_cdm", "track_ids": ["vid", "aud"], "drm_type": "playready"}, "sess", None
    )

    keys = json.loads(resp.body)["keys"]
    assert KID_B.hex in keys["aud"], f"audio got the video licence: {sorted(keys['aud'])}"


async def test_uncovered_kid_reaches_the_client(server_env, fake_playready, monkeypatch):
    """The client only sees `session.log_buffer`, so an uncovered track KID must land there."""
    buf = SessionLogBuffer()
    track = SimpleNamespace(id="vid", drm=[_FakePlayReady(pssh_b64=PSSH, kid=KID_A)], get_key_id=lambda init: KID_B)
    monkeypatch.setattr(handlers, "get_validated_session", _session({"vid": track}, buf))
    monkeypatch.setattr(handlers, "fetch_init_segment", lambda track, sess: b"init")
    monkeypatch.setattr(handlers, "drm_from_init_segment", lambda track, session=None, init_data=None: [])

    await handlers.session_license_handler(
        {"mode": "server_cdm", "track_ids": ["vid"], "drm_type": "playready"}, "sess", None
    )

    assert any(KID_B.hex in r["message"] for r in buf.since(0)), "client gets no reason for the missing key"
