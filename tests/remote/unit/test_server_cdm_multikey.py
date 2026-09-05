"""Server-CDM licensing must survive multi-key PlayReady titles.

A PlayReady WRMHEADER can advertise fewer KIDs than the license returns (the real
per-track KID is in the init segment, not the PSSH), so the server must license
instead of short-circuiting on a vault hit for the PSSH KID alone, and the batch
path must give each track the full bundle without borrowing another PSSH's keys.
"""

import base64
import json
from types import SimpleNamespace
from uuid import UUID

import pyplayready.system.pssh as pr_pssh_mod
import pytest
import pywidevine.pssh as wv_pssh_mod

import unshackle.core.cdm as cdm_mod
import unshackle.core.cdm.detect as detect_mod
import unshackle.core.drm as drm_mod
from unshackle.core.api import handlers

pytestmark = pytest.mark.unit

REAL = UUID("161219ec3df64e0eadbb3827c0ccc46d")  # real per-track KID, only in the init segment
PSSH_KID = UUID("75982fac2cd8412a990a59c0ef2d4bb1")  # the single KID the WRMHEADER advertises
OTHER = UUID("43de497c6d1945f49acc2951696ec628")  # a sibling track's KID from the same license

PSSH_B64 = base64.b64encode(b"pssh").decode()


class _FakePRPSSH:
    def __init__(self, data):
        self.data = data


class _FakePlayReady:
    """PSSH reports one KID; the license returns the full multi-key bundle."""

    def __init__(self, pssh=None, pssh_b64=None, **kwargs):
        self.kids = [PSSH_KID]
        self.content_keys: dict = {}
        self.pssh_b64 = pssh_b64

    def get_content_keys(self, cdm, certificate, licence):
        self.content_keys = {REAL: "k_real", PSSH_KID: "k_pssh", OTHER: "k_other"}


@pytest.fixture
def playready_env(monkeypatch):
    monkeypatch.setattr(pr_pssh_mod, "PSSH", _FakePRPSSH)
    monkeypatch.setattr(drm_mod, "PlayReady", _FakePlayReady)
    monkeypatch.setattr(cdm_mod, "load_cdm", lambda *a, **k: object())
    monkeypatch.setattr(detect_mod, "is_playready_cdm", lambda cdm: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)


def test_playready_multikey_not_shortcircuited_by_vault(playready_env, monkeypatch):
    # The vault holds only the PSSH KID (a stale subset). The old code returned it and skipped
    # the license, hiding REAL and OTHER; the fix licenses and returns the whole bundle.
    monkeypatch.setattr(handlers, "check_vaults", lambda kids, name: {PSSH_KID.hex: "STALE"})
    cached: dict = {}
    monkeypatch.setattr(handlers, "cache_to_vaults", lambda keys, name: cached.update(keys))

    keys = handlers.handle_single_server_cdm(
        service=SimpleNamespace(),
        title=SimpleNamespace(),
        track=SimpleNamespace(),
        pssh_b64=PSSH_B64,
        drm_type="playready",
        request=None,
    )

    assert set(keys) == {REAL.hex, PSSH_KID.hex, OTHER.hex}
    assert keys[REAL.hex] == "k_real"
    assert cached == keys  # the full bundle is cached for other consumers


def test_widevine_still_uses_vault_shortcut(monkeypatch):
    # Widevine PSSH KIDs are authoritative, so a vault hit stays a valid shortcut (no license).
    kid = UUID("11111111111111111111111111111111")

    class _FakeWvPSSH:
        def __init__(self, b64):
            self.b64 = b64

    class _FakeWidevine:
        def __init__(self, pssh=None, **kwargs):
            self.kids = [kid]
            self.content_keys: dict = {}

        def get_content_keys(self, **kwargs):
            raise AssertionError("Widevine must not license when the vault already has every KID")

    monkeypatch.setattr(wv_pssh_mod, "PSSH", _FakeWvPSSH)
    monkeypatch.setattr(drm_mod, "Widevine", _FakeWidevine)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers, "check_vaults", lambda kids, name: {kid.hex: "vaultkey"})
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)

    keys = handlers.handle_single_server_cdm(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), PSSH_B64, "widevine", None
    )
    assert keys == {kid.hex: "vaultkey"}


async def test_batch_shares_full_bundle_per_pssh(monkeypatch):
    # Two tracks share PSSH-A (one license, both get the full bundle); a third uses PSSH-B and must
    # get PSSH-B's own keys, never PSSH-A's.
    pssh_a = base64.b64encode(b"A").decode()
    pssh_b = base64.b64encode(b"B").decode()
    track_pssh = {"vid": pssh_a, "aud": pssh_a, "vid2": pssh_b}

    tracks = {tid: SimpleNamespace(id=tid, drm=[object()]) for tid in track_pssh}
    session = SimpleNamespace(
        service_tag="EXAMPLE",
        service_instance=SimpleNamespace(),
        tracks=SimpleNamespace(get=tracks.get),
    )

    async def fake_get_session(session_id, request):
        return session

    calls: list = []
    bundle_a = {REAL.hex: "k_real", PSSH_KID.hex: "k_pssh", OTHER.hex: "k_other"}
    bundle_b = {"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": "k_b"}

    def fake_single(service, title, track, pssh_str, drm_type, request):
        calls.append(pssh_str)
        return dict(bundle_a) if pssh_str == pssh_a else dict(bundle_b)

    monkeypatch.setattr(handlers, "get_validated_session", fake_get_session)
    monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request, tag: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "find_title_for_track", lambda tid, session: SimpleNamespace())
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: "playready")
    monkeypatch.setattr(handlers, "drm_preference_name", lambda track: None)
    monkeypatch.setattr(handlers, "extract_pssh_from_track", lambda track, drm: track_pssh[track.id])
    monkeypatch.setattr(handlers, "handle_single_server_cdm", fake_single)
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)

    resp = await handlers.session_license_handler(
        {"mode": "server_cdm", "track_ids": ["vid", "aud", "vid2"], "drm_type": "playready"},
        "sess",
        None,
    )
    payload = json.loads(resp.body)
    keys = payload["keys"]

    assert calls == [pssh_a, pssh_b]  # licensed once per unique PSSH, not per track
    assert keys["vid"] == bundle_a
    assert keys["aud"] == bundle_a  # the shared-PSSH sibling reuses the full bundle
    assert keys["vid2"] == bundle_b  # the other PSSH group keeps its own keys, no cross-borrow
