"""A server vault key is unproven until the client decodes with it.

Over --remote with server_cdm the server never sees a segment, so the client holds a
vault-sourced key back from its own vaults, checks the decode, and reports a bad pair to
the server. The client never learns which server vault answered: the server keeps the
name on its served-key record and flags the row itself, only for a pair it served.
"""

import json
from types import SimpleNamespace
from uuid import UUID

import click
import pytest

from tests.core.key_resolver import vaults_of
from tests.remote.key_source import one_title, remote_keys
from tests.remote.sessions import session_entry
from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError
from unshackle.core.drm import BlowfishStripe, ClearKeyCTR
from unshackle.core.drm.cenc import drm_stub
from unshackle.core.remote_service import RemoteError, RemoteService

pytestmark = pytest.mark.unit

KID = UUID("161219ec3df64e0eadbb3827c0ccc46d")
CDM_KID = UUID("43de497c6d1945f49acc2951696ec628")
KEYED_CIPHER = ClearKeyCTR(None, bytes(range(16, 32)), b"OggS", key=bytes(16))


def remote_service() -> RemoteService:
    svc = remote_keys(server_cdm=None)
    svc._track_alias = {}
    svc._title_alias = {}
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


def test_server_vault_flag_posts_only_the_pair():
    svc = remote_service()
    posted: list = []
    svc.client = SimpleNamespace(post_optional=lambda ep, data: posted.append((ep, data)) or {})
    assert svc.server_vault.report_now
    svc.server_vault.flag_bad_key("SVC", KID, "v", "server_cdm")
    assert posted == [("/api/session/sess/keys/bad", {"kid": KID.hex, "key": "v"})]


class _Vault:
    local = True
    name = "local"

    def __init__(self):
        self.flagged: list = []

    def flag_bad_key(self, service, kid, key, source):
        self.flagged.append((service, kid, key, source))


@pytest.fixture
def session(monkeypatch):
    entry = session_entry(service_tag="SVC")
    handlers.note_served_keys(entry, {KID.hex: "v", CDM_KID.hex: "c"}, {KID.hex: "sqlite"})
    assert entry.served_keys == {KID.hex: ("v", "sqlite"), CDM_KID.hex: ("c", "cdm")}

    async def validated(session_id, request):
        return entry

    monkeypatch.setattr(handlers, "get_validated_session", validated)
    monkeypatch.setattr(handlers, "require_authenticated", lambda s: None)
    vault = _Vault()
    monkeypatch.setattr(handlers, "load_server_vaults", lambda name: vaults_of(vault, service="SVC"))
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
        self.pssh = SimpleNamespace(dumps=lambda: b64)
        self.kids = kids


_WV.__name__ = "Widevine"


async def test_batch_reports_vault_keys_from_the_init_segment_pssh(monkeypatch):
    """A vault hit on the init-segment PSSH retry must reach `vault_keys` like a manifest one."""
    track = SimpleNamespace(id="vid", drm=[_WV("manifest", [KID])], get_key_id=lambda init: CDM_KID)
    session = session_entry(service_tag="SVC", tracks={"vid": track})

    probed: list = []

    async def validated(sid, req):
        return session

    def fake_single(
        service,
        title,
        track,
        pssh_str,
        drm_type,
        request,
        sources=None,
        refusal=None,
        vault_only=False,
        shared=None,
        track_kid=None,
        **_,
    ):
        probed.append(track_kid)
        if pssh_str == "init":
            sources[CDM_KID.hex] = "sqlite"
            return {CDM_KID.hex: "v"}
        return {KID.hex: "c"}

    monkeypatch.setattr(handlers, "get_validated_session", validated)
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
    assert probed == [CDM_KID, CDM_KID]


