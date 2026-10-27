"""A server vault key is unproven until the client decodes with it.

Over --remote with server_cdm the server never sees a segment, so the client holds a
vault-sourced key back from its own vaults, checks the decode, and reports a bad pair to
the server. The client never learns which server vault answered: the server keeps the
name on its served-key record and flags the row itself, only for a pair it served.
"""

import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError
from unshackle.core.api.session_store import SessionEntry
from unshackle.core.drm import BlowfishStripe
from unshackle.core.remote_service import RemoteService, ServerVault

pytestmark = pytest.mark.unit

KID = UUID("161219ec3df64e0eadbb3827c0ccc46d")
CDM_KID = UUID("43de497c6d1945f49acc2951696ec628")


def remote_service() -> RemoteService:
    svc = RemoteService.__new__(RemoteService)
    svc._track_alias = {}
    svc._title_alias = {}
    svc.server_vault_keys = {}
    svc.server_vault = ServerVault(svc)
    svc._session_id = "sess"
    svc.client_licensed = set()
    svc.log = SimpleNamespace(warning=lambda *a, **k: None)
    return svc


def test_client_marks_vault_keys_and_forgets_cdm_keys():
    svc = remote_service()
    svc.note_vault_keys({KID.hex: "v", CDM_KID.hex: "c"}, {KID.hex})
    assert svc.server_vault_keys == {KID: "v"}

    # a re-licence that came from the CDM clears the marker
    svc.note_vault_keys({KID.hex: "v2"}, set())
    assert svc.server_vault_keys == {}


def test_server_vault_flag_posts_only_the_pair():
    svc = remote_service()
    posted: list = []
    svc.client = SimpleNamespace(post_optional=lambda ep, data: posted.append((ep, data)) or {})
    assert svc.server_vault.report_now
    svc.server_vault.flag_bad_key("SVC", KID, "v", "server_cdm")
    assert posted == [("/api/session/sess/keys/bad", {"kid": KID.hex, "key": "v"})]


class _Vault:
    local = True

    def __init__(self):
        self.flagged: list = []

    def flag_bad_key(self, service, kid, key, source):
        self.flagged.append((service, kid, key, source))


@pytest.fixture
def session(monkeypatch):
    entry = SessionEntry(session_id="sess", service_tag="SVC", service_instance=SimpleNamespace())
    handlers.note_served_keys(entry, {KID.hex: "v", CDM_KID.hex: "c"}, {KID.hex: "sqlite"})
    assert entry.served_keys == {KID.hex: ("v", "sqlite"), CDM_KID.hex: ("c", "cdm")}

    async def validated(session_id, request):
        return entry

    monkeypatch.setattr(handlers, "get_validated_session", validated)
    monkeypatch.setattr(handlers, "require_authenticated", lambda s: None)
    vault = _Vault()
    monkeypatch.setattr(handlers, "load_server_vaults", lambda name: SimpleNamespace(vaults=[vault], service="SVC"))
    return entry, vault


async def test_server_flags_a_served_pair_with_its_own_vault_name(session, monkeypatch):
    entry, vault = session
    monkeypatch.setattr(handlers, "CACHED_PAIRS", {("SimpleNamespace", KID.hex, "v")})
    resp = await handlers.session_bad_key_handler({"kid": KID.hex, "key": "v"}, "sess")
    assert resp.status == 200
    assert vault.flagged == [("SVC", KID, "v", "sqlite")]
    assert KID.hex not in entry.served_keys
    assert not handlers.CACHED_PAIRS

    with pytest.raises(APIError):
        await handlers.session_bad_key_handler({"kid": CDM_KID.hex, "key": "nope"}, "sess")
    assert len(vault.flagged) == 1


async def test_server_does_not_flag_a_pair_its_cdm_licensed(session):
    entry, vault = session
    resp = await handlers.session_bad_key_handler({"kid": CDM_KID.hex, "key": "c"}, "sess")
    assert json.loads(resp.body) == {"flagged": False}
    assert vault.flagged == []


