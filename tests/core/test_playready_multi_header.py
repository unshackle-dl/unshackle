from __future__ import annotations

import base64
from typing import Any
from uuid import UUID

import pytest
from pyplayready.system.pssh import PSSH
from pyplayready.system.wrmheader import WRMHeader

from unshackle.core.drm import PlayReady

KID_A = UUID("00112233-4455-6677-8899-aabbccddeeff")
KID_B = UUID("ffeeddcc-bbaa-9988-7766-554433221100")
KEY_A = "00000000000000000000000000000001"
KEY_B = "00000000000000000000000000000002"


def _wrm_header(*kids: UUID, la_url: str = "https://example.invalid/") -> bytes:
    """A minimal v4.3 WRMHEADER naming the given KIDs, UTF-16LE as the PRO carries it."""
    kid_xml = "".join(f'<KID ALGID="AESCTR" VALUE="{base64.b64encode(kid.bytes_le).decode()}"></KID>' for kid in kids)
    xml = (
        '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="4.3.0.0">'
        f"<DATA><PROTECTINFO><KIDS>{kid_xml}</KIDS></PROTECTINFO><LA_URL>{la_url}</LA_URL></DATA>"
        "</WRMHEADER>"
    )
    return xml.encode("utf-16-le")


def _pro(*headers: bytes) -> bytes:
    records = [{"type": 1, "length": len(h), "data": h} for h in headers]
    return PSSH.PlayreadyHeader.build({"length": 6 + sum(4 + len(h) for h in headers), "records": records})


def _drm(*headers: bytes) -> PlayReady:
    pro = _pro(*headers)
    return PlayReady(pssh=PSSH(pro), pssh_b64=base64.b64encode(pro).decode())


class FakeKey:
    def __init__(self, kid: UUID, key: str) -> None:
        self.key_id = kid
        self.key = bytes.fromhex(key)


class FakeCdm:
    """Answers each challenge with the keys mapped for the header's KIDs; records what it saw."""

    def __init__(self, answers: dict[UUID, str], fail_on: set[UUID] | None = None) -> None:
        self.answers = answers
        self.fail_on = fail_on or set()
        self.challenged: list[list[UUID]] = []
        self.pssh_b64s: list[str] = []
        self.required: list[list[UUID]] = []
        self.attempted: list[list[UUID]] = []
        self._by_session: dict[str, list[UUID]] = {}
        self._next = 0

    def open(self) -> str:
        self._next += 1
        return f"session-{self._next}"

    def close(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)

    def set_pssh_b64(self, pssh_b64: str, session_id: Any = None) -> None:
        self.pssh_b64s.append(pssh_b64)

    def set_required_kids(self, kids: list[UUID], session_id: Any = None) -> None:
        self.required.append(list(kids))

    def get_license_challenge(self, session_id: str, header: WRMHeader) -> str:
        kids = [k.value for k in header.key_ids]
        self.attempted.append(kids)
        if self.fail_on & set(kids):
            raise RuntimeError("licence refused")
        self.challenged.append(kids)
        self._by_session[session_id] = kids
        return "challenge"

    def parse_license(self, session_id: str, response: str) -> None:
        pass

    def get_keys(self, session_id: str) -> list[FakeKey]:
        return [FakeKey(kid, self.answers[kid]) for kid in self._by_session[session_id] if kid in self.answers]


def _licence(**_: Any) -> str:
    return "<License>x</License>"


def _cert(**_: Any) -> None:
    return None


def test_kids_are_collected_from_every_header() -> None:
    drm = _drm(_wrm_header(KID_A), _wrm_header(KID_B))

    assert len(drm.pssh.wrm_headers) == 2
    assert drm.kids == [KID_A, KID_B]


def test_single_header_sends_one_challenge_with_the_original_pssh() -> None:
    drm = _drm(_wrm_header(KID_A, KID_B))
    cdm = FakeCdm({KID_A: KEY_A, KID_B: KEY_B})

    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)

    assert cdm.challenged == [[KID_A, KID_B]]
    assert cdm.pssh_b64s == [drm.pssh_b64]
    assert cdm.required == [[KID_A, KID_B]]
    assert drm.content_keys == {KID_A: KEY_A, KID_B: KEY_B}


