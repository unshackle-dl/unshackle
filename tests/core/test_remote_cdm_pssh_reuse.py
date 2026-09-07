"""A reused CustomRemoteCDM must not carry one title's PlayReady PSSH into the next.

``dl.get_cdm`` builds each CDM once per run, so one instance serves every title.
``PlayReady.get_content_keys`` calls ``set_pssh_b64`` only when the DRM has a PSSH, so
an instance-level value left over from an earlier title would be sent as the init data
of a title that has none.
"""

from __future__ import annotations

import base64

import pytest

from unshackle.core.cdm.custom_remote_cdm import CustomRemoteCDM

pytestmark = pytest.mark.unit


class FakePssh:
    """A PSSH object shaped like the ones the manifests hand to the CDM."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw


def make_cdm() -> CustomRemoteCDM:
    return CustomRemoteCDM(host="https://cdm.test", device={"name": "SL3000", "type": "PLAYREADY"})


def set_pssh(cdm: CustomRemoteCDM, session_id: bytes, pssh_b64: str) -> None:
    """Store a PSSH the way ``PlayReady.get_content_keys`` does."""
    try:
        cdm.set_pssh_b64(pssh_b64, session_id=session_id)
    except TypeError:  # CDM predating the per-session signature
        cdm.set_pssh_b64(pssh_b64)


def test_a_later_session_does_not_inherit_the_earlier_pssh() -> None:
    cdm = make_cdm()
    assert cdm.is_playready

    first = cdm.open()
    set_pssh(cdm, first, "FIRST_TITLE_PSSH")
    assert cdm.get_init_data_from_pssh(FakePssh(b"ignored")) == "FIRST_TITLE_PSSH"
    cdm.close(first)

    # The second title has no PSSH of its own, so nothing calls set_pssh_b64.
    cdm.open()
    init_data = cdm.get_init_data_from_pssh(FakePssh(b"second"))

    assert init_data == base64.b64encode(b"second").decode("utf-8")


def test_two_open_sessions_keep_their_own_pssh() -> None:
    """Tracks decrypt in parallel on the one shared instance, so the store is per session."""
    cdm = make_cdm()

    one = cdm.open()
    two = cdm.open()
    set_pssh(cdm, one, "PSSH_ONE")
    set_pssh(cdm, two, "PSSH_TWO")

    assert cdm.get_init_data_from_pssh(FakePssh(b"x"), cdm._sessions[one]["pssh_b64"]) == "PSSH_ONE"
    assert cdm.get_init_data_from_pssh(FakePssh(b"x"), cdm._sessions[two]["pssh_b64"]) == "PSSH_TWO"
