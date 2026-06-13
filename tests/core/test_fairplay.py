"""Tests for the FairPlay -> PlayReady bridge DRM type and skd KID extraction.

FairPlay HLS (SAMPLE-AES / cbcs) carries a content KID but no PlayReady PSSH. The
``FairPlay`` DRM synthesizes a PlayReady header from that KID so a PlayReady CDM can
license it; it is a PlayReady subclass so the pipeline reuses CDM/decrypt logic and
only differs in routing the request to ``get_fairplay_license``.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

import pytest

from unshackle.core.drm import FairPlay, PlayReady
from unshackle.core.drm.fairplay import fairplay_kid_from_skd


def test_fairplay_is_playready_subclass() -> None:
    # Pipeline relies on isinstance(drm, PlayReady) (get_drm_for_cdm, prepare_drm branch).
    fp = FairPlay.from_kid(UUID("279926a3-d9b9-4b6f-8b2c-1a2b3c4d5e6f"))
    assert isinstance(fp, PlayReady)
    assert isinstance(fp, FairPlay)


def test_from_kid_round_trips_kid_and_sets_pssh() -> None:
    kid = UUID("279926a3-d9b9-4b6f-8b2c-1a2b3c4d5e6f")
    fp = FairPlay.from_kid(kid)
    assert fp.pssh_b64
    assert [k.hex for k in fp.kids] == [kid.hex]


@pytest.mark.parametrize(
    ("skd", "expected"),
    [
        # hyphenated GUID (with optional :IV)
        ("skd://72ea9ec1-abb4-40d8-b3a9-e0bd1833bd58:0123456789ABCDEF", "72ea9ec1abb440d8b3a9e0bd1833bd58"),
        # GUID as a query param
        ("skd://lic.example/fairplay?KID=4376a4b3-d8ef-4f21-9a6b-faa81a2e59e3", "4376a4b3d8ef4f219a6bfaa81a2e59e3"),
        # bare 32-hex path segment
        ("skd://0000000069176cc2af6f93cce3515ced/clip/x", "0000000069176cc2af6f93cce3515ced"),
    ],
)
def test_kid_from_skd_common_forms(skd: str, expected: str) -> None:
    kid = fairplay_kid_from_skd(skd)
    assert kid is not None
    assert kid.hex == expected


@pytest.mark.parametrize(
    "skd",
    [
        "skd://example.com/p123456/c1",  # service-specific encoding, not a GUID/32-hex
        "skd://opaque-asset-token-abc",
        "",
    ],
)
def test_kid_from_skd_returns_none_for_non_kid_forms(skd: str) -> None:
    # Services with non-standard skd encodings derive the KID themselves; the generic
    # helper must not mis-parse them.
    assert fairplay_kid_from_skd(skd) is None


def test_base_get_fairplay_license_delegates_to_playready() -> None:
    captured: dict = {}

    class FakeService:
        # Bind the unbound base methods to a minimal stand-in.
        from unshackle.core.service import Service

        get_fairplay_license = Service.get_fairplay_license

        def get_playready_license(self, *, challenge: bytes, title, track) -> Optional[bytes]:
            captured["called"] = (challenge, title, track)
            return b"LICENSE"

    svc = FakeService()
    out = svc.get_fairplay_license(challenge=b"chal", title="T", track="K")
    assert out == b"LICENSE"
    assert captured["called"] == (b"chal", "T", "K")
