"""A bare UTF-16LE WRMHEADER in place of a PSSH box or PlayReady Object loads as PlayReady DRM."""

from __future__ import annotations

import base64
import logging
from uuid import UUID

import m3u8
import pytest
from lxml import etree
from pymp4.parser import Box
from pyplayready.misc.exceptions import InvalidPssh
from pyplayready.system.pssh import PSSH
from pyplayready.system.wrmheader import WRMHeader
from pywidevine.pssh import PSSH as WV_PSSH

from unshackle.core.api.handlers import pssh_kids
from unshackle.core.drm import PlayReady, Widevine, drm_from_dict
from unshackle.core.manifests.dash import DASH
from unshackle.core.manifests.hls import HLS
from unshackle.core.manifests.ism import ISM

KID = UUID("00112233-4455-6677-8899-aabbccddeeff")
KID_B64 = base64.b64encode(KID.bytes_le).decode()
PR_URN = "urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95"
WV_URN = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"


def bare_header(version: str) -> str:
    """Base64 of a bare WRMHEADER, with the KID where the given header version puts it."""
    if version == "4.0.0.0":
        data = f"<PROTECTINFO><KEYLEN>16</KEYLEN><ALGID>AESCTR</ALGID></PROTECTINFO><KID>{KID_B64}</KID>"
    else:
        data = f'<PROTECTINFO><KIDS><KID ALGID="AESCTR" VALUE="{KID_B64}"></KID></KIDS></PROTECTINFO>'
    xml = (
        f'<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="{version}">'
        f"<DATA>{data}<LA_URL>https://example.invalid/pr</LA_URL></DATA></WRMHEADER>"
    )
    return base64.b64encode(xml.encode("utf-16-le")).decode()


def pr_protection(child: str, value: str) -> etree._Element:
    return etree.fromstring(
        f'<ContentProtection xmlns:cenc="urn:mpeg:cenc:2013" xmlns:mspr="urn:microsoft:playready" '
        f'schemeIdUri="{PR_URN}"><{child}>{value}</{child}></ContentProtection>'
    )


@pytest.mark.parametrize("version", ["4.0.0.0", "4.1.0.0", "4.2.0.0", "4.3.0.0"])
@pytest.mark.parametrize("child", ["mspr:pro", "cenc:pssh"])
def test_dash_loads_a_bare_header(child: str, version: str) -> None:
    drm = DASH.get_drm([pr_protection(child, bare_header(version))])

    assert len(drm) == 1
    assert isinstance(drm[0], PlayReady)
    assert KID in drm[0].kids
    assert drm[0].pssh_b64
    PSSH(drm[0].pssh_b64)
    assert KID in drm_from_dict(drm[0].to_dict()).kids


def test_wrapped_header_bytes_are_unchanged() -> None:
    header = bare_header("4.0.0.0")
    drm = PlayReady.from_pssh_b64(header)

    assert drm.pssh.wrm_headers[0].dumps().encode("utf-16-le") == base64.b64decode(header)


def test_dash_skips_an_unparseable_playready_entry_and_keeps_widevine(caplog: pytest.LogCaptureFixture) -> None:
    widevine = etree.fromstring(
        f'<ContentProtection xmlns:cenc="urn:mpeg:cenc:2013" schemeIdUri="{WV_URN}">'
        f"<cenc:pssh>{WV_PSSH.new(system_id=WV_PSSH.SystemId.Widevine, key_ids=[KID]).dumps()}</cenc:pssh>"
        "</ContentProtection>"
    )
    garbage = pr_protection("mspr:pro", base64.b64encode(b"\x00\x00not a header").decode())

    with caplog.at_level(logging.WARNING, logger="DASH"):
        drm = DASH.get_drm([widevine, garbage])

    assert [type(d) for d in drm] == [Widevine]
    assert "Skipping unparseable PlayReady ContentProtection" in caplog.text


@pytest.mark.parametrize(
    "data",
    [b"\x00\x00not a header", "<WRMHEADER/>".encode("utf-8")],
    ids=["garbage", "utf-8-header"],
)
def test_other_bad_data_keeps_the_original_error(data: bytes) -> None:
    with pytest.raises(InvalidPssh):
        PlayReady.from_pssh_b64(base64.b64encode(data).decode())


def test_hls_loads_a_bare_header() -> None:
    key = m3u8.loads(
        "#EXTM3U\n"
        f'#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,URI="data:text/plain;charset=UTF-16;base64,{bare_header("4.0.0.0")}",'
        'KEYFORMAT="com.microsoft.playready",KEYFORMATVERSIONS="1"\n'
        "#EXTINF:4.0,\nseg0.mp4\n"
    ).keys[0]

    drm = HLS.get_drm(key)

    assert isinstance(drm, PlayReady)
    assert KID in drm.kids


def test_ism_loads_a_bare_header() -> None:
    header = etree.fromstring(f'<ProtectionHeader SystemID="{PR_URN[9:]}">{bare_header("4.0.0.0")}</ProtectionHeader>')

    drm = ISM.get_drm([header])

    assert len(drm) == 1
    assert KID in drm[0].kids


def pr_box(header: bytes, box_kids: tuple[UUID, ...] = ()) -> str:
    """Base64 of a PlayReady PSSH box around ``header``; ``box_kids`` makes it a v1 box."""
    return base64.b64encode(
        Box.build(
            dict(
                type=b"pssh",
                version=1 if box_kids else 0,
                flags=0,
                system_ID=WV_PSSH.SystemId.PlayReady,
                key_IDs=list(box_kids) or None,
                init_data=header,
            )
        )
    ).decode()


@pytest.mark.parametrize("version", ["4.0.0.0", "4.3.0.0"])
@pytest.mark.parametrize("wrap", [False, True], ids=["bare", "pro"])
def test_widevine_converts_a_playready_box_with_the_playready_kids(version: str, wrap: bool) -> None:
    header = base64.b64decode(bare_header(version))
    box = pr_box(base64.b64decode(PlayReady.pro_b64_for(WRMHeader(header))) if wrap else header)

    assert Widevine(pssh=WV_PSSH(box)).kids == [KID]
    assert PlayReady(pssh=PSSH(base64.b64decode(box)), pssh_b64=box).kids == [KID]
    assert pssh_kids(box, "widevine") == {KID}


def test_widevine_keeps_the_kids_of_a_v1_playready_box() -> None:
    other = UUID("ffeeddcc-bbaa-9988-7766-554433221100")
    box = pr_box(base64.b64decode(bare_header("4.0.0.0")), box_kids=(other, KID))

    assert Widevine(pssh=WV_PSSH(box)).kids == [other, KID]
