"""--all-drm: each track licenses with Widevine and with PlayReady, through one wrapper in content-key resolution."""

from __future__ import annotations

import base64
import logging
from types import SimpleNamespace
from typing import Any, Optional
from uuid import UUID

import m3u8
import pytest
from pyplayready.system.pssh import PSSH as PR_PSSH
from pywidevine.cdm import Cdm as WidevineCdm
from pywidevine.pssh import PSSH

from tests.core.key_resolver import NoVaults, key_table, resolver
from unshackle.commands.dl import dl
from unshackle.core.api.handlers import load_full_cdm, resolve_server_cdm, validate_download_parameters
from unshackle.core.cdm.detect import is_widevine_cdm
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


def widevine(kid: UUID = KID) -> Widevine:
    return Widevine(pssh=PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[kid]))


def playready(kid: UUID = KID) -> PlayReady:
    pro = playready_object(kid)
    return PlayReady(pssh=PR_PSSH(base64.b64decode(pro)), pssh_b64=pro)


class Run:
    """A ContentKeyResolver whose DRM objects license with fixed content keys, for one track in an --all-drm run."""

    def __init__(self, vault: Optional[dict[UUID, str]] = None, **flags: Any) -> None:
        self.requests: list[str] = []
        self.held: list[dict[UUID, str]] = []
        self.vault_queries: list[UUID] = []
        self.keys = resolver(Vault(self.get_key(vault or {})), tag="EXAMPLE", **flags)

    def get_key(self, vault: dict[UUID, str]) -> Any:
        def get_key(kid: UUID) -> tuple[Optional[str], Optional[str]]:
            self.vault_queries.append(kid)
            return vault.get(kid), "vault" if kid in vault else None

        return get_key

    def drm(self, drm: Any, keys: Optional[dict[UUID, str]] = None, fail: bool = False) -> Any:
        """Give ``drm`` a licence that returns ``keys`` (KID:KEY by default), or that is refused."""

        def get_content_keys(cdm: Any, certificate: Any = None, licence: Any = None) -> None:
            self.requests.append(type(drm).__name__)
            self.held.append(dict(drm.content_keys))
            if fail:
                raise ValueError("licence refused")
            drm.content_keys.update({KID: KEY} if keys is None else keys)

        drm.get_content_keys = get_content_keys
        return drm

    def run(self, drm: Any, *others: Any, track_kid: Optional[UUID] = KID) -> None:
        track = SimpleNamespace(drm=[drm, *others], id="track", height=None)
        self.keys.licence_for(track, None, key_table(), all_drm=True)(drm, track_kid=track_kid)


class Vault(NoVaults):
    """A key vault that answers from ``get_key`` and stores nothing."""

    def __init__(self, get_key: Any) -> None:
        self.get_key = get_key  # type: ignore[method-assign]


def test_cdm_only_licenses_each_drm_system_although_a_content_key_is_held() -> None:
    run = Run(cdm_only=True)
    wv, pr = run.drm(widevine()), run.drm(playready())
    wv.content_keys[KID] = VAULT_KEY

    run.run(wv, pr)

    assert run.requests == ["Widevine", "PlayReady"]
    assert run.held == [{}, {}]
    assert wv.content_keys == {KID: KEY} and pr.content_keys == {KID: KEY}


def test_a_second_track_with_the_same_kids_sends_no_request() -> None:
    run = Run(cdm_only=True, best_available=True)
    for _ in range(2):
        run.run(run.drm(widevine()), run.drm(playready(), fail=True))

    assert run.requests == ["Widevine", "PlayReady"]


def test_a_drm_system_that_fails_stops_the_title() -> None:
    run = Run(cdm_only=True)
    wv, pr = run.drm(widevine(), fail=True), run.drm(playready())
    wv.content_keys[KID] = VAULT_KEY

    with pytest.raises(ValueError, match="licence refused"):
        run.run(wv, pr)

    assert wv.content_keys == {KID: VAULT_KEY}


def test_with_tolerance_the_other_drm_system_gives_the_content_key() -> None:
    run = Run(best_available=True)
    wv, pr = run.drm(widevine(), fail=True), run.drm(playready())
    loaded = run.keys.cdms.cdm

    run.run(wv, pr)

    assert wv.content_keys == {KID: KEY}
    assert run.keys.cdms.cdm is loaded


def test_with_tolerance_a_track_with_no_content_key_still_fails() -> None:
    run = Run(best_available=True)

    with pytest.raises(ValueError, match="licence refused"):
        run.run(run.drm(widevine(), fail=True))


def test_a_drm_type_with_no_cdm_goes_straight_through() -> None:
    run = Run()
    other = SimpleNamespace(content_keys={})

    run.run(other, run.drm(playready()), track_kid=None)

    assert run.requests == []
    assert other.content_keys == {}


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


def test_a_playready_track_with_two_widevine_objects_licenses() -> None:
    run = Run(cdm_only=True)

    run.run(run.drm(playready()), run.drm(widevine()), run.drm(widevine()))

    assert run.requests == ["PlayReady", "Widevine"]


def test_cdm_only_licenses_each_drm_system_and_does_not_read_the_vault() -> None:
    run = Run(vault={KID: VAULT_KEY}, cdm_only=True)
    wv, pr = run.drm(widevine()), run.drm(playready())

    run.run(wv, pr)

    assert run.requests == ["Widevine", "PlayReady"]
    assert run.vault_queries == []
    assert wv.content_keys == {KID: KEY} and pr.content_keys == {KID: KEY}
    assert is_widevine_cdm(run.keys.cdms.cdm)

    run.run(run.drm(widevine()), run.drm(playready()))

    assert run.requests == ["Widevine", "PlayReady"]