def test_two_distinct_headers_are_both_licensed_and_keys_merged() -> None:
    drm = _drm(_wrm_header(KID_A), _wrm_header(KID_B))
    cdm = FakeCdm({KID_A: KEY_A, KID_B: KEY_B})

    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)

    assert cdm.challenged == [[KID_A], [KID_B]]
    assert drm.content_keys == {KID_A: KEY_A, KID_B: KEY_B}
    # a remote CDM sends whatever set_pssh_b64 gave it, so each round carries only its own header
    assert [len(PSSH(b).wrm_headers) for b in cdm.pssh_b64s] == [1, 1]
    for pro_b64 in cdm.pssh_b64s:
        pro = base64.b64decode(pro_b64)
        assert PSSH.PlayreadyHeader.parse(pro).length == len(pro)
        assert _pro(PSSH(pro).wrm_headers[0].dumps().encode("utf-16-le")) == pro
    assert [PSSH(b).wrm_headers[0].dumps() for b in cdm.pssh_b64s] == [h.dumps() for h in drm.pssh.wrm_headers]


def test_stacked_licence_covering_every_kid_stops_after_one_challenge() -> None:
    drm = _drm(_wrm_header(KID_A), _wrm_header(KID_B))
    cdm = FakeCdm({KID_A: KEY_A, KID_B: KEY_B})
    cdm.get_keys = lambda session_id: [FakeKey(KID_A, KEY_A), FakeKey(KID_B, KEY_B)]  # type: ignore[method-assign]

    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)

    assert cdm.challenged == [[KID_A]]
    assert drm.content_keys == {KID_A: KEY_A, KID_B: KEY_B}


def test_headers_naming_the_same_kids_cost_one_challenge() -> None:
    drm = _drm(_wrm_header(KID_A), _wrm_header(KID_A))
    cdm = FakeCdm({KID_A: KEY_A})

    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)

    assert cdm.challenged == [[KID_A]]


def test_a_header_already_covered_by_the_vault_is_skipped() -> None:
    drm = _drm(_wrm_header(KID_A), _wrm_header(KID_B))
    drm.content_keys[KID_A] = KEY_A
    cdm = FakeCdm({KID_A: KEY_A, KID_B: KEY_B})

    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)

    assert cdm.challenged == [[KID_B]]


def test_an_uncovered_kid_warns_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "unshackle.core.drm.playready.log_event",
        lambda operation, **kw: events.append({"operation": operation, **kw}),
    )
    drm = _drm(_wrm_header(KID_A), _wrm_header(KID_B))
    cdm = FakeCdm({KID_A: KEY_A})

    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)

    assert drm.content_keys == {KID_A: KEY_A}
    missing = [e for e in events if e["operation"] == "drm_missing_keys"]
    assert missing and missing[0]["level"] == "WARNING"
    assert missing[0]["missing_kids"] == [KID_B.hex]


def test_a_refused_header_does_not_lose_the_keys_of_the_others() -> None:
    drm = _drm(_wrm_header(KID_A), _wrm_header(KID_B))
    cdm = FakeCdm({KID_A: KEY_A, KID_B: KEY_B}, fail_on={KID_A})

    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)

    assert drm.content_keys == {KID_B: KEY_B}


def test_every_header_refused_raises() -> None:
    drm = _drm(_wrm_header(KID_A), _wrm_header(KID_B))
    cdm = FakeCdm({}, fail_on={KID_A, KID_B})

    with pytest.raises(RuntimeError, match="licence refused"):
        drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)


def test_get_drm_for_cdm_folds_sibling_playready_objects_into_the_first() -> None:
    from pyplayready.cdm import Cdm as PlayReadyCdm

    from unshackle.core.tracks import Video

    first = _drm(_wrm_header(KID_A))
    second = _drm(_wrm_header(KID_B))
    track = Video(
        id_="v", url="https://example.invalid/v.mpd", language="en", codec=Video.Codec.AVC, drm=[first, second]
    )
    fake_cdm = PlayReadyCdm.__new__(PlayReadyCdm)

    chosen = track.get_drm_for_cdm(fake_cdm)

    assert chosen is first
    assert chosen.kids == [KID_A, KID_B]
    assert [[k.value for k in h.key_ids] for h in chosen.distinct_headers()] == [[KID_A], [KID_B]]

    cdm = FakeCdm({KID_A: KEY_A, KID_B: KEY_B})
    chosen.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)
    assert chosen.content_keys == {KID_A: KEY_A, KID_B: KEY_B}


