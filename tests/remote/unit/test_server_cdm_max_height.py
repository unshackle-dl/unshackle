"""A per-key height cap on the server CDM hands licensing above it to the client's own device."""

import json
import logging
from types import SimpleNamespace
from uuid import UUID

import click
import pyplayready.system.pssh as pr_pssh_mod
import pytest
import pywidevine.pssh as wv_pssh_mod
from rich.table import Table

import unshackle.core.drm as drm_mod
from unshackle.commands.dl import dl
from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError, APIErrorCode
from unshackle.core.api.session_store import SessionEntry
from unshackle.core.drm import Widevine
from unshackle.core.remote_service import RemoteClient, RemoteService
from unshackle.core.tracks import Audio, Video

pytestmark = pytest.mark.unit

USERS = {
    "flat": {"server_cdm": True, "server_cdm_max_height": 1080},
    "mapped": {"server_cdm": True, "server_cdm_max_height": {"example1": 720, "default": 1080}},
    "partial": {"server_cdm": True, "server_cdm_max_height": {"EXAMPLE1": 1080}},
    "open": {"server_cdm": True},
    "none": {},
}


@pytest.fixture(autouse=True)
def serve_users(monkeypatch):
    monkeypatch.setattr(handlers.config, "serve", {"users": USERS}, raising=False)
    monkeypatch.setattr(handlers, "request_secret_key", lambda request: request.headers.get("X-Secret-Key"))


def request(key):
    return SimpleNamespace(headers={"X-Secret-Key": key} if key else {})


def video(height, width=None):
    track = Video.__new__(Video)
    track.height = height
    track.width = width
    return track


@pytest.mark.parametrize(
    ("key", "service", "cap"),
    [
        ("flat", "EXAMPLE1", 1080),
        ("mapped", "EXAMPLE1", 720),
        ("mapped", "EXAMPLE2", 1080),
        ("partial", "EXAMPLE2", None),
        ("open", "EXAMPLE1", None),
        ("admin", "EXAMPLE1", None),
    ],
)
def test_cap_per_key_and_service(key, service, cap):
    assert handlers.server_cdm_max_height(request(key), service) == cap


@pytest.mark.parametrize(
    ("key", "data", "expected"),
    [
        ("open", {"quality": [2160]}, (True, None)),
        ("flat", {"quality": [1080]}, (True, 1080)),
        ("flat", {}, (True, 1080)),  # no -q keeps the server CDM
        ("flat", {"quality": [2160, 1080], "cdm_type": "widevine"}, (False, 1080)),
        ("flat", {"quality": ["2160p"], "cdm_type": "widevine"}, (False, 1080)),
        ("none", {"quality": [2160], "cdm_type": "widevine"}, (False, None)),
    ],
)
def test_session_mode(key, data, expected):
    assert handlers.choose_session_cdm(request(key), "EXAMPLE1", data) == expected


def test_above_the_cap_without_a_device_is_refused_at_create():
    with pytest.raises(APIError) as ei:
        handlers.choose_session_cdm(request("flat"), "EXAMPLE1", {"quality": [2160]})
    assert ei.value.error_code is APIErrorCode.SERVER_CDM_CAPPED
    assert ei.value.details == {"reason": "server_cdm_max_height", "max_height": 1080}


def test_bad_quality_is_invalid_input():
    with pytest.raises(APIError) as ei:
        handlers.choose_session_cdm(request("flat"), "EXAMPLE1", {"quality": ["best"]})
    assert ei.value.error_code is APIErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    ("session", "track", "refused"),
    [
        (SimpleNamespace(server_cdm=True, server_cdm_max_height=1080), video(2160), True),
        (SimpleNamespace(server_cdm=True, server_cdm_max_height=1080), video(1080), False),
        (SimpleNamespace(server_cdm=True, server_cdm_max_height=1080), video(None), False),
        (SimpleNamespace(server_cdm=True, server_cdm_max_height=1080), Audio.__new__(Audio), False),
        (SimpleNamespace(server_cdm=True, server_cdm_max_height=None), video(2160), False),
        (SimpleNamespace(server_cdm=False, server_cdm_max_height=1080), video(720), True),
        (SimpleNamespace(), video(2160), False),  # a session from before the cap existed
    ],
)
def test_live_licence_refusal(session, track, refused):
    assert (handlers.live_licence_refusal(session, track) is not None) is refused


