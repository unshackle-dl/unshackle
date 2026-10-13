"""Unit tests for manifest response decoding. Reading `.text` on a response with no encoding
makes requests run chardet over the whole body, so the playlist fetch paths set utf-8
first. A charset the server did send must survive. An MPD is parsed from bytes, so the
XML declaration gives the encoding."""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from unshackle.core.manifests.dash import DASH
from unshackle.core.manifests.hls import HLS
from unshackle.core.session import RnetResponse, utf8_text
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
    if encoding:
        res.headers["Content-Type"] = f"application/vnd.apple.mpegurl; charset={encoding}"
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


MPD_LATIN1 = '<?xml version="1.0" encoding="ISO-8859-1"?><MPD><Title>café</Title></MPD>'.encode("iso-8859-1")


class StubRnetResponse:
    """The part of an rnet response that `RnetResponse` reads."""

    ok = True
    status_code = 200
    encoding = None

    def __init__(self, body: bytes, url: str) -> None:
        self.body = body
        self.url = url

    def bytes(self) -> bytes:
        return self.body


def test_dash_from_url_follows_the_xml_declaration() -> None:
    res = make_response(MPD_LATIN1, "https://example.com/manifest.mpd", None)
    dash = DASH.from_url("https://example.com/manifest.mpd", FakeSession(res))

    assert dash.manifest.findtext("Title") == "café"


def test_dash_from_url_reads_an_rnet_response() -> None:
    res = RnetResponse(StubRnetResponse(MPD_LATIN1, "https://example.com/manifest.mpd"))
    dash = DASH.from_url("https://example.com/manifest.mpd", FakeSession(res))  # type: ignore[arg-type]

    assert dash.manifest.findtext("Title") == "café"


def test_utf8_text_reads_an_rnet_response() -> None:
    assert utf8_text(RnetResponse(StubRnetResponse(MEDIA, "https://example.com/video/720.m3u8"))) == MEDIA.decode()


def test_utf8_text_ignores_the_requests_default_for_a_text_type() -> None:
    res = make_response('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,NAME="中文"\n'.encode(), "https://example.com/a.m3u8", None)
    res.headers["Content-Type"] = "text/plain"
    res.encoding = "ISO-8859-1"

    assert "中文" in utf8_text(res)


def test_dash_from_url_uses_the_http_charset_without_an_xml_declaration() -> None:
    res = make_response("<MPD><Title>中文</Title></MPD>".encode("gbk"), "https://example.com/manifest.mpd", "gbk")
    dash = DASH.from_url("https://example.com/manifest.mpd", FakeSession(res))

    assert dash.manifest.findtext("Title") == "中文"


def test_dash_from_url_rejects_an_empty_body() -> None:
    res = make_response(b"", "https://example.com/manifest.mpd", None)

    with pytest.raises(ValueError, match="must be provided"):
        DASH.from_url("https://example.com/manifest.mpd", FakeSession(res))
