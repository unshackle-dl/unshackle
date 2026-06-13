"""Tests for the FairPlay->PlayReady KID bridge.

FairPlay/cbcs HLS ships a tenc DEFAULT_KID but no PlayReady/Widevine PSSH.
``build_pr_header_from_kid`` synthesizes a PlayReady Object (PRO) from that bare
KID so a PlayReady license can be requested keyed on it. These pins ensure the
synthesized header parses back through pyplayready and yields the same KID.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from pyplayready.system.pssh import PSSH

from unshackle.core.drm.playready import PlayReady, build_pr_header_from_kid


@pytest.mark.parametrize(
    "kid",
    [
        UUID("279926a3-d9b9-4b6f-8b2c-1a2b3c4d5e6f"),
        UUID("00000000-0000-0000-0000-000000000001"),
        UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
    ],
)
def test_synth_header_parses_and_round_trips_kid(kid: UUID) -> None:
    pssh_b64 = build_pr_header_from_kid(kid)

    # pyplayready parses the synthesized PRO as a single WRMHEADER record.
    parsed = PSSH(pssh_b64)
    assert len(parsed.wrm_headers) == 1

    # PlayReady DRM object recovers the original KID from the synthesized header.
    drm = PlayReady(pssh=parsed, pssh_b64=pssh_b64)
    assert [k.hex for k in drm.kids] == [kid.hex]


def test_synth_header_is_v4_0_aesctr() -> None:
    import base64

    pssh_b64 = build_pr_header_from_kid(UUID("279926a3-d9b9-4b6f-8b2c-1a2b3c4d5e6f"))
    xml = base64.b64decode(pssh_b64).decode("utf-16-le", errors="ignore")
    assert "PlayReadyHeader" in xml
    assert 'version="4.0.0.0"' in xml
    assert "<ALGID>AESCTR</ALGID>" in xml


def test_cbcs_header_v4_3_round_trips_kid() -> None:
    kid = UUID("279926a3-d9b9-4b6f-8b2c-1a2b3c4d5e6f")
    pssh_b64 = build_pr_header_from_kid(kid, scheme="cbcs", version="4.3")
    drm = PlayReady(pssh=PSSH(pssh_b64), pssh_b64=pssh_b64)
    assert [k.hex for k in drm.kids] == [kid.hex]


def test_cbcs_requires_v4_3() -> None:
    with pytest.raises(ValueError, match="4.3"):
        build_pr_header_from_kid(UUID(int=1), scheme="cbcs", version="4.0")
