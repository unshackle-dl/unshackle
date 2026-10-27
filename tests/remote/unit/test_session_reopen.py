"""A remote session the server lost, as on a restart, reopens and the failed call runs again."""

from __future__ import annotations

import json
import logging
from threading import Event, Lock

import pytest
import requests
import responses
from langcodes import Language

from unshackle.core.remote_service import RemoteClient, RemoteError, RemoteService, build_title
from unshackle.core.tracks import Audio
from unshackle.core.tracks.track import Track

pytestmark = pytest.mark.unit

SERVER = "http://srv:8786"
GONE = {"status": "error", "error_code": "SESSION_NOT_FOUND", "message": "Remote session not found: s1"}


def audio(id_: str) -> Audio:
    return Audio(
        url="https://cdn.test/a.m3u8",
        language=Language.get("en"),
        descriptor=Track.Descriptor.HLS,
        codec=Audio.Codec.AAC,
        bitrate=128_000,
        channels=2,
        id_=id_,
    )


def make_service() -> RemoteService:
    service = RemoteService.__new__(RemoteService)
    service.client = RemoteClient(server_url=SERVER, api_key="k")
    service.log = logging.getLogger("test")
    service.credential = None
    service.service_tag = service.title_id = "TEST"
    service._session_id = "s1"
    service._reopen_lock = Lock()
    service._keepalive_stop = Event()
    service._segment_filters = {}
    service._tracks_by_title = {}
    service._titles = None
    service._title_alias = {}
    service._track_alias = {}
    service._current_title = None
    service._log_seq = 0
    service._server_cdm = service._server_device = service._server_vault = False
    return service


def license_call(service: RemoteService, track: Track) -> dict:
    return service.session_call(
        lambda sid: service.client.post(f"/api/session/{sid}/license", {"track_id": service.server_track_id(track.id)})
    )


@responses.activate
def test_a_lost_session_reopens_and_replays_the_call_under_the_new_ids(monkeypatch):
    service = make_service()
    held = audio("a-old")
    title = build_title({"type": "episode", "id": "ep3-aaaa", "season": 1, "number": 3}, "TEST", "x")
    service._titles = [title]
    service._current_title = title
    service._tracks_by_title["ep3-aaaa"] = [held]

    def authenticate(credential=None):
        service._session_id = "s2"

    monkeypatch.setattr(service, "authenticate", authenticate)
    monkeypatch.setattr(service, "get_tracks", lambda t: [audio("a-new")])
    responses.add(responses.POST, f"{SERVER}/api/session/s1/license", json=GONE, status=404)
    responses.add(responses.GET, f"{SERVER}/api/health", json={"status": "ok", "draining": False})
    responses.add(responses.GET, f"{SERVER}/api/session/s1", json=GONE, status=404)
    renamed = {"type": "episode", "id": "ep3-bbbb", "season": 1, "number": 3}
    responses.add(responses.GET, f"{SERVER}/api/session/s2/titles", json={"titles": [renamed]})
    replay = responses.add(responses.POST, f"{SERVER}/api/session/s2/license", json={"license": "AA=="})

    assert license_call(service, held) == {"license": "AA=="}
    assert json.loads(replay.calls[0].request.body) == {"track_id": "a-new"}
    assert service._tracks_by_title["ep3-aaaa"] == [held]
    assert service._title_alias == {"ep3-aaaa": "ep3-bbbb"}


@responses.activate
def test_a_connect_error_on_a_live_session_runs_again_on_the_same_session():
    service = make_service()
    url = f"{SERVER}/api/session/s1/license"
    responses.add(responses.POST, url, body=requests.ConnectionError("reset"))
    responses.add(responses.POST, url, json={"license": "AA=="})
    responses.add(responses.GET, f"{SERVER}/api/health", json={"status": "ok"})
    responses.add(responses.GET, f"{SERVER}/api/session/s1", json={"valid": True})

    assert license_call(service, audio("a1")) == {"license": "AA=="}
    assert service._session_id == "s1"


@responses.activate
def test_a_timeout_on_a_live_session_is_not_sent_again():
    service = make_service()
    url = f"{SERVER}/api/session/s1/license"
    responses.add(responses.POST, url, body=requests.Timeout())
    responses.add(responses.GET, f"{SERVER}/api/health", json={"status": "ok"})
    responses.add(responses.GET, f"{SERVER}/api/session/s1", json={"valid": True})

    with pytest.raises(RemoteError) as exc:
        license_call(service, audio("a1"))

    assert exc.value.kind == "TIMEOUT"
    assert len([c for c in responses.calls if c.request.url == url]) == 1


@responses.activate
def test_a_gateway_error_page_counts_as_an_unreachable_server():
    client = RemoteClient(server_url=SERVER, api_key="k")
    page = "<html><title>Origin is unreachable</title></html>"
    responses.add(responses.GET, f"{SERVER}/api/session/s1/titles", body=page, status=530, content_type="text/html")

    with pytest.raises(RemoteError) as exc:
        client.get("/api/session/s1/titles")

    assert exc.value.kind == "CONNECT"
