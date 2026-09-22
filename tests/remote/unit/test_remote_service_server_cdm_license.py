"""The client's single-track server_cdm licence follows the server's DRM system.

The server's config.cdm mapping decides the system, so the client sends the header of the
system the server reported at session create, not the one its local device asked for, and
takes the system the server answered with for the next call.
"""

import logging
from types import SimpleNamespace
from uuid import UUID

import pytest

from unshackle.core.remote_service import RemoteService

pytestmark = pytest.mark.unit

KID = UUID("161219ec3df64e0eadbb3827c0ccc46d")


class _PlayReady:
    def __init__(self):
        self.data = {"pssh_b64": "PR"}
        self.content_keys: dict = {}


class _Widevine:
    def __init__(self):
        self.pssh = SimpleNamespace(dumps=lambda: "WV")
        self.content_keys: dict = {}


# track_pssh matches on the DRM class name, so name the fakes after the DRM classes
_PlayReady.__name__ = "PlayReady"
_Widevine.__name__ = "Widevine"


class _Client:
    def __init__(self, answer):
        self.answer = answer
        self.posts: list = []

    def post(self, endpoint, payload):
        self.posts.append(payload)
        return self.answer


def _service(server_type, answer):
    svc = RemoteService.__new__(RemoteService)
    svc._server_cdm = True
    svc._server_cdm_type = server_type
    svc._session_id = "sess"
    svc.server_vault_keys = {}
    svc.log = logging.getLogger("test-remote-license")
    svc.client = _Client(answer)
    return svc


def test_server_type_picks_the_header_and_the_posted_type():
    svc = _service("widevine", {"keys": {KID.hex: "k"}, "drm_type": "widevine"})
    track = SimpleNamespace(id="vid", drm=[_PlayReady(), _Widevine()])

    svc.proxy_license(b"", track, "playready")

    assert svc.client.posts == [{"track_id": "vid", "drm_type": "widevine", "mode": "server_cdm", "pssh": "WV"}]
    assert all(drm.content_keys == {KID: "k"} for drm in track.drm)


def test_missing_header_of_the_server_type_sends_the_other_one():
    """A PlayReady-only track under a Widevine server: the header goes with its own system name."""
    svc = _service("widevine", {"keys": {KID.hex: "k"}, "drm_type": "widevine"})
    track = SimpleNamespace(id="vid", drm=[_PlayReady()])

    svc.proxy_license(b"", track, "widevine")

    assert svc.client.posts[0]["drm_type"] == "playready"
    assert svc.client.posts[0]["pssh"] == "PR"


def test_response_type_refreshes_the_server_type():
    svc = _service("widevine", {"keys": {KID.hex: "k"}, "drm_type": "playready"})
    track = SimpleNamespace(id="vid", drm=[_PlayReady(), _Widevine()])

    svc.proxy_license(b"", track, "widevine")

    assert svc._server_cdm_type == "playready"


def test_proxy_mode_keeps_the_callers_type():
    svc = _service("widevine", {"license": "bGlj"})
    svc._server_cdm = False
    svc.drain_server_logs = lambda: None
    track = SimpleNamespace(id="vid", drm=[_PlayReady(), _Widevine()])

    licence = svc.proxy_license(b"chal", track, "playready")

    assert licence == b"lic"
    assert svc.client.posts[0]["drm_type"] == "playready"
    assert svc.client.posts[0]["pssh"] == "PR"
