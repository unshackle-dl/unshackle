"""The server must read HLS media-playlist DRM for a track the service built by hand.

A service can make an HLS `Video` from a media playlist URL without going through
`HLS.to_tracks`, so the track carries the HLS descriptor but no `data["hls"]`. The
batch server CDM route then saw no DRM, told the client the track carried none, and left
the client to license the same track again through the single-track route.
"""

import base64
from types import SimpleNamespace
from uuid import UUID

import pytest
from pywidevine.pssh import PSSH as WvPSSH

from unshackle.core.api import handlers
from unshackle.core.drm import Widevine
from unshackle.core.tracks import Track

pytestmark = pytest.mark.unit

KID = UUID("161219ec3df64e0eadbb3827c0ccc46d")


def _media_playlist() -> str:
    pssh = WvPSSH.new(key_ids=[KID], system_id=WvPSSH.SystemId.Widevine).dumps()
    return "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            "#EXT-X-TARGETDURATION:6",
            f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="data:text/plain;base64,{pssh}",'
            'KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",KEYFORMATVERSIONS="1"',
            "#EXTINF:6.0,",
            "seg0.ts",
            "#EXT-X-ENDLIST",
        ]
    )


def test_hls_descriptor_alone_reaches_the_media_playlist_keys():
    session = SimpleNamespace(get=lambda url: SimpleNamespace(text=_media_playlist()))
    track = SimpleNamespace(
        id="vid", url="https://cdn.example.invalid/media.m3u8", descriptor=Track.Descriptor.HLS, data={}, drm=None
    )

    handlers.ensure_track_drm(track, session)

    assert track.drm and isinstance(track.drm[0], Widevine), "media playlist DRM not read"
    assert track.drm[0].kids == [KID]
    assert base64.b64decode(handlers.extract_pssh_from_track(track, "widevine"))
