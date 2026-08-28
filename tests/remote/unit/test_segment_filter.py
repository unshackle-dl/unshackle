"""The server runs a track's OnSegmentFilter and the client skips the segments it names."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import responses

from unshackle.core.api import handlers
from unshackle.core.remote_service import RemoteClient, RemoteService

pytestmark = pytest.mark.unit

PLAYLIST = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXTINF:4,
main0.ts
#EXTINF:4,
ad1.ts
#EXTINF:4,
main2.ts
#EXT-X-ENDLIST
"""

BASE = "https://cdn.test/hls/media.m3u8"


def make_session(monkeypatch, track):
    session = SimpleNamespace(
        tracks={"t1": track},
        service_instance=SimpleNamespace(session=SimpleNamespace(get=lambda url: SimpleNamespace(text=PLAYLIST))),
    )

    async def get_validated_session(session_id, request):
        return session

    monkeypatch.setattr(handlers, "get_validated_session", get_validated_session)
    return session


async def test_handler_returns_unwanted_absolute_uris(monkeypatch):
    track = SimpleNamespace(url=BASE, OnSegmentFilter=lambda segment: "ad" in segment.uri)
    make_session(monkeypatch, track)

    response = await handlers.session_segment_filter_handler({"track_id": "t1"}, "s1")

    assert json.loads(response.body) == {"unwanted": ["https://cdn.test/hls/ad1.ts"]}


async def test_handler_returns_null_without_a_filter(monkeypatch):
    make_session(monkeypatch, SimpleNamespace(url=BASE, OnSegmentFilter=None))

    response = await handlers.session_segment_filter_handler({"track_id": "t1"}, "s1")

    assert json.loads(response.body) == {"unwanted": None}


def make_service(unwanted, status=200):
    service = RemoteService.__new__(RemoteService)
    service.client = RemoteClient(server_url="http://srv:8786", api_key="k")
    service._session_id = "s1"
    service._segment_filters = {}
    responses.add(
        responses.POST,
        "http://srv:8786/api/session/s1/segment_filter",
        json={"unwanted": unwanted} if status == 200 else {"message": "no"},
        status=status,
    )
    return service


@responses.activate
def test_client_filter_drops_only_the_unwanted_segments():
    service = make_service(["https://cdn.test/hls/ad1.ts"])
    segment_filter = service.remote_segment_filter("t1")

    assert segment_filter(SimpleNamespace(absolute_uri="https://cdn.test/hls/ad1.ts")) is True
    # a token-differing query string still matches on the path
    assert segment_filter(SimpleNamespace(absolute_uri="https://cdn.test/hls/ad1.ts?token=2")) is True
    assert segment_filter(SimpleNamespace(absolute_uri="https://cdn.test/hls/main0.ts")) is False
    # fetched once, then cached per track id
    assert len(responses.calls) == 1


@responses.activate
def test_client_filter_keeps_everything_when_the_server_has_no_route():
    service = make_service(None, status=404)
    segment_filter = service.remote_segment_filter("t1")

    assert segment_filter(SimpleNamespace(absolute_uri="https://cdn.test/hls/ad1.ts")) is False
