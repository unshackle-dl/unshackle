from __future__ import annotations

from typing import Any
from uuid import UUID

import click
import pytest
import requests
from pywidevine.exceptions import InvalidLicenseMessage
from pywidevine.pssh import PSSH

from unshackle.core.cdm.custom_remote_cdm import CustomRemoteCDMExceptions
from unshackle.core.cdm.decrypt_labs_remote_cdm import DecryptLabsRemoteCDMExceptions
from unshackle.core.drm import ClearKeyCENC, PlayReady, Widevine
from unshackle.core.utilities import declared_kwargs

KID = UUID("00112233-4455-6677-8899-aabbccddeeff")
KEY = "00000000000000000000000000000001"


class FakeKey:
    def __init__(self, kid: UUID, key: str) -> None:
        self.kid = kid
        self.key = bytes.fromhex(key)
        self.type = "CONTENT"


class FakeCdm:
    """The smallest CDM the licensing flow accepts; records what it was given."""

    service_certificate_challenge = b"cert-challenge"

    def __init__(self, challenge: bytes = b"challenge") -> None:
        self._challenge = challenge
        self.parsed: list[Any] = []

    def open(self) -> str:
        return "session-1"

    def close(self, session_id: str) -> None:
        pass

    def get_license_challenge(self, session_id: str, pssh: PSSH) -> bytes:
        return self._challenge

    def parse_license(self, session_id: str, response: Any) -> None:
        self.parsed.append(response)

    def get_keys(self, session_id: str, type_: str) -> list[FakeKey]:
        return [FakeKey(KID, KEY)]


def _drm() -> Widevine:
    return Widevine(pssh=PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[KID]), kid=KID)


def _licence_for(fn: Any) -> Any:
    """Build the callable dl.py gives the DRM object, filtered to what the service declares."""
    return lambda **kw: fn(**declared_kwargs(fn, {**kw, "title": "t", "track": "tr"}))


def test_a_session_aware_service_gets_the_open_session_id() -> None:
    seen: dict[str, Any] = {}

    def get_widevine_license(*, challenge: bytes, title: str, track: str, session_id: Any = None) -> bytes:
        seen.update(challenge=challenge, title=title, track=track, session_id=session_id)
        return b"license"

    drm = _drm()
    cdm = FakeCdm()
    drm.get_content_keys(cdm=cdm, certificate=lambda **_: None, licence=_licence_for(get_widevine_license))

    assert seen["session_id"] == "session-1"
    assert "pssh" not in seen
    assert drm.content_keys == {KID: KEY}


def test_a_plain_service_does_not_get_pssh_or_session_id() -> None:
    seen: dict[str, Any] = {}

    def get_widevine_license(*, challenge: bytes, title: str, track: str) -> bytes:
        seen.update(challenge=challenge, title=title, track=track)
        return b"license"

    drm = _drm()
    drm.get_content_keys(cdm=FakeCdm(), certificate=lambda **_: None, licence=_licence_for(get_widevine_license))

    assert set(seen) == {"challenge", "title", "track"}
    assert drm.content_keys == {KID: KEY}


def test_an_empty_challenge_raises_instead_of_licensing() -> None:
    def get_widevine_license(*, challenge: bytes, title: str, track: str) -> bytes:
        raise AssertionError("the licence request must not go out for an empty challenge")

    drm = _drm()
    with pytest.raises(Widevine.Exceptions.EmptyLicense):
        drm.get_content_keys(
            cdm=FakeCdm(challenge=b""), certificate=lambda **_: None, licence=_licence_for(get_widevine_license)
        )


@pytest.mark.parametrize(
    "error",
    [
        InvalidLicenseMessage("Could not parse license_message as a SignedMessage"),
        CustomRemoteCDMExceptions.InvalidLicenseMessage("bad"),
        DecryptLabsRemoteCDMExceptions.InvalidLicenseMessage("bad"),
        requests.RequestException("License decrypt failed: 400"),
    ],
)
def test_a_licence_the_cdm_cannot_parse_is_one_line(error: Exception) -> None:
    class JunkCdm(FakeCdm):
        def parse_license(self, session_id: str, response: Any) -> None:
            raise error

    with pytest.raises(click.ClickException, match=KID.hex):
        _drm().get_content_keys(JunkCdm(), certificate=lambda **_: None, licence=lambda **_: b"junk licence")


@pytest.mark.parametrize("drm", [Widevine, PlayReady, ClearKeyCENC])
def test_a_licence_with_no_usable_key_is_one_line(drm: Any) -> None:
    assert issubclass(drm.Exceptions.EmptyLicense, click.ClickException)
    assert issubclass(drm.Exceptions.CEKNotFound, click.ClickException)