async def test_batch_asks_again_when_a_cached_vault_answer_lacks_the_track_kid(monkeypatch):
    """Renditions that share a header: a vault answer for one track's KID does not serve a track with another KID."""
    kids = {"a": KID, "b": CDM_KID, "c": CDM_KID}
    session = session_entry(
        service_tag="SVC",
        tracks={
            tid: SimpleNamespace(id=tid, drm=[_WV("shared", [KID, CDM_KID])], get_key_id=lambda init, k=kid: k)
            for tid, kid in kids.items()
        },
    )
    asked: list = []

    async def validated(sid, req):
        return session

    def fake_single(
        service, title, track, pssh_str, drm_type, request, sources=None, refusal=None, track_kid=None, **_
    ):
        asked.append(track_kid)
        if track_kid == KID:
            sources[KID.hex] = "sqlite"
            return {KID.hex: "v"}
        return {KID.hex: "v", CDM_KID.hex: "c"}

    monkeypatch.setattr(handlers, "get_validated_session", validated)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "find_title_for_track", lambda tid, session: SimpleNamespace())
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: "widevine")
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers, "fetch_init_segment", lambda track, sess: b"init")
    monkeypatch.setattr(handlers, "handle_single_server_cdm", fake_single)
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)

    resp = await handlers.session_license_handler(
        {"mode": "server_cdm", "track_ids": list(kids), "drm_type": "widevine"}, "sess", None
    )

    payload = json.loads(resp.body)
    assert asked == [KID, CDM_KID]
    assert payload["keys"]["c"] == {KID.hex: "v", CDM_KID.hex: "c"}


@pytest.mark.parametrize(
    ("drm", "head"),
    [(["drm"], b"head"), ([], None), ([BlowfishStripe(bytes(16))], None), ([KEYED_CIPHER], None)],
    ids=["with-drm", "clear", "stripe", "ctr"],
)
def test_the_server_reads_the_head_of_a_one_file_track_with_drm(drm, head):
    from unshackle.core.tracks import Video

    fetched: list = []
    track = SimpleNamespace(
        id="v",
        data={},
        descriptor=Video.Descriptor.URL,
        drm=drm,
        get_init_segment=lambda session: fetched.append(session) or b"head",
    )

    assert (handlers.fetch_init_segment(track, "svc-session"), fetched) == (
        (head, ["svc-session"]) if head else (None, [])
    )


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

    assert warnings == ["Server CDM returned no content keys for track simplenamespace (v-1)"]


def test_resolve_server_keys_takes_the_key_of_a_whole_file_cipher():
    """The server licensed the track's whole-file cipher: the client takes its key and keeps the cipher."""
    svc = remote_service()
    svc.log = SimpleNamespace(warning=lambda msg, *a, **k: pytest.fail(msg), debug=lambda *a, **k: None)
    svc._server_cdm = True
    svc.drain_server_logs = lambda: None
    svc.client = SimpleNamespace(post=lambda ep, data: {"keys": {}, "file_keys": {"v-1": "aa" * 16}})
    cipher = ClearKeyCTR(None, bytes(range(16)), b"OggS")
    track = SimpleNamespace(id="v-1", drm=[cipher])

    svc.resolve_server_keys(one_title(track))

    assert track.drm == [cipher] and cipher.key == bytes.fromhex("aa" * 16)


def test_license_file_asks_the_server_for_the_key_of_a_whole_file_cipher():
    """A whole-file cipher can only be licensed by the service on the server."""
    svc = remote_service()
    svc.log = SimpleNamespace(warning=lambda *a, **k: None, debug=lambda *a, **k: None)
    svc._server_cdm = True
    sent = []
    svc.client = SimpleNamespace(post=lambda ep, data, **kw: sent.append(data) or {"file_keys": {"v-1": "bb" * 16}})
    cipher = ClearKeyCTR(None, bytes(range(16)), b"OggS")
    track = SimpleNamespace(id="v-1", drm=[cipher])

    assert svc.license_file(track) == "bb" * 16

    assert sent == [{"mode": "server_cdm", "drm_type": "clearkeyctr", "track_id": "v-1"}]