class _WV:
    def __init__(self, b64, kids):
        self._pssh = SimpleNamespace(dumps=lambda: b64)
        self.kids = kids


_WV.__name__ = "Widevine"


async def test_batch_reports_vault_keys_from_the_init_segment_pssh(monkeypatch):
    """A vault hit on the init-segment PSSH retry must reach `vault_keys` like a manifest one."""
    track = SimpleNamespace(id="vid", drm=[_WV("manifest", [KID])], get_key_id=lambda init: CDM_KID)
    session = SessionEntry(session_id="sess", service_tag="SVC", service_instance=SimpleNamespace())
    session.tracks = {"vid": track}

    async def validated(sid, req):
        return session

    def fake_single(service, title, track, pssh_str, drm_type, request, sources=None, refusal=None, vault_only=False):
        if pssh_str == "init":
            sources[CDM_KID.hex] = "sqlite"
            return {CDM_KID.hex: "v"}
        return {KID.hex: "c"}

    monkeypatch.setattr(handlers, "get_validated_session", validated)
    monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request, tag: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "find_title_for_track", lambda tid, session: SimpleNamespace())
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: "widevine")
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers, "fetch_init_segment", lambda track, sess: b"init")
    monkeypatch.setattr(
        handlers, "drm_from_init_segment", lambda track, session=None, init_data=None: [_WV("init", [CDM_KID])]
    )
    monkeypatch.setattr(handlers, "handle_single_server_cdm", fake_single)
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)

    resp = await handlers.session_license_handler(
        {"mode": "server_cdm", "track_ids": ["vid"], "drm_type": "widevine"}, "sess", None
    )

    payload = json.loads(resp.body)
    assert payload["keys"] == {"vid": {CDM_KID.hex: "v"}}
    assert payload["vault_keys"] == [CDM_KID.hex]
    assert session.served_keys == {CDM_KID.hex: ("v", "sqlite")}


async def test_server_tells_a_remote_vault_only_when_it_served_the_pair(monkeypatch, session):
    entry, local = session
    bystander, source = _Vault(), _Vault()
    bystander.local = source.local = False
    bystander.name, source.name = "other", "sqlite"
    monkeypatch.setattr(
        handlers, "load_server_vaults", lambda name: SimpleNamespace(vaults=[local, bystander, source], service="SVC")
    )
    await handlers.session_bad_key_handler({"kid": KID.hex, "key": "v"}, "sess")
    assert local.flagged == [("SVC", KID, "v", "sqlite")]
    assert source.flagged == [("SVC", KID, "v", "sqlite")]
    assert bystander.flagged == []


def test_resolve_server_keys_does_not_warn_for_clear_tracks(monkeypatch):
    """A requested track the server lists in clear_tracks carries no DRM, so its
    missing keys are expected and draw no warning."""
    svc = remote_service()
    warnings: list = []
    svc.log = SimpleNamespace(warning=lambda msg, *a, **k: warnings.append(msg), debug=lambda *a, **k: None)
    svc._server_cdm = True
    svc.drain_server_logs = lambda: None
    svc.client = SimpleNamespace(post=lambda ep, data: {"keys": {}, "clear_tracks": ["a-1"]})

    video_track = SimpleNamespace(id="v-1", drm=None)
    audio_track = SimpleNamespace(id="a-1", drm=None)

    class Tracks(list):
        videos = [video_track]
        audio = [audio_track]

    svc.resolve_server_keys(SimpleNamespace(tracks=Tracks([video_track, audio_track])))

    assert warnings == ["Server CDM returned no content keys for track v-1"]


