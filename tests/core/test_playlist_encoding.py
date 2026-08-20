"""Unit tests for M3U8 response decoding. Reading `.text` on a response with no encoding
makes requests run chardet over the whole body, so both playlist fetch paths set utf-8
first. A charset the server did send must survive."""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from unshackle.core.manifests.hls import HLS
from unshackle.core.tracks import Video

pytestmark = pytest.mark.unit

MASTER = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720,CODECS="avc1.4d401f"\nvideo/720.m3u8\n'
MEDIA = b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg1.ts\n#EXT-X-ENDLIST\n"


def make_response(body: bytes, url: str, encoding: Optional[str]) -> requests.Response:
    res = requests.Response()
    res.status_code = 200
    res._content = body
    res.url = url
    res.encoding = encoding
    return res


class FakeSession(requests.Session):
    def __init__(self, res: requests.Response) -> None:
        super().__init__()
        self.res = res

    def get(self, url: str, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        return self.res


def test_hls_from_url_sets_utf8_when_server_sent_no_charset() -> None:
    res = make_response(MASTER, "https://example.com/master.m3u8", None)
    hls = HLS.from_url("https://example.com/master.m3u8", FakeSession(res))

    assert res.encoding == "utf-8"
    assert hls.manifest.is_variant
    assert len(hls.manifest.playlists) == 1


def test_hls_from_url_keeps_a_charset_the_server_sent() -> None:
    res = make_response(MASTER, "https://example.com/master.m3u8", "iso-8859-1")
    HLS.from_url("https://example.com/master.m3u8", FakeSession(res))

    assert res.encoding == "iso-8859-1"


def make_track(res: requests.Response) -> Video:
    track = Video.__new__(Video)
    track.id = "test"
    track.url = "https://example.com/video/720.m3u8"
    track.session = FakeSession(res)
    track.drm = None
    track.needs_drm_loading = True
    return track


def test_load_drm_from_playlist_sets_utf8_when_server_sent_no_charset() -> None:
    res = make_response(MEDIA, "https://example.com/video/720.m3u8", None)

    assert make_track(res).load_drm_from_playlist() is False
    assert res.encoding == "utf-8"


def test_load_drm_from_playlist_keeps_a_charset_the_server_sent() -> None:
    res = make_response(MEDIA, "https://example.com/video/720.m3u8", "iso-8859-1")

    make_track(res).load_drm_from_playlist()
    assert res.encoding == "iso-8859-1"
