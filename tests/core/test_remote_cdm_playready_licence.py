"""A remote CDM must send a PlayReady licence to the host as the XML it received.

A licence server may return the SOAP envelope with or without an XML declaration.
``base64.b64decode`` drops every character it cannot read, so XML text can decode
without an error and the host then receives bytes that are not a licence.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from unshackle.core.cdm.custom_remote_cdm import CustomRemoteCDM
from unshackle.core.cdm.decrypt_labs_remote_cdm import DecryptLabsRemoteCDM

pytestmark = pytest.mark.unit

ENVELOPE = (
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
    "<AcquireLicenseResponse><License>AAAA</License></AcquireLicenseResponse>"
    "</soap:Body></soap:Envelope>"
)


class Sent(Exception):
    """Stops ``parse_license`` after the request body has been captured."""


def decrypt_labs_cdm() -> tuple[Any, str]:
    return DecryptLabsRemoteCDM(secret="x", host="https://cdm.test", device_name="SL3"), "decrypt_labs_session_id"


def custom_cdm() -> tuple[Any, str]:
    cdm = CustomRemoteCDM(host="https://cdm.test", device={"name": "SL3000", "type": "PLAYREADY"})
    return cdm, "remote_session_id"


def licence_sent(cdm: Any, remote_id_key: str, licence: str) -> bytes:
    """Run ``parse_license`` on a session that has a challenge, and return the licence bytes it posts."""
    session_id = cdm.open()
    cdm.set_pssh_b64("UFNTSA==", session_id=session_id)
    cdm._sessions[session_id].update({"challenge": b"challenge", remote_id_key: "remote", "pssh": object()})

    body: dict[str, Any] = {}

    def capture(_url: str, json: dict[str, Any], **_: Any) -> None:
        body.update(json)
        raise Sent

    cdm._http_session.post = capture
    with pytest.raises(Sent):
        cdm.parse_license(session_id, licence)
    return base64.b64decode(body["license_response"])


@pytest.mark.parametrize("make_cdm", [decrypt_labs_cdm, custom_cdm])
@pytest.mark.parametrize(
    "licence",
    [
        ENVELOPE,
        '<?xml version="1.0" encoding="utf-8"?>' + ENVELOPE,
        "\n  " + ENVELOPE,
        # Each length makes a different count of base64 characters, so one of them decodes without an error.
        *(ENVELOPE.replace("AAAA", "A" * n) for n in range(5, 8)),
    ],
)
def test_a_playready_xml_licence_is_sent_as_it_is(make_cdm: Any, licence: str) -> None:
    cdm, remote_id_key = make_cdm()
    assert licence_sent(cdm, remote_id_key, licence) == licence.encode("utf-8")


@pytest.mark.parametrize("make_cdm", [decrypt_labs_cdm, custom_cdm])
def test_a_base64_licence_is_still_decoded(make_cdm: Any) -> None:
    cdm, remote_id_key = make_cdm()
    assert licence_sent(cdm, remote_id_key, base64.b64encode(b"binary licence").decode()) == b"binary licence"