def test_repeated_get_drm_for_cdm_does_not_grow_the_header_list() -> None:
    from pyplayready.cdm import Cdm as PlayReadyCdm

    from unshackle.core.tracks import Video

    # a KID-less header is exempt from the KID-set dedupe, so only identity-level idempotence protects it
    first = _drm(_wrm_header(KID_A))
    second = PlayReady(pssh=PSSH(_pro(_wrm_header(la_url="https://kidless.invalid/"))), kid=KID_B)
    track = Video(
        id_="v", url="https://example.invalid/v.mpd", language="en", codec=Video.Codec.AVC, drm=[first, second]
    )
    fake_cdm = PlayReadyCdm.__new__(PlayReadyCdm)

    for _ in range(3):
        chosen = track.get_drm_for_cdm(fake_cdm)

    assert chosen is first
    assert len(chosen.distinct_headers()) == 2
    assert chosen.kids == [KID_A, KID_B]

    cdm = FakeCdm({KID_A: KEY_A})
    chosen.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)
    assert len(cdm.challenged) == 2


def test_absorb_keeps_every_sibling_under_concurrent_calls() -> None:
    from concurrent.futures import ThreadPoolExecutor

    first = _drm(_wrm_header(KID_A))
    # each call brings a sibling nobody else brings, so a lost update shows as a missing header
    siblings = [PlayReady(pssh=PSSH(_pro(_wrm_header(la_url=f"https://x{i}.invalid/"))), kid=KID_B) for i in range(64)]

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda sibling: first.absorb(sibling), siblings))

    assert len(first.distinct_headers()) == 65
    assert first.kids == [KID_A, KID_B]


def test_a_track_carrying_playready_survives_deepcopy() -> None:
    from copy import deepcopy

    from unshackle.core.tracks import Video

    first = _drm(_wrm_header(KID_A))
    second = _drm(_wrm_header(KID_B))
    first.absorb(second)
    track = Video(
        id_="v", url="https://example.invalid/v.mpd", language="en", codec=Video.Codec.AVC, drm=[first, second]
    )

    clone = deepcopy(track)

    assert clone.drm[0] is not first
    assert clone.drm[0].kids == [KID_A, KID_B]
    assert len(clone.drm[0].distinct_headers()) == 2


def _pro_b64(*headers: bytes) -> str:
    return base64.b64encode(_pro(*headers)).decode()


def _xml_header(version: str, body: str) -> bytes:
    return (
        f'<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="{version}">'
        f"<DATA>{body}</DATA></WRMHEADER>"
    ).encode("utf-16-le")


def _b64le(kid: UUID) -> str:
    return base64.b64encode(kid.bytes_le).decode()


@pytest.mark.parametrize(
    ("version", "body", "expected"),
    [
        (
            "4.0.0.0",
            f"<PROTECTINFO><KEYLEN>16</KEYLEN><ALGID>AESCTR</ALGID></PROTECTINFO><KID>{_b64le(KID_A)}</KID>",
            [KID_A],
        ),
        ("4.1.0.0", f'<PROTECTINFO><KID ALGID="AESCTR" VALUE="{_b64le(KID_B)}"></KID></PROTECTINFO>', [KID_B]),
        (
            "4.2.0.0",
            f'<PROTECTINFO><KIDS><KID ALGID="AESCTR" VALUE="{_b64le(KID_A)}"></KID>'
            f'<KID ALGID="AESCTR" VALUE="{_b64le(KID_B)}"></KID></KIDS></PROTECTINFO>',
            [KID_A, KID_B],
        ),
    ],
)
def test_kids_are_read_from_every_header_version(version: str, body: str, expected: list[UUID]) -> None:
    header = _xml_header(version, body)
    assert PlayReady(pssh=PSSH(_pro(header)), pssh_b64=_pro_b64(header)).kids == expected