@pytest.fixture
def widevine_env(monkeypatch):
    class _FakeWidevine:
        def __init__(self, pssh=None, **kwargs):
            self.kids = ["11" * 16]

    monkeypatch.setattr(wv_pssh_mod, "PSSH", lambda b64: b64)
    monkeypatch.setattr(drm_mod, "Widevine", _FakeWidevine)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")

    def no_device(*a, **k):
        raise AssertionError("a refused track must not load the server's device")

    monkeypatch.setattr("unshackle.core.cdm.load_cdm", no_device)


def single(refusal):
    return handlers.handle_single_server_cdm(
        SimpleNamespace(), SimpleNamespace(), video(2160), "cHNzaA==", "widevine", None, {}, refusal
    )


def test_a_vault_hit_is_served_above_the_cap(widevine_env, monkeypatch):
    monkeypatch.setattr(handlers, "check_vaults", lambda kids, name: ({"11" * 16: "key"}, {"11" * 16: "sqlite"}))
    refusal = handlers.live_licence_refusal(SimpleNamespace(server_cdm_max_height=1080), video(2160))
    assert single(refusal) == {"11" * 16: "key"}


def test_a_vault_miss_raises_the_refusal_before_the_device_loads(widevine_env, monkeypatch):
    monkeypatch.setattr(handlers, "check_vaults", lambda kids, name: None)
    refusal = handlers.live_licence_refusal(SimpleNamespace(server_cdm_max_height=1080), video(2160))
    with pytest.raises(APIError) as ei:
        single(refusal)
    assert ei.value is refusal


@pytest.mark.parametrize(("quality", "allowed"), [([1080], True), ([720, 1080], True), ([2160], False), ([], False)])
def test_download_jobs_stay_inside_the_cap(quality, allowed):
    params = {"service": "EXAMPLE1", "quality": quality}
    if allowed:
        handlers.enforce_download_gates(params, request("flat"))
    else:
        with pytest.raises(APIError) as ei:
            handlers.enforce_download_gates(params, request("flat"))
        assert ei.value.error_code is APIErrorCode.SERVER_CDM_CAPPED


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (None, True),
        (1080, True),
        ({"EXAMPLE1": 720, "default": None}, True),
        (True, False),
        ("1080", False),
        (-1, False),
    ],
)
def test_config_shape(value, ok):
    assert (handlers.server_cdm_max_height_error("serve.users.x", value) is None) is ok


class _WV:
    pssh = SimpleNamespace(dumps=lambda: "WV")


_WV.__name__ = "Widevine"


def remote(local_cdm, answer):
    svc = RemoteService.__new__(RemoteService)
    svc._server_cdm = True
    svc._server_cdm_type = "playready"
    svc._session_id = "sess"
    svc.server_vault_keys = {}
    svc.client_licensed = set()
    svc.log = logging.getLogger("test-server-cdm-cap")
    svc.ctx = SimpleNamespace(obj=SimpleNamespace(cdm=local_cdm))
    posts: list = []
    svc.client = SimpleNamespace(post=lambda endpoint, payload, expect=(): posts.append(payload) or answer)
    svc.drain_server_logs = lambda: None
    svc.posts = posts
    return svc


class _Title:
    def __init__(self, track):
        self.tracks = _Tracks([track])


class _Tracks(list):
    @property
    def videos(self):
        return list(self)

    @property
    def audio(self):
        return []


CAPPED = {"keys": {}, "capped_tracks": {"vid": {"reason": "server_cdm_max_height", "max_height": 1080}}}


def test_a_capped_track_moves_to_the_local_device():
    track = SimpleNamespace(id="vid", drm=[_WV()], drm_preference=None)
    svc = remote(SimpleNamespace(is_playready=False, security_level=1), CAPPED)

    svc.resolve_server_keys(_Title(track))

    assert svc.client_licensed == {"vid"}
    assert track.drm_preference == "widevine"

    svc.posts.clear()
    svc.client.post = lambda endpoint, payload: svc.posts.append(payload) or {"license": "bGlj"}
    assert svc.proxy_license(b"challenge", track, "widevine") == b"lic"
    assert "mode" not in svc.posts[0] and svc.posts[0]["challenge"]


def test_a_capped_track_without_a_local_device_fails_clearly():
    track = SimpleNamespace(id="vid", drm=[_WV()], drm_preference=None)
    svc = remote(None, CAPPED)
    with pytest.raises(click.ClickException, match="1080p"):
        svc.resolve_server_keys(_Title(track))


