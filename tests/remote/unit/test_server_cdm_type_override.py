"""The single-track server_cdm route licenses with the server's configured DRM system.

A client that holds a local PlayReady device asks for PlayReady, but a server whose
config.cdm maps the service to a Widevine device has no PlayReady device to answer with.
The batch route already ignores the client's choice; this route must match it, down to
licensing a PlayReady-only track with the Widevine device. With no server mapping, the
client's choice decides, and the type the routes plan with must be the type of the device
they load.
"""

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
from unshackle.core.api.errors import APIError

pytestmark = pytest.mark.unit

KID = UUID("161219ec3df64e0eadbb3827c0ccc46d")
WV_PSSH = "AAAAOHBzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAABgSEBYSGew99k4OrbtjJ8DMxG1I49yVmwY="
PR_PSSH = "UFI="


class _FakeWidevine:
    def __init__(self, pssh=None, **kwargs):
        self.kids = [KID]
        self.content_keys: dict = {}
        self.pssh = pssh

    def get_content_keys(self, cdm, certificate, licence):
        self.content_keys = {KID: "key_wv"}


class _FakePlayReady:
    def __init__(self, pssh=None, pssh_b64=None, kid=None, **kwargs):
        self.kids = [KID]
        self.content_keys: dict = {}
        self.pssh_b64 = pssh_b64
        self.data = {"pssh_b64": pssh_b64} if pssh_b64 else {}

    def absorb(self, *others):
        pass

    def get_content_keys(self, cdm, certificate, licence):
        self.content_keys = {KID: "key_pr"}


# extract_pssh_from_track matches on class name, so name the fakes after the DRM classes
_FakePlayReady.__name__ = "PlayReady"
_FakeWidevine.__name__ = "Widevine"


def _pr_drm():
    return _FakePlayReady(pssh_b64=PR_PSSH)


def _wv_drm():
    return _FakeWidevine(pssh=SimpleNamespace(dumps=lambda: WV_PSSH))


def _session(track):
    # handle_single_server_cdm looks config.cdm up by the service class name, so the fake needs the tag as its name
    service = type("PCOK", (SimpleNamespace,), {})(
        get_widevine_service_certificate=lambda **k: None,
        get_widevine_license=lambda **k: b"",
        get_playready_license=lambda **k: b"",
    )
    return SimpleNamespace(
        service_tag="PCOK",
        service_instance=service,
        served_keys={},
        tracks=SimpleNamespace(get={"vid": track}.get),
        log_buffer=None,
    )


@pytest.fixture
def env(monkeypatch):
    """Fake DRM classes and PSSH parsers; the CDM is a namespace that names its device."""
    monkeypatch.setattr(drm_mod, "Widevine", _FakeWidevine)
    monkeypatch.setattr(drm_mod, "PlayReady", _FakePlayReady)
    monkeypatch.setattr(pr_pssh_mod, "PSSH", lambda data: SimpleNamespace(data=data))
    monkeypatch.setattr(wv_pssh_mod, "PSSH", lambda data: SimpleNamespace(data=data))
    monkeypatch.setitem(cdm_mod.__dict__, "load_cdm", lambda name, **k: SimpleNamespace(device=name))
    monkeypatch.setattr(detect_mod, "is_widevine_cdm", lambda cdm: cdm.device.startswith("wv"))
    monkeypatch.setattr(detect_mod, "is_playready_cdm", lambda cdm: cdm.device.startswith("pr"))
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "check_vaults", lambda kids, name: None)
    monkeypatch.setattr(handlers, "cache_to_vaults", lambda keys, name: None)
    monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request, tag: True)
    monkeypatch.setattr(handlers, "find_title_for_track", lambda tid, session: SimpleNamespace())
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)
    # the device name says what the device is, in place of the .wvd / .prd file on disk
    monkeypatch.setattr(
        handlers, "detect_cdm_type", lambda name, cfg: {"wv": "widevine", "pr": "playready"}.get(name[:2])
    )


async def _license(monkeypatch, track, body):
    session = _session(track)

    async def fake_get_session(sid, req):
        return session

    monkeypatch.setattr(handlers, "get_validated_session", fake_get_session)
    resp = await handlers.session_license_handler({"track_id": "vid", "mode": "server_cdm", **body}, "sess", None)
    return json.loads(resp.body)


async def test_server_type_wins_over_client_request(env, monkeypatch):
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: "widevine")
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "wv_dev")
    monkeypatch.setattr(handlers, "require_track_pssh", lambda track, pssh, drm_type: None)

    track = SimpleNamespace(id="vid", drm=[_pr_drm(), _wv_drm()])
    body = await _license(monkeypatch, track, {"drm_type": "playready", "pssh": PR_PSSH})

    assert body["drm_type"] == "widevine"
    assert body["keys"] == {KID.hex: "key_wv"}
    assert not hasattr(track, "pr_pssh"), "the discarded client PSSH must not persist on the track"


