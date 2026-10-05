"""--all-drm: each track licenses with Widevine and with PlayReady, through one wrapper in dl."""

from __future__ import annotations

import base64
from collections import defaultdict
from functools import partial
from types import SimpleNamespace
from typing import Any, Optional
from uuid import UUID

import m3u8
import pytest
from pyplayready.system.pssh import PSSH as PR_PSSH
from pywidevine.cdm import Cdm as WidevineCdm
from pywidevine.pssh import PSSH
from rich.table import Table

from unshackle.commands.dl import dl
from unshackle.core.api.handlers import validate_download_parameters
from unshackle.core.cdm.detect import cdm_type_stub, is_widevine_cdm
from unshackle.core.drm import PlayReady, Widevine
from unshackle.core.manifests import HLS

KID = UUID("00112233-4455-6677-8899-aabbccddeeff")
KEY = "00000000000000000000000000000001"
VAULT_KEY = "000000000000000000000000000000aa"
OTHER_KID = UUID("11112233-4455-6677-8899-aabbccddeeff")
OTHER_KEY = "00000000000000000000000000000002"


def playready_object(kid: UUID) -> str:
    kid_b64 = base64.b64encode(kid.bytes_le).decode()
    xml = (
        '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="4.3.0.0">'
        f'<DATA><PROTECTINFO><KIDS><KID ALGID="AESCTR" VALUE="{kid_b64}"></KID></KIDS></PROTECTINFO></DATA>'
        "</WRMHEADER>"
    ).encode("utf-16-le")
    pro = PR_PSSH.PlayreadyHeader.build(
        {"length": 10 + len(xml), "records": [{"type": 1, "length": len(xml), "data": xml}]}
    )
    return base64.b64encode(pro).decode()


def widevine() -> Widevine:
    return Widevine(pssh=PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[KID]))


def playready(kid: UUID = KID) -> PlayReady:
    pro = playready_object(kid)
    return PlayReady(pssh=PR_PSSH(base64.b64decode(pro)), pssh_b64=pro)


class Prepare:
    """Stands in for prepare_drm: records each call and gives the content key, or fails for one DRM system."""

    def __init__(self, fail: Optional[type] = None) -> None:
        self.fail = fail
        self.calls: list[tuple[str, bool, dict[UUID, str]]] = []

    def __call__(self, drm: Any, track_kid: Optional[UUID] = None, **kwargs: Any) -> None:
        forced = bool(kwargs.get("force") and kwargs.get("cdm_only"))
        self.calls.append((type(drm).__name__, forced, dict(drm.content_keys)))
        if type(drm) is self.fail:
            raise ValueError("licence refused")
        drm.content_keys[KID] = KEY

    def forced(self) -> list[str]:
        return [name for name, forced, _ in self.calls if forced]


@pytest.fixture
def cmd() -> dl:
    cmd = dl.__new__(dl)
    cmd.log = SimpleNamespace(warning=lambda *a: None, debug=lambda *a: None)
    cmd.cdm = object()
    cmd.all_drm_attempted = defaultdict(set)
    cmd.all_drm_warned = set()
    dl.DRM_LOCKS.clear()
    return cmd


def test_cdm_only_licenses_each_drm_system_although_a_content_key_is_held(cmd: dl) -> None:
    wv, pr = widevine(), playready()
    wv.content_keys[KID] = VAULT_KEY
    prepare = Prepare()

    cmd.licence_all_drm(prepare, SimpleNamespace(drm=[wv, pr]), False, wv, track_kid=KID, force=True)

    assert prepare.forced() == ["Widevine", "PlayReady"]
    assert all(held == {} for _, _, held in prepare.calls)
    assert wv.content_keys == {KID: KEY} and pr.content_keys == {KID: KEY}


def test_a_second_track_with_the_same_kids_sends_no_request(cmd: dl) -> None:
    prepare = Prepare(fail=PlayReady)
    for _ in range(2):
        wv, pr = widevine(), playready()
        cmd.licence_all_drm(prepare, SimpleNamespace(drm=[wv, pr]), True, wv, track_kid=KID, force=True)

    assert prepare.forced() == ["Widevine", "PlayReady"]
    assert len(prepare.calls) == 4