def test_a_capped_track_without_the_local_drm_system_fails_clearly():
    track = SimpleNamespace(id="vid", drm=[_WV()], drm_preference=None)
    svc = remote(SimpleNamespace(is_playready=True, security_level=3000), CAPPED)
    with pytest.raises(click.ClickException, match="PlayReady"):
        svc.resolve_server_keys(_Title(track))


def test_a_null_service_in_the_cap_map_falls_through_to_the_default(monkeypatch):
    users = {"k": {"server_cdm": True, "server_cdm_max_height": {"EXAMPLE1": None, "default": 1080}}}
    monkeypatch.setattr(handlers.config, "serve", {"users": users}, raising=False)
    assert handlers.server_cdm_max_height(request("k"), "EXAMPLE1") == 1080


def test_an_ultrawide_track_is_measured_as_the_smallest_quality_that_selects_it():
    """-q matches a height or a 16:9 width, so a track passes the cap when either one is inside it."""
    session = SimpleNamespace(server_cdm=True, server_cdm_max_height=1080)
    assert handlers.live_licence_refusal(session, video(1600, 3840)) is not None
    assert handlers.live_licence_refusal(session, video(None, 3840)) is not None
    assert handlers.live_licence_refusal(session, video(1080, 2560)) is None  # -q 1080 selects it
    assert handlers.live_licence_refusal(session, video(1080, 1920)) is None


@pytest.mark.parametrize("quality", [[-1], [0], [1080, 0]])
def test_a_height_of_zero_or_less_is_invalid_input(quality):
    with pytest.raises(APIError) as ei:
        handlers.enforce_download_gates({"service": "EXAMPLE1", "quality": quality}, request("flat"))
    assert ei.value.error_code is APIErrorCode.INVALID_INPUT


def test_a_capped_download_job_cannot_set_best_available():
    with pytest.raises(APIError) as ei:
        handlers.enforce_download_gates(
            {"service": "EXAMPLE1", "quality": [1080], "best_available": True}, request("flat")
        )
    assert ei.value.error_code is APIErrorCode.SERVER_CDM_CAPPED


def test_the_download_gate_reads_the_serve_dl_defaults(monkeypatch):
    serve = {"users": USERS, "quality": [1080], "best_available": True}
    monkeypatch.setattr(handlers.config, "serve", serve, raising=False)
    with pytest.raises(APIError) as ei:
        handlers.enforce_download_gates({"service": "EXAMPLE1"}, request("flat"))
    assert ei.value.error_code is APIErrorCode.SERVER_CDM_CAPPED

    serve["best_available"] = False
    handlers.enforce_download_gates({"service": "EXAMPLE1"}, request("flat"))
    # the job's own value wins over the serve default
    handlers.enforce_download_gates({"service": "EXAMPLE1", "best_available": False}, request("flat"))


@pytest.mark.parametrize("hdr_range", ["HYBRID", ["SDR", "hybrid"], [Video.Range.HYBRID]])
def test_a_capped_download_job_cannot_ask_for_hybrid(hdr_range):
    """HYBRID always keeps the lowest DV track, whatever the quality."""
    with pytest.raises(APIError) as ei:
        handlers.enforce_download_gates({"service": "EXAMPLE1", "quality": [1080], "range": hdr_range}, request("flat"))
    assert ei.value.error_code is APIErrorCode.SERVER_CDM_CAPPED
    handlers.enforce_download_gates({"service": "EXAMPLE1", "quality": [1080], "range": ["HDR10"]}, request("flat"))


def test_the_download_gate_reads_the_service_click_defaults(monkeypatch):
    """A service's own click default overrides the serve default, and the gate sees what the job runs with."""
    import asyncio

    from unshackle.core.api import download_manager as dm

    option = click.Option(["--best-available"], is_flag=True, default=True)
    monkeypatch.setattr(handlers, "validate_service", lambda service, request=None: "EXAMPLE1")
    monkeypatch.setattr(
        handlers.Services, "load", lambda service: SimpleNamespace(cli=SimpleNamespace(params=[option]))
    )

    def no_job():
        raise AssertionError("a refused job must not reach the download manager")

    monkeypatch.setattr(dm, "get_download_manager", no_job)
    body = {"service": "EXAMPLE1", "title_id": "t1", "quality": [1080]}
    with pytest.raises(APIError) as ei:
        asyncio.run(handlers.download_handler(body, request("flat")))
    assert ei.value.error_code is APIErrorCode.SERVER_CDM_CAPPED