@pytest.mark.parametrize(
    ("grants", "answer", "message"),
    [
        ({"_server_cdm": False}, None, "needs a server licence"),
        ({"_local_vaults_only": True}, None, "needs a server licence"),
        ({}, {"keys": {}}, "sent no content key"),
        ({}, RemoteError("Content key tool not found: tool", "SERVICE_ERROR"), "Content key tool not found"),
    ],
    ids=["no-grant", "server-device", "no-key", "refused"],
)
def test_a_cipher_the_server_does_not_license_raises(grants, answer, message):
    """A whole-file cipher the server gives no key for stops the track with one DRM error before its download."""
    svc = remote_service()
    svc._server_cdm = True
    for name, value in grants.items():
        setattr(svc, name, value)

    def post(ep, data, **kw):
        if isinstance(answer, BaseException):
            raise answer
        return answer if answer is not None else pytest.fail("asked the server")

    svc.client = SimpleNamespace(post=post)
    cipher = ClearKeyCTR(None, bytes(range(16)), b"OggS")

    with pytest.raises(click.ClickException, match=message) as stopped:
        svc.license_file(SimpleNamespace(id="v-1", drm=[cipher]))

    assert stopped.value.error_code == "DRM_ERROR"


def test_track_url_asks_the_segments_route_for_the_url_of_one_track():
    """A track the server sent with resolve_url gets its URL from the server when it is downloaded."""
    svc = remote_service()
    svc.drain_server_logs = lambda: None
    sent = []
    svc.client = SimpleNamespace(
        post=lambda ep, data, **kw: sent.append((ep, data)) or {"tracks": {"a-1": {"url": "https://cdn.example/a"}}}
    )

    assert svc.track_url(SimpleNamespace(id="a-1")) == "https://cdn.example/a"

    assert [data for _, data in sent] == [{"track_ids": ["a-1"]}]
    assert sent[0][0].endswith("/segments")


@pytest.mark.parametrize(
    "answer",
    [
        {"tracks": {"a-1": {"url": None}}},
        {"tracks": {}},
        RemoteError("Track not found in session: a-1", "TRACK_NOT_FOUND"),
    ],
    ids=["no-url", "no-track", "refused"],
)
def test_a_track_the_server_gives_no_url_raises(answer):
    """A track with no URL from the server stops at its download with one line."""
    svc = remote_service()
    svc.drain_server_logs = lambda: None

    def post(ep, data, **kw):
        if isinstance(answer, BaseException):
            raise answer
        return answer

    svc.client = SimpleNamespace(post=post)

    with pytest.raises(click.ClickException, match="no URL for track a-1|Track not found"):
        svc.track_url(SimpleNamespace(id="a-1"))


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

    playready = drm_stub("playready", [KID.hex])
    assert resolve([playready], "playready").kids == [KID]


def test_resolve_server_keys_names_every_kid_without_manifest_drm():
    assert set(resolve(None).kids) == {KID, CDM_KID}


def test_reconstruct_drm_keeps_the_default_kid_of_a_pssh_without_kids():
    from pywidevine.pssh import PSSH

    from unshackle.core.drm import real_kids
    from unshackle.core.wire import reconstruct_drm

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


async def batch_answer(monkeypatch, track, vault_only=False, request=None, server_device=False):
    """The server's licence answer for one track, with every CDM call stubbed out."""
    session = session_entry(service_tag="SVC", tracks={"aud": track})
    session.server_cdm, session.server_vault = not vault_only, vault_only
    session.server_device = server_device

    async def validated(sid, req):
        return session

    monkeypatch.setattr(handlers, "get_validated_session", validated)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "find_title_for_track", lambda tid, session: SimpleNamespace())
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: "widevine")
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers, "fetch_init_segment", lambda track, sess: None)
    monkeypatch.setattr(handlers, "handle_single_server_cdm", lambda *a, **k: pytest.fail("licensed a clear track"))
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)
    resp = await handlers.session_license_handler(
        request or {"mode": "server_cdm", "track_ids": ["aud"], "drm_type": "widevine"}, "sess", None
    )
    return json.loads(resp.body)