def test_customattributes_kids_follow_the_header_kids() -> None:
    kid_c = UUID("11111111-2222-3333-4444-555555555555")
    header = _xml_header(
        "4.1.0.0",
        f'<PROTECTINFO><KID ALGID="AESCTR" VALUE="{_b64le(KID_A)}"></KID></PROTECTINFO>'
        f'<CUSTOMATTRIBUTES><KIDS><KID VALUE="{_b64le(KID_B)}"/><KID VALUE="{_b64le(kid_c)}"/></KIDS></CUSTOMATTRIBUTES>',
    )

    drm = PlayReady(pssh=PSSH(_pro(header)), pssh_b64=_pro_b64(header))

    # the header KID leads (it is what the licence server acts on); CUSTOMATTRIBUTES KIDs stay reachable
    assert drm.kids == [KID_A, KID_B, kid_c]
    assert drm.kid == KID_A


def test_device_revoked_on_the_first_header_is_not_swallowed() -> None:
    drm = _drm(_wrm_header(KID_A), _wrm_header(KID_B))
    cdm = FakeCdm({KID_A: KEY_A, KID_B: KEY_B})

    calls: list[int] = []

    def licence(**_: Any) -> str:
        calls.append(1)
        if len(calls) == 1:
            raise PlayReady.Exceptions.DeviceRevoked("revoked")
        return "<License>x</License>"

    with pytest.raises(PlayReady.Exceptions.DeviceRevoked):
        drm.get_content_keys(cdm=cdm, certificate=_cert, licence=licence)
    assert cdm.challenged == [[KID_A]]


def test_a_revocation_hresult_in_a_refusal_becomes_device_revoked() -> None:
    drm = _drm(_wrm_header(KID_A), _wrm_header(KID_B))
    cdm = FakeCdm({KID_A: KEY_A, KID_B: KEY_B})

    calls: list[int] = []

    def licence(**_: Any) -> str:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("<StatusCode>0x8004C065</StatusCode>")
        return "<License>x</License>"

    with pytest.raises(PlayReady.Exceptions.DeviceRevoked):
        drm.get_content_keys(cdm=cdm, certificate=_cert, licence=licence)
    assert cdm.challenged == [[KID_A]]


def _kidless_header() -> bytes:
    return _wrm_header(la_url="https://kidless.invalid/")


def test_a_kidless_header_is_still_sent_when_the_vault_covered_every_kid() -> None:
    drm = _drm(_wrm_header(KID_A), _kidless_header())
    drm.content_keys[KID_A] = KEY_A
    cdm = FakeCdm({KID_A: KEY_A})

    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)

    assert cdm.challenged == [[]]


def test_a_kidless_header_is_still_sent_when_a_licence_round_covered_every_kid() -> None:
    drm = _drm(_wrm_header(KID_A), _kidless_header())
    cdm = FakeCdm({KID_A: KEY_A})

    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)

    assert cdm.challenged == [[KID_A], []]


def test_a_header_that_yielded_nothing_is_not_sent_again_by_the_same_object() -> None:
    drm = _drm(_wrm_header(KID_A), _wrm_header(KID_B))
    cdm = FakeCdm({KID_A: KEY_A})

    # every track of a title calls this on the shared object; the second call must not repeat B
    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)
    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)

    assert cdm.challenged == [[KID_A], [KID_B]]
    assert drm.content_keys == {KID_A: KEY_A}


def test_a_refused_header_is_not_sent_again_by_the_same_object() -> None:
    drm = _drm(_wrm_header(KID_A), _wrm_header(KID_B))
    cdm = FakeCdm({KID_A: KEY_A, KID_B: KEY_B}, fail_on={KID_B})

    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)
    attempts_after_first = len(cdm.attempted)
    drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)

    assert len(cdm.attempted) == attempts_after_first
    assert drm.content_keys == {KID_A: KEY_A}


def test_a_single_refused_header_still_raises_on_every_call() -> None:
    drm = _drm(_wrm_header(KID_A))
    cdm = FakeCdm({}, fail_on={KID_A})

    for _ in range(2):
        with pytest.raises(RuntimeError, match="licence refused"):
            drm.get_content_keys(cdm=cdm, certificate=_cert, licence=_licence)