@pytest.fixture
def playready_env(monkeypatch):
    class _FakePlayReady:
        def __init__(self, **kwargs):
            self.kids = []

    monkeypatch.setattr(pr_pssh_mod, "PSSH", lambda data: data)
    monkeypatch.setattr(drm_mod, "PlayReady", _FakePlayReady)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")

    def no_device(*a, **k):
        raise AssertionError("a refused track must not load the server's device")

    monkeypatch.setattr("unshackle.core.cdm.load_cdm", no_device)


def test_playready_raises_the_refusal_before_the_device_loads(playready_env):
    refusal = handlers.live_licence_refusal(SimpleNamespace(server_cdm_max_height=1080), video(2160))
    with pytest.raises(APIError) as ei:
        handlers.handle_single_server_cdm(
            SimpleNamespace(), SimpleNamespace(), video(2160), "cHNzaA==", "playready", None, {}, refusal
        )
    assert ei.value is refusal


KID = UUID("161219ec3df64e0eadbb3827c0ccc46d")
INIT_KID = UUID("75982fac2cd8412a990a59c0ef2d4bb1")
CAP_DETAILS = {"reason": "server_cdm_max_height", "max_height": 1080}


class _PSSHDrm:
    def __init__(self, b64, kids):
        self._pssh = SimpleNamespace(dumps=lambda: b64)
        self.kids = kids


_PSSHDrm.__name__ = "Widevine"


def media(kind, tid, drm, **attrs):
    track = kind.__new__(kind)
    track.id = tid
    track.drm = drm
    track.drm_preference = None
    for name, value in attrs.items():
        setattr(track, name, value)
    return track


@pytest.fixture
def licence_env(monkeypatch):
    """A capped remote session and a stand-in server licence that honours the refusal like the real one."""
    session = SessionEntry(session_id="sess", service_tag="EXAMPLE1", service_instance=SimpleNamespace())
    session.server_cdm = True
    session.server_cdm_max_height = 1080
    calls: list = []
    vault = {}

    def fake_single(service, title, track, pssh_str, drm_type, request, sources=None, refusal=None, vault_only=False):
        calls.append((track.id, pssh_str))
        if pssh_str in vault:
            return dict(vault[pssh_str])
        if refusal:
            raise refusal
        return {KID.hex: "k"}

    monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request, tag: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "find_title_for_track", lambda tid, session: SimpleNamespace())
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: "widevine")
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers, "fetch_init_segment", lambda track, sess: None)
    monkeypatch.setattr(handlers, "handle_single_server_cdm", fake_single)
    return SimpleNamespace(session=session, calls=calls, vault=vault)


def batch(env, *tracks):
    env.session.tracks = {t.id: t for t in tracks}
    data = {"mode": "server_cdm", "track_ids": [t.id for t in tracks], "drm_type": "widevine"}
    return json.loads(handlers.license_response(data, env.session, "sess", None).body)


def test_batch_hands_back_a_capped_track_and_still_licenses_its_pssh_sibling(licence_env):
    shared = [_PSSHDrm("manifest", [KID])]
    payload = batch(licence_env, media(Video, "vid", shared, height=2160, width=3840), media(Audio, "aud", shared))
    assert payload["capped_tracks"] == {"vid": CAP_DETAILS}
    assert payload["keys"] == {"aud": {KID.hex: "k"}}
    assert licence_env.calls == [("vid", "manifest"), ("aud", "manifest")]


@pytest.fixture
def init_segment(monkeypatch):
    init_drm = [_PSSHDrm("init", [INIT_KID])]
    monkeypatch.setattr(handlers, "fetch_init_segment", lambda track, sess: b"init")
    monkeypatch.setattr(handlers, "drm_from_init_segment", lambda track, session=None, init_data=None: init_drm)


def test_batch_restores_the_manifest_drm_on_a_track_refused_twice(licence_env, init_segment):
    manifest = [_PSSHDrm("manifest", [KID])]
    track = media(Video, "vid", manifest, height=2160, width=3840, get_key_id=lambda init: INIT_KID)
    payload = batch(licence_env, track)
    assert payload["capped_tracks"] == {"vid": CAP_DETAILS}
    assert track.drm is manifest
    assert licence_env.calls == [("vid", "manifest"), ("vid", "init")]


