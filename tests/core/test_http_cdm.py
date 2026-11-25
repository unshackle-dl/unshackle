"""The shared bookkeeping of the HTTP CDMs: sessions, vault and cached keys, and their merge with licence keys."""

from __future__ import annotations

import base64
from typing import Any
from uuid import UUID

import pytest
from pywidevine.cdm import Cdm as WidevineCdm
from pywidevine.pssh import PSSH

from unshackle.core.cdm.custom_remote_cdm import CustomRemoteCDM, CustomRemoteCDMExceptions
from unshackle.core.cdm.decrypt_labs_remote_cdm import DecryptLabsRemoteCDM, DecryptLabsRemoteCDMExceptions
from unshackle.core.cdm.http_cdm import HttpCdm, HttpCdmExceptions
from unshackle.core.drm import Widevine

pytestmark = pytest.mark.unit

KID_A = UUID("00112233445566778899aabbccddeeff")
KID_B = UUID("ffeeddccbbaa99887766554433221100")
KEY_A, KEY_B, KEY_X = "01" * 16, "02" * 16, "0f" * 16


class FakeVaults:
    def __init__(self, keys: dict[UUID, str]) -> None:
        self.keys = dict(keys)
        self.added: dict[Any, str] = {}

    def __len__(self) -> int:
        return 1

    def get_key(self, kid: UUID) -> tuple[str | None, None]:
        return self.keys.get(kid), None

    def add_keys(self, keys: dict[Any, str]) -> None:
        self.added.update(keys)


class StubCdm(HttpCdm):
    """An adapter whose API answers from fixed lists and records what it was asked."""

    def __init__(
        self,
        cached: list[dict[str, Any]] | None = None,
        licence: list[dict[str, Any]] | None = None,
        vaults: Any = None,
    ) -> None:
        super().__init__("https://cdm.test/", "L3", None, "EXAMPLE", vaults, {})
        self.cached = cached
        self.licence = licence or []
        self.challenges = 0
        self.required: list[list[str] | None] = []
        self.bodies: list[dict[str, Any]] = []

    def _request_challenge(
        self,
        session: dict[str, Any],
        body: dict[str, Any],
        license_type: str,
        privacy_mode: bool,
    ) -> bytes:
        self.challenges += 1
        self.required.append(session.get("required_kids"))
        if self.cached is not None and self._take_cached_keys(session, self.cached):
            return b""
        return self._keep_challenge(session, b"challenge", "remote-1")

    def _request_keys(self, session: dict[str, Any], body: dict[str, Any]) -> list[dict[str, Any]]:
        self.bodies.append(body)
        return self.licence


def key(kid: UUID, value: str) -> dict[str, str]:
    return {"kid": kid.hex, "key": value, "type": "CONTENT"}


def licensed(cdm: StubCdm, *kids: UUID) -> dict[UUID, str]:
    """Run one challenge and licence round and return the content keys of the session."""
    session_id = cdm.open()
    cdm.set_required_kids(list(kids), session_id=session_id)
    if cdm.get_license_challenge(session_id, PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=list(kids))):
        cdm.parse_license(session_id, b"licence")
    return {k.kid: k.key.hex() for k in cdm.get_keys(session_id, "CONTENT")}


def test_vaults_holding_every_kid_skip_the_api() -> None:
    cdm = StubCdm(vaults=FakeVaults({KID_A: KEY_A, KID_B: KEY_B}))
    assert licensed(cdm, KID_A, KID_B) == {KID_A: KEY_A, KID_B: KEY_B}
    assert cdm.challenges == 0


def test_a_vault_key_wins_over_the_licence_and_licence_keys_go_to_the_vaults() -> None:
    vaults = FakeVaults({KID_A: KEY_A})
    cdm = StubCdm(licence=[key(KID_A, KEY_X), key(KID_B, KEY_B), key(KID_B, KEY_X)], vaults=vaults)
    assert licensed(cdm, KID_A, KID_B) == {KID_A: KEY_A, KID_B: KEY_B}
    assert vaults.added == {KID_A: KEY_A, KID_B: KEY_B}


def test_cached_keys_covering_every_kid_need_no_licence() -> None:
    cdm = StubCdm(cached=[key(KID_A, KEY_A)])
    assert licensed(cdm, KID_A) == {KID_A: KEY_A}
    assert cdm.bodies == []


def test_partial_cached_keys_merge_with_the_licence() -> None:
    cdm = StubCdm(cached=[key(KID_A, KEY_A)], licence=[key(KID_A, KEY_X), key(KID_B, KEY_B)])
    assert licensed(cdm, KID_A, KID_B) == {KID_A: KEY_A, KID_B: KEY_B}
    assert cdm.bodies[0]["session_id"] == "remote-1"


def test_an_unknown_session_raises_the_shared_exception() -> None:
    cdm = StubCdm()
    session_id = cdm.open()
    cdm.close(session_id)
    with pytest.raises(CustomRemoteCDMExceptions.InvalidSession):
        cdm.get_keys(session_id)
    assert CustomRemoteCDMExceptions is DecryptLabsRemoteCDMExceptions is HttpCdmExceptions


def test_widevine_skips_the_licence_when_the_cdm_holds_cached_keys() -> None:
    def licence(**_: Any) -> bytes:
        raise AssertionError("no licence request when the cache holds every key")

    drm = Widevine(pssh=PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[KID_A]), kid=KID_A)
    cdm = StubCdm(cached=[key(KID_A, KEY_A)])
    drm.get_content_keys(cdm, certificate=lambda **_: None, licence=licence)
    assert drm.content_keys == {KID_A: KEY_A}
    assert cdm.required == [[KID_A.hex]]


@pytest.mark.parametrize(
    ("cdm", "privacy_cert"),
    [
        (DecryptLabsRemoteCDM(secret="s", device_name="L2"), True),
        (DecryptLabsRemoteCDM(secret="s", device_name="L3"), False),
        (CustomRemoteCDM(host="https://cdm.test", device={"name": "L1"}), True),
        (CustomRemoteCDM(host="https://cdm.test", device={"name": "L2"}), False),
    ],
)
def test_only_the_listed_devices_get_the_common_privacy_certificate(cdm: HttpCdm, privacy_cert: bool) -> None:
    session_id = cdm.open()
    cdm.set_service_certificate(session_id, None)
    assert (cdm.get_service_certificate(session_id) is not None) is privacy_cert


class L1StubCdm(StubCdm):
    privacy_cert_devices = ("L3",)

    def __init__(self) -> None:
        super().__init__(licence=[key(KID_A, KEY_A)])
        self.sent: list[dict[str, Any]] = []

    def _request_challenge(self, session: dict[str, Any], body: dict[str, Any], *rest: Any) -> bytes:
        self.sent.append(body)
        return super()._request_challenge(session, body, *rest)


def test_widevine_gives_a_listed_device_the_common_privacy_certificate_when_the_service_has_none() -> None:
    drm = Widevine(pssh=PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[KID_A]), kid=KID_A)
    cdm = L1StubCdm()
    drm.get_content_keys(cdm, certificate=lambda **_: None, licence=lambda **_: b"licence")
    assert base64.b64decode(cdm.sent[0]["service_certificate"]) == base64.b64decode(WidevineCdm.common_privacy_cert)
    assert drm.content_keys == {KID_A: KEY_A}
