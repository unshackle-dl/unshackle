"""The CENC reader finds the systems and the track's own KID in init data and in HLS keys."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import requests
from pymp4.parser import Box
from pyplayready.system.wrmheader import WRMHeader
from pywidevine.pssh import PSSH

from tests.core.test_hls_kid_from_init import KEY_LINE, key_of, no_kid_pssh
from tests.core.test_kid_windows import box, encv
from unshackle.core.drm import PlayReady, Widevine, cenc
from unshackle.core.drm.verify import PIFF_TENC
from unshackle.core.tracks import Video

TRACK_KID = UUID(int=0xA)
OTHER_KID = UUID(int=0xB)
PR_KEYFORMAT = "urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95"


@pytest.fixture(autouse=True)
def no_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cenc, "ffprobe", lambda data: {})


def playready_box(kid: UUID) -> bytes:
    kid_b64 = base64.b64encode(kid.bytes_le).decode()
    header = WRMHeader(
        (
            '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="4.3.0.0"><DATA>'
            f'<PROTECTINFO><KIDS><KID ALGID="AESCTR" VALUE="{kid_b64}"></KID></KIDS></PROTECTINFO></DATA></WRMHEADER>'
        ).encode("utf-16-le")
    )
    pro = base64.b64decode(PlayReady.pro_b64_for(header))
    return Box.build({"type": b"pssh", "version": 0, "flags": 0, "system_ID": cenc.PLAYREADY_ID, "init_data": pro})


def init(*boxes: bytes) -> bytes:
    return box(b"moov", box(b"trak", *boxes))


def test_init_kid_reads_the_first_real_tenc() -> None:
    assert cenc.init_kid(init(encv(UUID(int=0)), encv(TRACK_KID))) == TRACK_KID


def test_init_kid_reads_a_piff_tenc() -> None:
    assert cenc.init_kid(init(encv(TRACK_KID, piff=True))) == TRACK_KID


def test_init_kid_prefers_the_ffprobe_kid(monkeypatch: pytest.MonkeyPatch) -> None:
    enc_key_id = base64.b64encode(OTHER_KID.bytes).decode()
    monkeypatch.setattr(cenc, "ffprobe", lambda data: {"streams": [{"tags": {"enc_key_id": enc_key_id}}]})
    assert cenc.init_kid(init(encv(TRACK_KID))) == OTHER_KID


def test_init_kid_skips_a_clear_piff_tenc() -> None:
    clear = box(b"uuid", PIFF_TENC, bytes(8) + TRACK_KID.bytes)
    assert cenc.init_kid(init(clear, encv(OTHER_KID, piff=True))) == OTHER_KID


def test_init_kid_is_none_without_a_real_kid() -> None:
    assert cenc.init_kid(init(encv(UUID(int=0)))) is None


def test_widevine_from_init_data_takes_the_piff_kid_when_the_pssh_names_none() -> None:
    pssh = PSSH(no_kid_pssh()).dump()
    drm = Widevine.from_init_data(init(pssh, encv(TRACK_KID, piff=True)))
    assert drm.kids == [TRACK_KID]


def test_playready_from_init_data_keeps_the_piff_kid_as_the_track_kid() -> None:
    drm = PlayReady.from_init_data(init(playready_box(OTHER_KID), encv(TRACK_KID, piff=True)))
    drm.content_keys = {TRACK_KID: "00" * 16, OTHER_KID: "11" * 16}
    assert drm.kids == [OTHER_KID, TRACK_KID]
    assert drm.own_kids() == [TRACK_KID]


def test_from_init_data_without_a_pssh_of_the_system_raises() -> None:
    with pytest.raises(Widevine.Exceptions.PSSHNotFound):
        Widevine.from_init_data(init(playready_box(OTHER_KID), encv(TRACK_KID)))


@pytest.mark.parametrize(
    ("keyformat", "system"),
    [
        ("urn:uuid:EDEF8BA9-79D6-4ACE-A3C8-27DCD51D21ED", cenc.WIDEVINE_ID),
        (PR_KEYFORMAT, cenc.PLAYREADY_ID),
        ("com.microsoft.playready", cenc.PLAYREADY_ID),
        ("identity", None),
        (None, None),
    ],
)
def test_key_system(keyformat: str | None, system: UUID | None) -> None:
    assert cenc.key_system(SimpleNamespace(keyformat=keyformat)) == system


def test_hls_key_drm_takes_the_tag_kid_before_the_init_kid() -> None:
    drm = cenc.hls_key_drm(key_of(no_kid_pssh(), f",KEYID=0x{TRACK_KID.hex}"), kid=OTHER_KID)
    assert isinstance(drm, Widevine)
    assert drm.kids == [TRACK_KID]


def test_hls_key_drm_replaces_placeholder_kids_with_the_init_kid() -> None:
    placeholder = PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[UUID(int=0)]).dumps()
    drm = cenc.hls_key_drm(key_of(placeholder), kid=TRACK_KID)
    assert isinstance(drm, Widevine)
    assert drm.kids == [TRACK_KID]


def test_hls_key_drm_ignores_the_init_kid_when_the_pssh_names_a_real_kid() -> None:
    real = PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[OTHER_KID]).dumps()
    drm = cenc.hls_key_drm(key_of(real), kid=TRACK_KID)
    assert isinstance(drm, Widevine)
    assert drm.kids == [OTHER_KID]


def test_hls_key_drm_is_none_for_another_keyformat() -> None:
    assert cenc.hls_key_drm(SimpleNamespace(keyformat="identity", uri="x")) is None


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(",KEYID=0xZZZ", id="bad-hex"),
        pytest.param(f",KEYID=0x{UUID(int=0).hex}", id="zero"),
        pytest.param(f",KEYID=0x{TRACK_KID.hex},KID=0x{OTHER_KID.hex}", id="keyid-and-kid"),
        pytest.param(',PSSH="x"', id="pssh-attribute"),
    ],
)
def test_hls_key_drm_falls_back_on_an_unusable_tag_kid(extra: str) -> None:
    real = PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[OTHER_KID]).dumps()
    drm = cenc.hls_key_drm(key_of(real, extra))
    assert isinstance(drm, Widevine)
    assert drm.kids[0] == OTHER_KID
    assert UUID(int=0) not in drm.kids


class RouteSession(requests.Session):
    def __init__(self, routes: dict[str, bytes]) -> None:
        super().__init__()
        self.routes = routes

    def get(self, url: str | bytes, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        res = requests.Response()
        res.status_code = 200
        res._content = self.routes[str(url).rsplit("/", 1)[-1]]
        res.encoding = "utf-8"
        return res


def playlist_track(text: str, init_data: bytes = b"") -> Video:
    track = Video.__new__(Video)
    track.id = "test"
    track.url = "https://example.test/video.m3u8"
    track.descriptor = Video.Descriptor.HLS
    track.drm = None
    track.needs_drm_loading = True
    track.session = RouteSession({"video.m3u8": text.encode(), "init.mp4": init_data})
    return track


def test_load_drm_from_playlist_takes_the_tag_kid() -> None:
    line = KEY_LINE.format(pssh=no_kid_pssh()) + f",KEYID=0x{TRACK_KID.hex}"
    track = playlist_track(f"#EXTM3U\n{line}\n#EXTINF:6.0,\na.m4s\n#EXT-X-ENDLIST\n")

    assert track.load_drm_from_playlist() is True
    assert track.drm is not None
    assert [drm.kids for drm in track.drm] == [[TRACK_KID]]


def test_load_drm_from_playlist_takes_the_init_kid_when_the_tag_and_pssh_have_none() -> None:
    line = KEY_LINE.format(pssh=no_kid_pssh())
    text = f'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n{line}\n#EXTINF:6.0,\na.m4s\n#EXT-X-ENDLIST\n'
    track = playlist_track(text, init(encv(TRACK_KID, piff=True)))

    assert track.load_drm_from_playlist() is True
    assert track.drm is not None
    assert [drm.kids for drm in track.drm] == [[TRACK_KID]]


def kid_pssh(kid: UUID) -> str:
    return str(PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[kid]).dumps())


def test_load_drm_from_playlist_reads_the_session_keys_and_skips_a_bad_key() -> None:
    bad = KEY_LINE.format(pssh="AAAA")
    session = KEY_LINE.format(pssh=kid_pssh(TRACK_KID)).replace("#EXT-X-KEY", "#EXT-X-SESSION-KEY")
    track = playlist_track(f"#EXTM3U\n{session}\n{bad}\n#EXTINF:6.0,\na.m4s\n#EXT-X-ENDLIST\n")

    assert track.load_drm_from_playlist() is True
    assert track.drm is not None
    assert [drm.kids for drm in track.drm] == [[TRACK_KID]]
    assert track.needs_drm_loading is False


def test_load_drm_from_playlist_keeps_every_key_once() -> None:
    line = KEY_LINE.format(pssh=kid_pssh(TRACK_KID))
    other = KEY_LINE.format(pssh=kid_pssh(OTHER_KID))
    iso = f'#EXT-X-KEY:METHOD=ISO-23001-7,URI="data:text/plain;base64,{base64.b64encode(OTHER_KID.bytes).decode()}"'
    session = line.replace("#EXT-X-KEY", "#EXT-X-SESSION-KEY")
    track = playlist_track(
        f"#EXTM3U\n{session}\n{line}\n#EXTINF:6.0,\na.m4s\n{other}\n#EXTINF:6.0,\nb.m4s\n{iso}\n#EXTINF:6.0,\nc.m4s\n"
    )

    assert track.load_drm_from_playlist() is True
    assert track.drm is not None
    assert [drm.kids for drm in track.drm] == [[TRACK_KID], [OTHER_KID], [OTHER_KID]]


def test_load_drm_from_playlist_raises_when_every_key_fails() -> None:
    track = playlist_track(f"#EXTM3U\n{KEY_LINE.format(pssh='AAAA')}\n#EXTINF:6.0,\na.m4s\n#EXT-X-ENDLIST\n")

    with pytest.raises(ValueError, match="Failed to load DRM from playlist"):
        track.load_drm_from_playlist()


def test_load_drm_from_playlist_uses_the_given_session_when_the_track_has_none() -> None:
    track = playlist_track(f"#EXTM3U\n{KEY_LINE.format(pssh=kid_pssh(TRACK_KID))}\n#EXTINF:6.0,\na.m4s\n")
    given, track.session = track.session, None

    assert track.load_drm_from_playlist(given) is True
    assert track.drm is not None


def test_from_track_reads_the_playlist_pssh_and_the_init_kid() -> None:
    pssh_b64 = base64.b64encode(playready_box(OTHER_KID)).decode()
    line = (
        f'#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,URI="data:text/plain;charset=UTF-16;base64,{pssh_b64}",'
        f'KEYFORMAT="{PR_KEYFORMAT}",KEYFORMATVERSIONS="1"'
    )
    track = playlist_track(f"#EXTM3U\n{line}\n#EXTINF:6.0,\na.m4s\n#EXT-X-ENDLIST\n")
    track.get_init_segment = lambda **kwargs: init(encv(TRACK_KID, piff=True))  # type: ignore[method-assign]

    drm = PlayReady.from_track(track, track.session)
    assert drm.kids == [OTHER_KID, TRACK_KID]
    with pytest.raises(Widevine.Exceptions.PSSHNotFound):
        Widevine.from_track(track, track.session)