def test_batch_serves_a_capped_track_from_a_vault_hit_on_its_init_pssh(licence_env, init_segment):
    licence_env.vault["init"] = {INIT_KID.hex: "v"}
    track = media(Video, "vid", [_PSSHDrm("manifest", [KID])], height=2160, get_key_id=lambda init: INIT_KID)
    payload = batch(licence_env, track)
    assert "capped_tracks" not in payload
    assert payload["keys"] == {"vid": {INIT_KID.hex: "v"}}


def test_single_licence_refuses_a_capped_track_with_details(licence_env):
    track = media(Video, "vid", [_PSSHDrm("manifest", [KID])], height=2160)
    licence_env.session.tracks = {"vid": track}
    data = {"mode": "server_cdm", "track_id": "vid", "drm_type": "widevine"}
    with pytest.raises(APIError) as ei:
        handlers.license_response(data, licence_env.session, "sess", None)
    assert ei.value.error_code is APIErrorCode.SERVER_CDM_CAPPED
    assert ei.value.details == CAP_DETAILS


def test_a_client_device_session_builds_the_service_on_a_stub_of_that_device(monkeypatch):
    built: dict = {}

    def no_server_device(*a, **k):
        raise AssertionError("a client-device session must not load the server's device")

    monkeypatch.setattr(handlers, "load_service_yaml", lambda tag: {})
    monkeypatch.setattr(handlers, "load_full_cdm", no_server_device)
    monkeypatch.setattr(handlers, "build_parent_ctx", lambda profile, cdm, *a, **k: built.update(cdm=cdm))
    monkeypatch.setattr(handlers.Services, "load", lambda tag: object)
    monkeypatch.setattr(handlers, "instantiate_service", lambda *a, **k: "service")

    data = {"cdm_type": "playready", "cdm_security_level": 3000}
    handlers.create_service_instance("EXAMPLE1", "t", data, None, [], None, client_device=True)

    assert built["cdm"].is_playready is True
    assert built["cdm"].security_level == 3000


def test_the_tracks_response_echoes_the_session_choice(monkeypatch):
    import asyncio

    from unshackle.core.api import session_store as store_mod
    from unshackle.core.tracks import Tracks

    monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request, tag: True)
    monkeypatch.setattr(store_mod.bus, "publish", lambda topic, data: None)

    class StubService:
        session = SimpleNamespace(headers={}, cookies=[])

        def get_tracks(self, title):
            return Tracks()

        def get_chapters(self, title):
            raise NotImplementedError

    async def run():
        store = store_mod.get_session_store()
        entry = await store.create(
            service_tag="EXAMPLE1", service_instance=StubService(), session_id="capped", owner_key=None
        )
        entry.title_map = {"t1": "Title"}
        entry.server_cdm = False
        try:
            resp = await handlers.session_tracks_handler({"title_id": "t1"}, "capped")
            return json.loads(resp.body)
        finally:
            await store.delete("capped")

    assert asyncio.run(run())["server_cdm"] is False


class _LocalWidevine(Widevine):
    """Stops at the licence call and reports the device dl handed it."""

    class Used(Exception):
        pass

    def __init__(self):
        self._pssh = SimpleNamespace(dumps=lambda: "AAAA", key_ids=[UUID(int=0xCA9)])
        self.content_keys = {}

    def get_content_keys(self, cdm, licence, certificate):
        raise self.Used(cdm)


def prepare(svc, licence):
    runner = dl.__new__(dl)
    runner._remote_service = svc
    runner.cdm = "server stub"
    runner.service = "EXAMPLE1"
    runner.debug_logger = None
    runner.log = logging.getLogger("test-server-cdm-cap")
    table = Table()
    table.add_column()
    track = SimpleNamespace(id="vid", drm=None, get_drm_for_cdm=lambda cdm: None)
    with pytest.raises(_LocalWidevine.Used) as ei:
        runner.prepare_drm(_LocalWidevine(), track, None, lambda **k: None, licence, table=table, cdm_only=True)
    return ei.value.args[0]


def test_dl_licenses_a_client_licensed_track_with_the_local_device():
    svc = SimpleNamespace(_server_cdm=True, client_licensed={"vid"}, local_cdm="local device")

    server_calls: list = []

    assert prepare(svc, lambda **kwargs: server_calls.append(kwargs)) == "local device"
    assert server_calls == []