def test_a_drm_system_that_fails_stops_the_title(cmd: dl) -> None:
    wv, pr = widevine(), playready()
    wv.content_keys[KID] = VAULT_KEY

    with pytest.raises(ValueError, match="licence refused"):
        cmd.licence_all_drm(Prepare(fail=Widevine), SimpleNamespace(drm=[wv, pr]), False, wv, track_kid=KID, force=True)

    assert wv.content_keys == {KID: VAULT_KEY}


def test_with_tolerance_the_other_drm_system_gives_the_content_key(cmd: dl) -> None:
    wv, pr = widevine(), playready()
    loaded = cmd.cdm

    cmd.licence_all_drm(Prepare(fail=Widevine), SimpleNamespace(drm=[wv, pr]), True, wv, track_kid=KID)

    assert wv.content_keys == {KID: KEY}
    assert cmd.cdm is loaded


def test_with_tolerance_a_track_with_no_content_key_still_fails(cmd: dl) -> None:
    wv = widevine()

    with pytest.raises(ValueError, match="licence refused"):
        cmd.licence_all_drm(Prepare(fail=Widevine), SimpleNamespace(drm=[wv]), True, wv, track_kid=KID)


def test_a_drm_type_with_no_cdm_goes_straight_through(cmd: dl) -> None:
    prepare = Prepare()
    other = SimpleNamespace(content_keys={})

    cmd.licence_all_drm(prepare, SimpleNamespace(drm=[other, playready()]), False, other)

    assert prepare.calls == [("SimpleNamespace", False, {})]


def hls_keys(*lines: str) -> list[Any]:
    playlist = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n" + "".join(lines) + "#EXTINF:6.0,\nseg0.m4s\n#EXT-X-ENDLIST\n"
    return [key for key in m3u8.loads(playlist).keys if key]


def test_hls_builds_the_other_drm_system_from_its_key_entry() -> None:
    wv_pssh = PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[KID]).dumps()
    keys = hls_keys(
        f'#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="{WidevineCdm.urn}",URI="data:text/plain;base64,{wv_pssh}"\n',
        '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.microsoft.playready",'
        f'URI="data:text/plain;charset=UTF-16;base64,{playready_object(KID)}"\n',
    )

    assert [type(d) for d in HLS.alternate_drm(keys, widevine())] == [PlayReady]
    assert [type(d) for d in HLS.alternate_drm(keys, playready())] == [Widevine]


def test_hls_requests_no_key_uri_for_the_other_drm_system() -> None:
    keys = hls_keys('#EXT-X-KEY:METHOD=AES-128,URI="https://example.invalid/key.bin"\n')

    assert HLS.alternate_drm(keys, widevine()) == []


def test_a_playready_track_with_two_widevine_objects_licenses(cmd: dl) -> None:
    pr = playready()
    prepare = Prepare()

    cmd.licence_all_drm(prepare, SimpleNamespace(drm=[pr, widevine(), widevine()]), False, pr, track_kid=KID)

    assert [name for name, _, _ in prepare.calls] == ["PlayReady", "Widevine"]


