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
from unshackle.core.remote_service import RemoteService, ServerVault

pytestmark = pytest.mark.unit

KID = UUID("161219ec3df64e0eadbb3827c0ccc46d")
CDM_KID = UUID("43de497c6d1945f49acc2951696ec628")


def remote_service() -> RemoteService:
    svc = RemoteService.__new__(RemoteService)
    svc.server_vault_keys = {}
    svc.server_vault = ServerVault(svc)
    svc._session_id = "sess"
    svc.log = SimpleNamespace(warning=lambda *a, **k: None)
    return svc


def test_client_marks_vault_keys_and_forgets_cdm_keys():
    svc = remote_service()
    svc.note_vault_keys({KID.hex: "v", CDM_KID.hex: "c"}, {KID.hex})
    assert svc.server_vault_keys == {KID: "v"}

    # a re-licence that came from the CDM clears the marker
    svc.note_vault_keys({KID.hex: "v2"}, set())
    assert svc.server_vault_keys == {}


def test_report_bad_posts_only_the_pair():
    svc = remote_service()
    posted: list = []
    svc.client = SimpleNamespace(post_optional=lambda ep, data: posted.append((ep, data)) or {})
    svc.server_vault.report_bad(KID, "v")
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


async def test_server_flags_a_served_pair_with_its_own_vault_name(session):
    entry, vault = session
    resp = await handlers.session_bad_key_handler({"kid": KID.hex, "key": "v"}, "sess")
    assert resp.status == 200
    assert vault.flagged == [("SVC", KID, "v", "sqlite")]
    assert KID.hex not in entry.served_keys

    with pytest.raises(APIError):
        await handlers.session_bad_key_handler({"kid": CDM_KID.hex, "key": "nope"}, "sess")
    assert len(vault.flagged) == 1


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

    def fake_single(service, title, track, pssh_str, drm_type, request, sources=None):
        if pssh_str == "init":
            sources[CDM_KID.hex] = "sqlite"
            return {CDM_KID.hex: "v"}
        return {KID.hex: "c"}

    monkeypatch.setattr(handlers, "get_validated_session", validated)
    monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request, tag: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "find_title_for_track", lambda tid, session: SimpleNamespace())
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: "widevine")
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