def test_dl_takes_the_local_path_when_the_server_hands_the_track_over():
    svc = SimpleNamespace(_server_cdm=True, client_licensed=set(), local_cdm="local device")
    assert prepare(svc, lambda **kwargs: svc.client_licensed.add("vid")) == "local device"


class _KeyedWidevine(_LocalWidevine):
    """Licenses one content key with whatever device dl hands it."""

    def get_content_keys(self, cdm, licence, certificate):
        self.content_keys[UUID(int=0xCA9)] = "ab" * 16


class PlayReady:
    kids = [UUID(int=0xCA9)]

    def __init__(self):
        self.content_keys = {}


def test_a_mid_download_handover_puts_the_keys_on_the_callers_drm(monkeypatch):
    """The caller decrypts with the DRM object it passed in, so the local licence must fill that one."""
    monkeypatch.setattr(dl, "LICENSE_KEY_CACHE", {})
    svc = SimpleNamespace(_server_cdm=True, client_licensed=set(), local_cdm="local device", server_vault_keys={})
    runner = dl.__new__(dl)
    runner._remote_service = svc
    runner.cdm = "server stub"
    runner.service = "EXAMPLE1"
    runner.debug_logger = None
    runner.log = logging.getLogger("test-server-cdm-cap")
    runner.flush_vault_writes = lambda pending: None
    table = Table()
    table.add_column()
    server_drm, local_drm = PlayReady(), _KeyedWidevine()
    track = SimpleNamespace(id="vid", drm=[server_drm, local_drm], get_drm_for_cdm=lambda cdm: local_drm)

    runner.prepare_drm(
        server_drm,
        track,
        None,
        lambda **k: None,
        lambda **k: svc.client_licensed.add("vid"),
        table=table,
        cdm_only=True,
    )

    assert server_drm.content_keys == {UUID(int=0xCA9): "ab" * 16}


def test_a_capped_hls_track_with_no_drm_yet_moves_to_the_local_device():
    track = SimpleNamespace(id="vid", drm=[], drm_preference=None)
    svc = remote(SimpleNamespace(is_playready=False, security_level=1), CAPPED)
    svc.resolve_server_keys(_Title(track))
    assert svc.client_licensed == {"vid"}
    assert track.drm_preference == "widevine"


def test_resolve_server_keys_forgets_the_last_titles_handovers():
    track = SimpleNamespace(id="vid", drm=[_WV()], drm_preference=None)
    svc = remote(SimpleNamespace(is_playready=False, security_level=1), {"keys": {}})
    svc.client_licensed = {"vid"}
    svc.resolve_server_keys(_Title(track))
    assert svc.client_licensed == set()


def test_a_single_licence_refusal_hands_the_track_to_the_local_device():
    track = SimpleNamespace(id="vid", drm=[_WV()], drm_preference=None)
    refusal = {"status": "error", "error_code": "SERVER_CDM_CAPPED", "details": CAP_DETAILS}
    svc = remote(SimpleNamespace(is_playready=False, security_level=1), refusal)

    assert svc.proxy_license(b"", track, "widevine") == b""
    assert svc.client_licensed == {"vid"}
    assert track.drm_preference == "widevine"


class _Response:
    def __init__(self, status, body):
        self.status_code, self.body, self.text = status, body, json.dumps(body)

    def json(self):
        return self.body


def test_the_client_returns_only_an_expected_error_body():
    client = RemoteClient("http://server", "key")
    body = {"status": "error", "error_code": "SERVER_CDM_CAPPED", "message": "capped"}
    client._session = SimpleNamespace(post=lambda url, json, timeout: _Response(403, body))
    assert client.post("/x", {}, expect=("SERVER_CDM_CAPPED",)) == body
    with pytest.raises(SystemExit):
        client.post("/x", {})
    with pytest.raises(SystemExit):
        client.post("/x", {}, expect=("FORBIDDEN",))


def test_cdm_with_an_uncapped_server_cdm_key_warns_that_it_is_ignored(caplog):
    svc = remote(SimpleNamespace(is_playready=False), {})
    svc.service_tag = "EXAMPLE1"
    svc.ctx.parent = SimpleNamespace(params={"cdm_name": "my_device"})
    with caplog.at_level(logging.WARNING):
        svc.adopt_session_cdm(True, None)
    assert "--cdm is ignored" in caplog.text

    caplog.clear()
    svc.ctx.parent.params = {}
    with caplog.at_level(logging.WARNING):
        svc.adopt_session_cdm(True, None)
    assert "--cdm is ignored" not in caplog.text