async def test_a_stripe_cipher_that_holds_its_key_is_never_licensed(monkeypatch):
    """A track whose only DRM is a stripe cipher that holds its key is named as clear and licensed by nothing."""
    cipher = BlowfishStripe(bytes(16))
    track = SimpleNamespace(id="aud", drm=[cipher])

    body = await batch_answer(monkeypatch, track)

    assert body == {"keys": {}, "clear_tracks": ["aud"]}
    assert track.drm[0] is cipher


async def test_the_server_batch_licenses_a_cipher_that_holds_no_key(monkeypatch):
    """A track whose whole-file cipher holds no key yet is licensed on the server and answered under file_keys."""
    cipher = ClearKeyCTR(lambda: "aa" * 16, bytes(range(16)), b"OggS")
    track = SimpleNamespace(id="aud", drm=[cipher])

    body = await batch_answer(monkeypatch, track)

    assert (body["keys"], body["file_keys"], "clear_tracks" in body) == ({}, {"aud": "aa" * 16}, False)
    assert cipher.key == bytes.fromhex("aa" * 16)


async def test_the_server_batch_answers_a_cipher_that_already_holds_its_key(monkeypatch):
    """A track the server licensed before answers its key again and licenses nothing."""
    cipher = ClearKeyCTR(lambda: pytest.fail("licensed a second time"), bytes(range(16)), b"OggS")
    cipher.take_key("dd" * 16)
    track = SimpleNamespace(id="aud", drm=[cipher])

    body = await batch_answer(monkeypatch, track)

    assert (body["file_keys"], "clear_tracks" in body) == ({"aud": "dd" * 16}, False)


@pytest.mark.parametrize("refusal", ["vault_only", "server_device"])
async def test_a_session_without_live_licences_licenses_no_cipher(monkeypatch, refusal):
    """A session the server makes no live licence for gets no key of a whole-file cipher and no clear mark."""
    cipher = ClearKeyCTR(lambda: pytest.fail("licensed a cipher"), bytes(range(16)), b"OggS")
    track = SimpleNamespace(id="aud", drm=[cipher])

    body = await batch_answer(monkeypatch, track, **{refusal: True})

    assert body == {"keys": {}}


@pytest.mark.parametrize("refusal", ["vault_only", "server_device"])
async def test_one_licence_request_refuses_a_cipher_without_live_licences(monkeypatch, refusal):
    """The single-track route answers FORBIDDEN for a cipher with no key in a session without live licences."""
    cipher = ClearKeyCTR(lambda: pytest.fail("licensed a cipher"), bytes(range(16)), b"OggS")
    track = SimpleNamespace(id="aud", drm=[cipher])
    request = {"mode": "server_cdm", "track_id": "aud", "drm_type": "clearkeyctr"}

    with pytest.raises(APIError) as refused:
        await batch_answer(monkeypatch, track, request=request, **{refusal: True})

    assert refused.value.error_code == "FORBIDDEN"


@pytest.mark.parametrize("mode", ["server_cdm", "proxy"])
async def test_one_licence_request_answers_the_key_of_a_cipher(monkeypatch, mode):
    """The single-track licence route answers the key of a whole-file cipher, licensed or not, and calls no CDM."""
    cipher = ClearKeyCTR(lambda: "bb" * 16, bytes(range(16)), b"OggS")
    if mode == "proxy":
        cipher.take_key("cc" * 16)
    track = SimpleNamespace(id="aud", drm=[cipher])

    body = await batch_answer(monkeypatch, track, request={"mode": mode, "track_id": "aud", "drm_type": "clearkeyctr"})

    assert body == {"file_keys": {"aud": ("cc" if mode == "proxy" else "bb") * 16}}


def test_the_server_licence_drops_a_null_key():
    kid, null = UUID(int=1), UUID(int=2)

    class Licensed:
        content_keys: dict = {}

        def get_content_keys(self, **kwargs):
            self.content_keys = {kid: "ab" * 16, null: "0" * 32}

    assert handlers.license_with_track_fallback(Licensed(), SimpleNamespace(drm=None)) == {kid.hex: "ab" * 16}