def resolve(manifest_drm, drm_type="widevine"):
    """Run resolve_server_keys for one track the server answers with its own key and a sibling."""
    svc = remote_service()
    svc.log = SimpleNamespace(warning=lambda *a, **k: None, debug=lambda *a, **k: None)
    svc._server_cdm = True
    svc.drain_server_logs = lambda: None
    keys = {CDM_KID.hex: "bb" * 16, KID.hex: "aa" * 16}
    svc.client = SimpleNamespace(post=lambda ep, data: {"keys": {"v-1": keys}, "drm_type": drm_type})
    track = SimpleNamespace(id="v-1", drm=manifest_drm)

    class Tracks(list):
        videos = [track]
        audio: list = []

    svc.resolve_server_keys(SimpleNamespace(tracks=Tracks([track])))
    assert set(track.drm[0].content_keys) == {KID, CDM_KID}
    return track.drm[0]


def test_resolve_server_keys_names_only_the_manifest_kids():
    """Sibling keys in the licence stay content keys but are not KIDs the track names."""
    from pywidevine.pssh import PSSH

    from unshackle.core.drm import Widevine

    widevine = Widevine(pssh=PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[KID]))
    assert resolve([widevine]).kids == [KID]

    playready = RemoteService.create_drm_stub("playready", [KID.hex])
    assert resolve([playready], "playready").kids == [KID]


def test_resolve_server_keys_names_every_kid_without_manifest_drm():
    assert set(resolve(None).kids) == {KID, CDM_KID}


def test_reconstruct_drm_keeps_the_default_kid_of_a_pssh_without_kids():
    from pywidevine.pssh import PSSH

    from unshackle.core.drm import real_kids
    from unshackle.core.remote_service import reconstruct_drm

    content_id_only = PSSH.new(system_id=PSSH.SystemId.Widevine, init_data=b"\x22\x03abc")
    (drm,) = reconstruct_drm([{"type": "widevine", "pssh": content_id_only.dumps(), "kids": [str(KID)]}])

    assert real_kids(drm) == [KID]


def test_a_cdm_licence_unflags_a_pair_a_client_reported(monkeypatch, tmp_path):
    """The CDM returned the reported pair, so the client's decode check was wrong."""
    from unshackle.core.vaults import Vaults
    from unshackle.vaults.SQLite import SQLite

    local = SQLite("local", tmp_path / "local.db")
    vaults = Vaults("SVC")
    vaults.vaults = [local]
    vaults.flag_bad_key(KID, "ab" * 16)
    monkeypatch.setattr(handlers, "load_server_vaults", lambda service: vaults)
    monkeypatch.setattr(handlers, "CACHED_PAIRS", set())

    handlers.cache_to_vaults({KID.hex: "ab" * 16}, "SVC")
    assert not local.is_bad_key(KID, "ab" * 16)
    assert local.get_key(KID, "SVC") == "ab" * 16


async def test_a_stripe_cipher_track_is_clear_and_never_licensed(monkeypatch):
    """A track whose only DRM needs no licence goes to clear_tracks and keeps its DRM for the client."""
    track = SimpleNamespace(id="aud", drm=[BlowfishStripe(bytes(16))])
    session = SessionEntry(session_id="sess", service_tag="SVC", service_instance=SimpleNamespace())
    session.tracks = {"aud": track}

    async def validated(sid, req):
        return session

    monkeypatch.setattr(handlers, "get_validated_session", validated)
    monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request, tag: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "find_title_for_track", lambda tid, session: SimpleNamespace())
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: "widevine")
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers, "fetch_init_segment", lambda track, sess: None)
    monkeypatch.setattr(handlers, "handle_single_server_cdm", lambda *a, **k: pytest.fail("licensed a clear track"))
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)

    resp = await handlers.session_license_handler(
        {"mode": "server_cdm", "track_ids": ["aud"], "drm_type": "widevine"}, "sess", None
    )

    assert json.loads(resp.body)["clear_tracks"] == ["aud"]
    assert isinstance(track.drm[0], BlowfishStripe)