def test_the_other_drm_system_can_return_a_different_set_of_kids() -> None:
    run = Run()
    wv = run.drm(widevine())
    pr = run.drm(playready(OTHER_KID), {OTHER_KID: OTHER_KEY})

    run.run(wv, pr)

    assert wv.content_keys == {KID: KEY, OTHER_KID: OTHER_KEY}


def test_the_api_refuses_all_drm_with_one_device() -> None:
    assert "cdm" in (validate_download_parameters({"all_drm": True, "cdm": "device"}) or "")
    assert validate_download_parameters({"all_drm": True, "cdm_only": True}) is None
    assert validate_download_parameters({"all_drm": True, "cdm_only": False}) is None


def test_a_content_key_in_a_vault_stops_the_challenge_of_each_drm_system() -> None:
    run = Run(vault={KID: VAULT_KEY})
    wv, pr = run.drm(widevine()), run.drm(playready())

    run.run(wv, pr)

    assert run.requests == []
    assert wv.content_keys == {KID: VAULT_KEY} and pr.content_keys == {KID: VAULT_KEY}


def test_the_other_drm_system_licenses_only_for_a_kid_with_no_content_key() -> None:
    run = Run()

    run.run(run.drm(widevine()), run.drm(playready()))

    assert run.requests == ["Widevine"]


def test_the_init_segment_adds_the_drm_system_the_track_does_not_have(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.tracks import Track

    wv, pr = widevine(), playready()
    monkeypatch.setattr(PlayReady, "from_init_data", classmethod(lambda cls, data: pr))
    monkeypatch.setattr(
        Widevine, "from_init_data", classmethod(lambda cls, data: pytest.fail("the track has Widevine already"))
    )
    track = SimpleNamespace(drm=[wv], prefers_playready=lambda cdm: True)
    track.pssh_order = lambda cdm: Track.pssh_order(track, cdm)

    Track.add_init_drm(track, b"init", None)

    assert track.drm == [wv, pr]


@pytest.mark.parametrize(
    ("preference", "found", "read", "expected"),
    [
        ("pr", {PlayReady, Widevine}, [PlayReady], PlayReady),
        ("pr", {Widevine}, [PlayReady, Widevine], Widevine),
        ("wv", {PlayReady, Widevine}, [Widevine], Widevine),
        ("wv", {PlayReady}, [Widevine, PlayReady], PlayReady),
    ],
)
def test_drm_from_pssh_reads_the_preferred_system_first(
    preference: str, found: set, read: list, expected: type, caplog: pytest.LogCaptureFixture
) -> None:
    from unshackle.core.tracks import Video

    track = Video("https://cdn.example/v.mp4", language="en")
    track.drm_preference = preference
    tried: list[type] = []

    def read_pssh(system: Any) -> Any:
        tried.append(system)
        if system not in found:
            raise system.Exceptions.PSSHNotFound()
        return system

    with caplog.at_level(logging.DEBUG, logger="EXAMPLE"):
        track.drm_from_pssh(None, read_pssh, logging.getLogger("EXAMPLE"))

    assert tried == read
    assert track.drm == [expected]
    assert not caplog.records


def test_drm_from_pssh_without_a_pssh_logs_once_and_keeps_the_drm(caplog: pytest.LogCaptureFixture) -> None:
    from unshackle.core.tracks import Video

    track = Video("https://cdn.example/v.mp4", language="en")
    track.drm_preference = "pr"

    def read_pssh(system: Any) -> Any:
        raise system.Exceptions.PSSHNotFound()

    with caplog.at_level(logging.DEBUG, logger="EXAMPLE"):
        track.drm_from_pssh(None, read_pssh, logging.getLogger("EXAMPLE"))

    assert track.drm is None
    assert [r.getMessage() for r in caplog.records] == [
        "No PlayReady or Widevine PSSH was found for this track, is it DRM free?"
    ]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"playready": "pr_device", "widevine": "wv_device"}, "pr_device"),
        ({"widevine": "wv_device", "playready": "pr_device"}, "wv_device"),
    ],
)
def test_the_first_drm_system_of_the_config_entry_loads_first(
    monkeypatch: pytest.MonkeyPatch, entry: dict, expected: str
) -> None:
    monkeypatch.setattr("unshackle.commands.dl.config.cdm", {"EXAMPLE": entry})
    monkeypatch.setattr("unshackle.core.cdm.load_cdm", lambda name, **_: name)
    monkeypatch.setattr("unshackle.core.config.config.cdm", {"EXAMPLE": entry})
    cmd = dl.__new__(dl)
    cmd.vaults = None

    assert cmd.get_cdm("EXAMPLE") == expected
    assert load_full_cdm("EXAMPLE", None) == expected
    assert cmd.get_cdm("EXAMPLE", drm="widevine") == "wv_device"


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"playready": "pr_device", "widevine": "wv_device"}, "playready"),
        ({"widevine": "wv_device", "playready": "pr_device"}, "widevine"),
    ],
)
def test_the_server_cdm_stub_is_the_first_drm_system_of_the_config_entry(
    monkeypatch: pytest.MonkeyPatch, entry: dict, expected: str
) -> None:
    monkeypatch.setattr("unshackle.core.config.config.cdm", {"EXAMPLE": entry})
    monkeypatch.setattr(
        "unshackle.core.api.handlers.device_system",
        lambda name, _: "playready" if name == "pr_device" else "widevine",
    )

    assert is_widevine_cdm(resolve_server_cdm("EXAMPLE", None, None)) is (expected == "widevine")