async def test_playready_only_track_licenses_with_widevine_server(env, monkeypatch):
    """The batch route licenses a PlayReady-only header with the Widevine device; so must this one."""
    monkeypatch.setattr(handlers, "detect_cdm_type_for_service", lambda tag, cfg: "widevine")
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "wv_dev")

    track = SimpleNamespace(id="vid", drm=[_pr_drm()])
    body = await _license(monkeypatch, track, {"drm_type": "playready"})

    assert body["drm_type"] == "widevine"
    assert body["keys"] == {KID.hex: "key_wv"}


async def test_client_type_decides_without_server_mapping(env, monkeypatch):
    """No config.cdm entry for the service: the tier's devices answer in the client's order."""
    monkeypatch.setattr(handlers.config, "cdm", {}, raising=False)
    monkeypatch.setattr(
        handlers, "serve_user_config", lambda key: {"devices": ["wv_dev"], "playready_devices": ["pr_dev"]}
    )

    track = SimpleNamespace(id="vid", drm=[_pr_drm(), _wv_drm()])
    body = await _license(monkeypatch, track, {"drm_type": "playready"})
    assert body["drm_type"] == "playready"
    assert body["keys"] == {KID.hex: "key_pr"}

    track = SimpleNamespace(id="vid", drm=[_pr_drm(), _wv_drm()])
    body = await _license(monkeypatch, track, {"drm_type": "widevine"})
    assert body["drm_type"] == "widevine"
    assert body["keys"] == {KID.hex: "key_wv"}


async def test_quality_keyed_mapping_resolves_to_the_device_it_loads(env, monkeypatch):
    """A quality-tier mapping falls through to cdm.default on both the type probe and the device lookup.

    The type probe used to take the first tier's device (Widevine) while the device lookup
    returned the global default (PlayReady), so the route loaded a PlayReady device for a
    Widevine licence and failed with "is not a Widevine device".
    """
    monkeypatch.setattr(
        handlers.config,
        "cdm",
        {"PCOK": {"<=1080": "wv_tier", ">1080": {"widevine": "wv_tier", "playready": "pr_tier"}}, "default": "pr_dev"},
        raising=False,
    )

    track = SimpleNamespace(id="vid", drm=[_pr_drm(), _wv_drm()])
    body = await _license(monkeypatch, track, {"drm_type": "widevine"})

    assert body["drm_type"] == "playready"
    assert body["keys"] == {KID.hex: "key_pr"}


async def test_both_systems_mapped_follow_the_client(env, monkeypatch):
    """A mapping with a device per system settles nothing, so the client's choice goes first."""
    monkeypatch.setattr(handlers.config, "cdm", {"PCOK": {"widevine": "wv_dev", "playready": "pr_dev"}}, raising=False)

    track = SimpleNamespace(id="vid", drm=[_pr_drm(), _wv_drm()])
    body = await _license(monkeypatch, track, {"drm_type": "widevine"})

    assert body["drm_type"] == "widevine"


async def test_no_candidate_header_is_a_client_error(env, monkeypatch):
    monkeypatch.setattr(handlers.config, "cdm", {"PCOK": "pr_dev"}, raising=False)

    track = SimpleNamespace(id="vid", drm=[_wv_drm()])
    with pytest.raises(APIError, match="No PSSH on track"):
        await _license(monkeypatch, track, {"drm_type": "widevine"})


async def test_track_preference_goes_first_when_the_server_has_that_device(env, monkeypatch):
    """A track that prefers PlayReady licenses with PlayReady even on a Widevine-mapped server."""
    monkeypatch.setattr(handlers.config, "cdm", {"PCOK": {"widevine": "wv_dev", "default": "pr_dev"}}, raising=False)

    track = SimpleNamespace(id="vid", drm=[_pr_drm(), _wv_drm()], drm_preference="pr")
    body = await _license(monkeypatch, track, {"drm_type": "widevine"})

    assert body["drm_type"] == "playready"
    assert body["keys"] == {KID.hex: "key_pr"}


async def test_track_preference_falls_back_with_a_warning(env, monkeypatch, caplog):
    """With no PlayReady device the preference cannot be met: Widevine licenses and the client hears why."""
    monkeypatch.setattr(handlers.config, "cdm", {"PCOK": "wv_dev"}, raising=False)

    track = SimpleNamespace(id="vid", drm=[_pr_drm(), _wv_drm()], drm_preference="pr")
    with caplog.at_level("WARNING", logger=handlers.log.name):
        body = await _license(monkeypatch, track, {"drm_type": "playready"})

    assert body["drm_type"] == "widevine"
    assert "wants playready DRM but the server has no device for it" in caplog.text
