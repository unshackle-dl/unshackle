"""The server CDM must return keys a service wrote onto the track's own DRM object.

Some services skip the CDM exchange: the licence callback fetches the keys itself, writes
them into `track.drm[0].content_keys`, and returns a value the CDM cannot parse. `dl.py`
accepts the failed parse because the object it licensed already holds keys. The server
licenses a rebuilt object, so it must read the track's own objects too.
"""

import base64
from types import SimpleNamespace
from uuid import UUID

import pytest
from pyplayready.system.pssh import PSSH as PRPSSH
from pywidevine.pssh import PSSH as WvPSSH

import unshackle.core.cdm as cdm_mod
import unshackle.core.cdm.detect as detect_mod
from unshackle.core.api import handlers
from unshackle.core.drm import PlayReady, Widevine

pytestmark = pytest.mark.unit

KID_A = UUID("161219ec3df64e0eadbb3827c0ccc46d")
KEY_A = "0" * 31 + "1"


class _BypassCdm:
    """A CDM that cannot parse the service's stand-in licence, like pywidevine or decrypt-labs."""

    service_certificate_challenge = b"cert"

    def open(self):
        return b"sid"

    def close(self, session_id):
        pass

    def set_pssh_b64(self, *a, **k):
        pass

    def get_license_challenge(self, session_id, pssh):
        return b"challenge"

    def parse_license(self, session_id, licence):
        raise ValueError(f"not a licence: {licence!r}")

    def get_keys(self, session_id, type_=None):
        return []


@pytest.fixture
def server_env(monkeypatch):
    monkeypatch.setitem(cdm_mod.__dict__, "load_cdm", lambda *a, **k: _BypassCdm())
    monkeypatch.setattr(detect_mod, "is_widevine_cdm", lambda cdm: True)
    monkeypatch.setattr(detect_mod, "is_playready_cdm", lambda cdm: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers, "check_vaults", lambda kids, name: None)
    monkeypatch.setattr(handlers, "cache_to_vaults", lambda keys, name: None)
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)


def _bypass_service(kind: str):
    """A service that writes the key onto the track's own object and returns a stand-in licence."""

    def licence(*, challenge, title, track, **_):
        track.drm[0].content_keys[KID_A] = KEY_A
        return b"bypass"

    return SimpleNamespace(
        get_widevine_service_certificate=lambda **_: None,
        **{f"get_{kind}_license": licence},
    )


def test_widevine_bypass_keys_reach_the_client(server_env):
    pssh = WvPSSH.new(key_ids=[KID_A], system_id=WvPSSH.SystemId.Widevine)
    track = SimpleNamespace(id="vid", drm=[Widevine(pssh=pssh, kid=KID_A)])

    keys = handlers.handle_single_server_cdm(
        _bypass_service("widevine"), SimpleNamespace(), track, pssh.dumps(), "widevine", None
    )

    assert keys == {KID_A.hex: KEY_A}


def test_playready_bypass_keys_reach_the_client(server_env):
    xml = (
        '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="4.2.0.0">'
        "<DATA><PROTECTINFO></PROTECTINFO><LA_URL>https://example.invalid/</LA_URL></DATA></WRMHEADER>"
    )
    record = xml.encode("utf-16-le")
    pro = PRPSSH.PlayreadyHeader.build(
        {"length": 10 + len(record), "records": [{"type": 1, "length": len(record), "data": record}]}
    )
    pro_b64 = base64.b64encode(pro).decode()
    track = SimpleNamespace(id="vid", drm=[PlayReady(pssh=PRPSSH(pro), kid=KID_A, pssh_b64=pro_b64)])

    keys = handlers.handle_single_server_cdm(
        _bypass_service("playready"), SimpleNamespace(), track, pro_b64, "playready", None
    )

    assert keys == {KID_A.hex: KEY_A}


def test_failed_licence_without_injected_keys_still_raises(server_env):
    """The fold is not a licence to swallow real failures."""
    pssh = WvPSSH.new(key_ids=[KID_A], system_id=WvPSSH.SystemId.Widevine)
    track = SimpleNamespace(id="vid", drm=[Widevine(pssh=pssh, kid=KID_A)])
    service = SimpleNamespace(
        get_widevine_service_certificate=lambda **_: None,
        get_widevine_license=lambda **_: b"garbage",
    )

    with pytest.raises(ValueError, match="not a licence"):
        handlers.handle_single_server_cdm(service, SimpleNamespace(), track, pssh.dumps(), "widevine", None)
