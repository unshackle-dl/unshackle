"""Tests for the DASH/W3C EME ClearKey (``org.w3.clearkey``) DRM system.

Covers the three seams of the native ClearKey flow:
- ``DASH.get_drm`` emitting a ``ClearKeyCENC`` from a clearkey ContentProtection
  element (KID from own attrs or sibling mp4protection, Laurl namespace variants)
- ``ClearKeyCENC.get_content_keys`` building the W3C JSON license request and
  parsing the JWK Set response (dict/str/bytes, unpadded base64url)
- ``to_dict`` / ``drm_from_dict`` round-trip for the --export/import path
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional
from uuid import UUID

import pytest
# lxml.etree: XML parser used to build ContentProtection fixtures for DASH.get_drm
from lxml import etree

from unshackle.core.drm import drm_from_dict
from unshackle.core.drm.clearkey_cenc import ClearKeyCENC
from unshackle.core.manifests.dash import DASH

KID = UUID("9eb4050d-e44b-4802-932e-27d75083e266")
KEY = bytes.fromhex("ccd0064c43f7e9fcbaa9b12af3fd1f40")
LAURL = "https://license.example.test/clearkey"

CLEARKEY_URN = "urn:uuid:e2719d58-a985-b3c9-781a-b030af78d30e"
CENC_NS = "urn:mpeg:cenc:2013"


def b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwk_set() -> dict:
    return {
        "keys": [{"kty": "oct", "kid": b64url_nopad(KID.bytes), "k": b64url_nopad(KEY)}],
        "type": "temporary",
    }


def protection(xml: str) -> etree._Element:
    return etree.fromstring(xml.encode("utf8"))


class StubLicence:
    """Callable license stub that records the challenge it was given."""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.challenge: Optional[bytes] = None

    def __call__(self, *, challenge: bytes) -> Any:
        self.challenge = challenge
        return self.response


class StubResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class StubSession:
    """Records the POST that the laurl fallback makes."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.url: Optional[str] = None
        self.data: Optional[bytes] = None

    def post(self, url: str, data: Any = None, **_: Any) -> StubResponse:
        self.url = url
        self.data = data
        return StubResponse(self.content)


def test_get_drm_parses_clearkey_contentprotection() -> None:
    elem = protection(
        f'<ContentProtection xmlns:cenc="{CENC_NS}" xmlns:dashif="https://dashif.org/CPS" '
        f'schemeIdUri="{CLEARKEY_URN}" value="ClearKey1.0" cenc:default_KID="{KID}">'
        f"<dashif:Laurl>{LAURL}</dashif:Laurl>"
        f"</ContentProtection>"
    )

    drm = DASH.get_drm([elem])

    assert len(drm) == 1
    assert isinstance(drm[0], ClearKeyCENC)
    assert drm[0].kids == [KID]
    assert drm[0].laurl == LAURL


@pytest.mark.parametrize(
    "laurl_xml",
    [
        f'<dashif:Laurl xmlns:dashif="https://dashif.org/CPS">{LAURL}</dashif:Laurl>',
        f'<ck:Laurl xmlns:ck="http://dashif.org/guidelines/clearKey" Lic_type="EME-1.0">{LAURL}</ck:Laurl>',
        f"<laurl>{LAURL}</laurl>",
    ],
    ids=["dashif-cps", "legacy-clearkey-ns", "bare-lowercase"],
)
def test_get_drm_clearkey_laurl_variants(laurl_xml: str) -> None:
    elem = protection(
        f'<ContentProtection xmlns:cenc="{CENC_NS}" schemeIdUri="{CLEARKEY_URN}" cenc:default_KID="{KID}">'
        f"{laurl_xml}"
        f"</ContentProtection>"
    )

    drm = DASH.get_drm([elem])

    assert len(drm) == 1
    assert drm[0].laurl == LAURL