class RealPrepare:
    """A dl whose real prepare_drm runs, with the licence of each DRM object replaced by fixed content keys."""

    def __init__(self, vault: Optional[dict[UUID, str]] = None) -> None:
        self.requests: list[str] = []
        self.vault_queries: list[UUID] = []
        cmd = dl.__new__(dl)
        cmd.log = SimpleNamespace(warning=lambda *a: None, debug=lambda *a: None, info=lambda *a: None)
        cmd.debug_logger = None
        cmd.service = "EXAMPLE"
        cmd.profile = None
        cmd.cdm = cdm_type_stub("widevine")
        cmd.all_drm_attempted = defaultdict(set)
        cmd.all_drm_warned = set()
        cmd.vaults = SimpleNamespace(get_key=self.get_key(vault or {}), sources={}, flagged=set())
        cmd.flush_vault_writes = lambda writes: None
        cmd.cdm_for = lambda system, quality=None: cdm_type_stub(system)
        dl.DRM_LOCKS.clear()
        dl.LICENSE_KEY_CACHE.clear()
        self.cmd = cmd

    def get_key(self, vault: dict[UUID, str]) -> Any:
        def get_key(kid: UUID) -> tuple[Optional[str], Optional[str]]:
            self.vault_queries.append(kid)
            return vault.get(kid), "vault" if kid in vault else None

        return get_key

    def drm(self, drm: Any, keys: dict[UUID, str]) -> Any:
        def get_content_keys(cdm: Any, certificate: Any = None, licence: Any = None) -> None:
            self.requests.append(type(drm).__name__)
            drm.content_keys.update(keys)

        drm.get_content_keys = get_content_keys
        return drm

    def run(self, wv: Any, pr: Any, force: bool = False) -> None:
        table = Table.grid()
        table.add_row("")
        track = SimpleNamespace(drm=[wv, pr], id="track", height=None)
        prepare = partial(
            self.cmd.prepare_drm,
            table=table,
            track=track,
            title=None,
            certificate=None,
            licence=lambda **kwargs: b"",
            cdm_only=False,
            vaults_only=False,
        )
        self.cmd.licence_all_drm(prepare, track, False, wv, track_kid=KID, force=force)


def test_cdm_only_licenses_each_drm_system_and_does_not_read_the_vault() -> None:
    real = RealPrepare(vault={KID: VAULT_KEY})
    wv, pr = real.drm(widevine(), {KID: KEY}), real.drm(playready(), {KID: KEY})

    real.run(wv, pr, force=True)

    assert real.requests == ["Widevine", "PlayReady"]
    assert real.vault_queries == []
    assert wv.content_keys == {KID: KEY} and pr.content_keys == {KID: KEY}
    assert is_widevine_cdm(real.cmd.cdm)

    real.run(real.drm(widevine(), {KID: KEY}), real.drm(playready(), {KID: KEY}), force=True)

    assert real.requests == ["Widevine", "PlayReady"]


def test_the_other_drm_system_can_return_a_different_set_of_kids() -> None:
    real = RealPrepare()
    wv = real.drm(widevine(), {KID: KEY})
    pr = real.drm(playready(OTHER_KID), {OTHER_KID: OTHER_KEY})

    real.run(wv, pr)

    assert wv.content_keys == {KID: KEY, OTHER_KID: OTHER_KEY}


def test_the_api_refuses_all_drm_with_one_device() -> None:
    assert "cdm" in (validate_download_parameters({"all_drm": True, "cdm": "device"}) or "")
    assert validate_download_parameters({"all_drm": True, "cdm_only": True}) is None
    assert validate_download_parameters({"all_drm": True, "cdm_only": False}) is None


def test_a_content_key_in_a_vault_stops_the_challenge_of_each_drm_system() -> None:
    real = RealPrepare(vault={KID: VAULT_KEY})
    wv, pr = real.drm(widevine(), {KID: KEY}), real.drm(playready(), {KID: KEY})

    real.run(wv, pr)

    assert real.requests == []
    assert wv.content_keys == {KID: VAULT_KEY} and pr.content_keys == {KID: VAULT_KEY}


def test_the_other_drm_system_licenses_only_for_a_kid_with_no_content_key() -> None:
    real = RealPrepare()

    real.run(real.drm(widevine(), {KID: KEY}), real.drm(playready(), {KID: KEY}))

    assert real.requests == ["Widevine"]


def test_the_init_segment_adds_the_drm_system_the_track_does_not_have(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.tracks import Track

    wv, pr = widevine(), playready()
    monkeypatch.setattr(PlayReady, "from_init_data", classmethod(lambda cls, data: pr))
    monkeypatch.setattr(
        Widevine, "from_init_data", classmethod(lambda cls, data: pytest.fail("the track has Widevine already"))
    )
    track = SimpleNamespace(drm=[wv], prefers_playready=lambda cdm: True)

    Track.add_init_drm(track, b"init", None)

    assert track.drm == [wv, pr]
