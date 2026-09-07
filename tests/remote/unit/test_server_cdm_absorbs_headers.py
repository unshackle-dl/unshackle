"""The server CDM must license every WRM header a track carries, not only the first.

A track can hold one `PlayReady` object per header: `ISM` shares a Protection list, `HLS`
emits one per `EXT-X-KEY`. `extract_pssh_from_track` returns the first PSSH only, so the
server folds in the siblings. Without that it returns fewer content keys than a local CDM.
"""

import base64
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


class _FakePRPSSH:
    def __init__(self, data):
        self.data = data


class _FakePlayReady:
    """Only the headers this object knows about yield keys."""

    def __init__(self, pssh=None, pssh_b64=None, kid=None, **kwargs):
        self.kids = [kid] if kid else [KID_A]
        self.content_keys: dict = {}
        self.pssh_b64 = pssh_b64
        self.absorbed: list = []

    def absorb(self, *others):
        for other in others:
            if other is self:
                continue
            self.absorbed.append(other)
            for kid in other.kids:
                if kid not in self.kids:
                    self.kids.append(kid)

    def get_content_keys(self, cdm, certificate, licence):
        self.content_keys = {kid: f"key_{kid.hex[:4]}" for kid in self.kids}


@pytest.fixture
def playready_env(monkeypatch):
    monkeypatch.setattr(pr_pssh_mod, "PSSH", _FakePRPSSH)
    monkeypatch.setattr(drm_mod, "PlayReady", _FakePlayReady)
    monkeypatch.setitem(cdm_mod.__dict__, "load_cdm", lambda *a, **k: object())
    monkeypatch.setattr(detect_mod, "is_playready_cdm", lambda cdm: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers, "check_vaults", lambda kids, name: None)
    monkeypatch.setattr(handlers, "cache_to_vaults", lambda keys, name: None)
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)


def _keys_for(track):
    return handlers.handle_single_server_cdm(
        service=SimpleNamespace(),
        title=SimpleNamespace(),
        track=track,
        pssh_b64=base64.b64encode(b"A").decode(),
        drm_type="playready",
        request=None,
    )


def test_sibling_headers_are_licensed(playready_env):
    track = SimpleNamespace(drm=[_FakePlayReady(kid=KID_A), _FakePlayReady(kid=KID_B)])
    assert set(_keys_for(track)) == {KID_A.hex, KID_B.hex}


def test_single_header_track_is_untouched(playready_env):
    track = SimpleNamespace(drm=[_FakePlayReady(kid=KID_A)])
    assert set(_keys_for(track)) == {KID_A.hex}
