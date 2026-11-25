"""A reused HTTP CDM must not carry one title's PSSH or required KIDs into the next.

``dl.get_cdm`` builds each CDM once per run, so one instance serves every title, and tracks
decrypt in parallel on it. Each CDM session keeps its own PSSH and required KIDs.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

import pytest
from pyplayready.system.wrmheader import WRMHeader

from unshackle.core.cdm.custom_remote_cdm import CustomRemoteCDM
from unshackle.core.cdm.decrypt_labs_remote_cdm import DecryptLabsRemoteCDM
from unshackle.core.drm import PlayReady

pytestmark = pytest.mark.unit

ADAPTERS: list[Callable[[], Any]] = [
    lambda: CustomRemoteCDM(host="https://cdm.test", device={"name": "SL3000", "type": "PLAYREADY"}),
    lambda: DecryptLabsRemoteCDM(secret="x", host="https://cdm.test", device_name="SL3"),
]

HEADER = (
    '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="4.0.0.0">'
    "<DATA><KID>MzMzMzMzMzMzMzMzMzMzMw==</KID></DATA></WRMHEADER>"
)


class FakePssh:
    """A PSSH object shaped like the ones the manifests hand to the CDM."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw


def recording(cdm: Any) -> list[tuple[str, Any]]:
    """Make the adapter record the init data and required KIDs of each challenge instead of sending it."""
    sent: list[tuple[str, Any]] = []

    def request_challenge(session: Any, body: Any, *_: Any) -> bytes:
        sent.append((body["init_data"], session.get("required_kids")))
        return b"challenge"

    cdm._request_challenge = request_challenge
    return sent


@pytest.mark.parametrize("make_cdm", ADAPTERS)
def test_a_later_session_does_not_inherit_the_earlier_pssh_or_kids(make_cdm: Callable[[], Any]) -> None:
    cdm = make_cdm()
    assert cdm.is_playready
    sent = recording(cdm)

    first = cdm.open()
    cdm.set_pssh_b64("FIRST_TITLE_PSSH", session_id=first)
    cdm.set_required_kids(["00" * 16], session_id=first)
    cdm.get_license_challenge(first, FakePssh(b"ignored"))
    cdm.close(first)

    second = cdm.open()
    cdm.get_license_challenge(second, FakePssh(b"second"))

    assert sent == [("FIRST_TITLE_PSSH", ["00" * 16]), (base64.b64encode(b"second").decode(), None)]


@pytest.mark.parametrize("make_cdm", ADAPTERS)
def test_two_open_sessions_keep_their_own_pssh(make_cdm: Callable[[], Any]) -> None:
    cdm = make_cdm()
    sent = recording(cdm)

    one = cdm.open()
    two = cdm.open()
    cdm.set_pssh_b64("PSSH_ONE", session_id=one)
    cdm.set_pssh_b64("PSSH_TWO", session_id=two)
    cdm.get_license_challenge(one, FakePssh(b"x"))
    cdm.get_license_challenge(two, FakePssh(b"x"))

    assert [init_data for init_data, _ in sent] == ["PSSH_ONE", "PSSH_TWO"]


@pytest.mark.parametrize("make_cdm", ADAPTERS)
@pytest.mark.parametrize("header", [WRMHeader(HEADER), HEADER])
def test_a_bare_wrm_header_goes_out_as_a_playready_object(make_cdm: Callable[[], Any], header: Any) -> None:
    """A service or the client relay may pass only a header; the API takes a PlayReady Object."""
    cdm = make_cdm()
    sent = recording(cdm)

    cdm.get_license_challenge(cdm.open(), header)

    assert sent[0][0] == PlayReady.pro_b64_for(WRMHeader(HEADER))