def test_get_drm_clearkey_kid_from_sibling_mp4protection() -> None:
    # Canonical DASH-IF shape: default_KID on the mp4protection element only.
    clearkey = protection(f'<ContentProtection schemeIdUri="{CLEARKEY_URN}" value="ClearKey1.0"/>')
    mp4protection = protection(
        f'<ContentProtection xmlns:cenc="{CENC_NS}" '
        f'schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc" cenc:default_KID="{KID}"/>'
    )

    drm = DASH.get_drm([mp4protection, clearkey])

    assert len(drm) == 1
    assert isinstance(drm[0], ClearKeyCENC)
    assert drm[0].kids == [KID]
    assert drm[0].laurl is None


def test_get_drm_clearkey_without_any_kid_is_skipped() -> None:
    elem = protection(f'<ContentProtection schemeIdUri="{CLEARKEY_URN}"/>')
    assert DASH.get_drm([elem]) == []


@pytest.mark.parametrize(
    "shape",
    ["dict", "str", "bytes"],
)
def test_get_content_keys_parses_jwk_set(shape: str) -> None:
    response: Any = jwk_set()
    if shape == "str":
        response = json.dumps(response)
    elif shape == "bytes":
        response = json.dumps(response).encode("utf8")

    drm = ClearKeyCENC(kids=[KID])
    drm.get_content_keys(licence=StubLicence(response))

    assert drm.content_keys == {KID: KEY.hex()}


def test_get_content_keys_challenge_shape() -> None:
    licence = StubLicence(jwk_set())
    drm = ClearKeyCENC(kids=[KID])
    drm.get_content_keys(licence=licence)

    assert licence.challenge is not None
    request = json.loads(licence.challenge.decode("utf8"))
    assert request == {"kids": [b64url_nopad(KID.bytes)], "type": "temporary"}
    # W3C EME mandates unpadded base64url key IDs
    assert all("=" not in kid for kid in request["kids"])


def test_get_content_keys_laurl_fallback() -> None:
    session = StubSession(json.dumps(jwk_set()).encode("utf8"))
    drm = ClearKeyCENC(kids=[KID], laurl=LAURL)
    drm.get_content_keys(licence=StubLicence(None), session=session)

    assert session.url == LAURL
    assert session.data is not None
    assert json.loads(session.data.decode("utf8"))["type"] == "temporary"
    assert drm.content_keys == {KID: KEY.hex()}


def test_get_content_keys_no_response_raises_empty_license() -> None:
    drm = ClearKeyCENC(kids=[KID])
    with pytest.raises(ClearKeyCENC.Exceptions.EmptyLicense):
        drm.get_content_keys(licence=StubLicence(None))


def test_get_content_keys_missing_kid_raises_cek_not_found() -> None:
    other_kid = UUID(int=7)
    response = {"keys": [{"kty": "oct", "kid": b64url_nopad(other_kid.bytes), "k": b64url_nopad(KEY)}]}
    drm = ClearKeyCENC(kids=[KID])
    with pytest.raises(ClearKeyCENC.Exceptions.CEKNotFound):
        drm.get_content_keys(licence=StubLicence(response))


def test_get_content_keys_skips_when_already_keyed() -> None:
    licence = StubLicence(jwk_set())
    drm = ClearKeyCENC(kids=[KID], content_keys={KID: KEY.hex()})
    drm.get_content_keys(licence=licence)

    assert licence.challenge is None  # no license round-trip needed


def test_to_dict_roundtrip() -> None:
    drm = ClearKeyCENC(kids=[KID], laurl=LAURL, content_keys={KID: KEY.hex()})
    data = drm.to_dict()
    assert data["system"] == "ClearKeyCENC"

    data["content_keys"] = {kid.hex: key for kid, key in drm.content_keys.items()}
    rebuilt = drm_from_dict(data)

    assert isinstance(rebuilt, ClearKeyCENC)
    assert rebuilt.kids == [KID]
    assert rebuilt.laurl == LAURL
    assert rebuilt.content_keys == {KID: KEY.hex()}
