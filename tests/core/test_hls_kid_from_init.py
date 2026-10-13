"""An HLS Widevine key whose PSSH holds a content id and no Key ID takes the Key ID of its init segment."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import m3u8
from pywidevine.license_protocol_pb2 import WidevinePsshData
from pywidevine.pssh import PSSH

from unshackle.core.constants import DOWNLOAD_LICENCE_ONLY
from unshackle.core.drm import PlayReady, Widevine
from unshackle.core.import_service import hls_playlist_kids
from unshackle.core.manifests.hls import HLS
from unshackle.core.tracks import Video
from unshackle.core.tracks.track import DownloadContext, Track

INIT_KID = UUID("11111111-1111-1111-1111-111111111111")
PSSH_KID = UUID("22222222-2222-2222-2222-222222222222")
TAG_KID = UUID("33333333-3333-3333-3333-333333333333")

ZERO_KID = UUID(int=0)


def no_kid_pssh(content_id: bytes = b"content-id-only") -> str:
    return PSSH.new(
        system_id=PSSH.SystemId.Widevine,
        init_data=WidevinePsshData(content_id=content_id).SerializeToString(),
    ).dumps()


NO_KID_PSSH = no_kid_pssh()
KEY_LINE = (
    '#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,URI="data:text/plain;base64,{pssh}",'
    'KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",KEYFORMATVERSIONS="1"'
)


def keyed_segment(pssh: str, extra: str = "", init: str = "init.mp4") -> str:
    return f'#EXT-X-MAP:URI="{init}"\n{KEY_LINE.format(pssh=pssh)}{extra}\n#EXTINF:6.0,\n{init}.m4s\n'


def playlist(*segments: str) -> str:
    body = "#EXT-X-DISCONTINUITY\n".join(segments)
    return f"#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:6\n{body}#EXT-X-ENDLIST\n"


def key_of(pssh: str, extra: str = "") -> m3u8.Key:
    return m3u8.loads(playlist(keyed_segment(pssh, extra))).keys[0]


def licence_only(text: str, tmp_path: Path) -> list[tuple[list[UUID], Any]]:
    """Run a licence-only download of the playlist and return the Key IDs of each licence call."""
    media = tmp_path / "media.m3u8"
    media.write_text(text, encoding="utf-8")
    track = Video(
        url="https://example.test/media.m3u8",
        language="en",
        descriptor=Track.Descriptor.HLS,
        codec=Video.Codec.AVC,
    )
    track.from_file = media
    licensed: list[tuple[list[UUID], Any]] = []
    ctx = DownloadContext(
        save_path=tmp_path / "video.mp4",
        save_dir=tmp_path / "video_segments",
        progress=lambda **_: None,
        license_widevine=lambda drm, track_kid=None: licensed.append((drm.kids, track_kid)),
        cdm=None,
    )
    DOWNLOAD_LICENCE_ONLY.set()
    try:
        HLS.download_track(track=track, ctx=ctx)
    finally:
        DOWNLOAD_LICENCE_ONLY.clear()
    return licensed


def test_download_track_licenses_with_the_init_segment_kid(tmp_path: Path, monkeypatch: Any) -> None:
    # the probe itself needs the network and FFprobe; the order of the calls is under test
    monkeypatch.setattr(HLS, "get_track_kid_from_init", staticmethod(lambda *args, **kwargs: INIT_KID))
    assert licence_only(playlist(keyed_segment(NO_KID_PSSH)), tmp_path) == [([INIT_KID], INIT_KID)]


def test_licence_only_reads_each_later_key_from_its_own_init_segment(tmp_path: Path, monkeypatch: Any) -> None:
    def probe(master: Any, track: Any, session: Any, init_section: Any = None) -> UUID:
        return PSSH_KID if init_section is not None and init_section.uri == "init2.mp4" else INIT_KID

    monkeypatch.setattr(HLS, "get_track_kid_from_init", staticmethod(probe))
    text = playlist(keyed_segment(no_kid_pssh(b"first")), keyed_segment(no_kid_pssh(b"later"), init="init2.mp4"))
    assert licence_only(text, tmp_path) == [([INIT_KID], INIT_KID), ([PSSH_KID], PSSH_KID)]


def test_get_drm_keeps_a_pssh_that_has_its_own_kid() -> None:
    pssh = PSSH.new(key_ids=[PSSH_KID], system_id=PSSH.SystemId.Widevine).dumps()
    drm = HLS.get_drm(key_of(pssh), kid=INIT_KID)
    assert drm.kids == [PSSH_KID]
    assert drm.pssh.dumps() == pssh


def test_get_drm_prefers_the_tag_keyid_over_the_init_segment_kid() -> None:
    drm = HLS.get_drm(key_of(NO_KID_PSSH, f",KEYID=0x{TAG_KID.hex}"), kid=INIT_KID)
    assert drm.kids == [TAG_KID]


def test_get_drm_replaces_a_placeholder_pssh_kid_and_ignores_a_placeholder_init_kid() -> None:
    placeholder = PSSH.new(key_ids=[ZERO_KID], system_id=PSSH.SystemId.Widevine).dumps()
    assert HLS.get_drm(key_of(placeholder), kid=INIT_KID).kids == [INIT_KID]
    assert HLS.get_drm(key_of(placeholder), kid=ZERO_KID).kids == [ZERO_KID]


def test_session_key_without_a_kid_does_not_stop_the_parse() -> None:
    with_kid = PSSH.new(key_ids=[PSSH_KID], system_id=PSSH.SystemId.Widevine).dumps()
    drm = HLS.get_all_drm([key_of(NO_KID_PSSH), key_of(with_kid)])
    assert [d.kids for d in drm] == [[PSSH_KID]]


def test_alternate_drm_builds_the_other_system_with_the_init_segment_kid() -> None:
    chosen = object.__new__(PlayReady)
    alternate = HLS.alternate_drm([key_of(NO_KID_PSSH)], chosen, kid=INIT_KID)
    assert [type(d) for d in alternate] == [Widevine] and alternate[0].kids == [INIT_KID]


def test_import_reads_the_kid_of_a_playlist_whose_key_has_none() -> None:
    def get(url: str, headers: Any = None) -> Any:
        return SimpleNamespace(text=playlist(keyed_segment(NO_KID_PSSH)), ok=True, content=b"init")

    track = SimpleNamespace(id="vid", url="https://example.test/media.m3u8", get_key_id=lambda init: INIT_KID)
    assert hls_playlist_kids(track, SimpleNamespace(get=get)) == {INIT_KID}
