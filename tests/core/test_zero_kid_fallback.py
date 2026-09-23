from __future__ import annotations

import base64
from typing import Union
from uuid import UUID

import pytest
from pyplayready.system.pssh import PSSH as PlayReadyPSSH
from pywidevine.pssh import PSSH as WidevinePSSH

from unshackle.core.drm import PlayReady, Widevine, drm_from_dict, key_args
from unshackle.core.drm.key_args import ZERO_KID

VIDEO_KID = UUID("00112233-4455-6677-8899-aabbccddeeff")
AUDIO_KID = UUID("ffeeddcc-bbaa-9988-7766-554433221100")
VIDEO_KEY = "0000000000000000000000000000000a"
AUDIO_KEY = "0000000000000000000000000000000b"

DRM = Union[Widevine, PlayReady]


def _playready_pssh(*kids: UUID) -> tuple[PlayReadyPSSH, str]:
    kid_xml = "".join(f'<KID ALGID="AESCTR" VALUE="{base64.b64encode(k.bytes_le).decode()}"></KID>' for k in kids)
    header = (
        '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="4.3.0.0">'
        f"<DATA><PROTECTINFO><KIDS>{kid_xml}</KIDS></PROTECTINFO></DATA></WRMHEADER>"
    ).encode("utf-16-le")
    pro = PlayReadyPSSH.PlayreadyHeader.build(
        {"length": 6 + 4 + len(header), "records": [{"type": 1, "length": len(header), "data": header}]}
    )
    return PlayReadyPSSH(pro), base64.b64encode(pro).decode()


def _widevine(*kids: UUID, kid: UUID | None = None) -> Widevine:
    return Widevine(pssh=WidevinePSSH.new(system_id=WidevinePSSH.SystemId.Widevine, key_ids=list(kids)), kid=kid)


def _playready(*kids: UUID, kid: UUID | None = None) -> PlayReady:
    pssh, pssh_b64 = _playready_pssh(*kids)
    return PlayReady(pssh=pssh, kid=kid, pssh_b64=pssh_b64)


def _with_keys(drm: DRM, keys: dict[UUID, str]) -> DRM:
    drm.content_keys.update(keys)
    return drm


BOTH_KEYS = {VIDEO_KID: VIDEO_KEY, AUDIO_KID: AUDIO_KEY}


def _zero_entries(args: list[str]) -> list[str]:
    return [a.split(":", 1)[1] for a in args if a.startswith(f"{ZERO_KID}:")]


def _shaka_zero_entries(keys: str) -> list[str]:
    return [e.rsplit("key=", 1)[1] for e in keys.split(",") if f"key_id={ZERO_KID}" in e]


def _shaka_keys(drm: DRM) -> str:
    return key_args.shaka_keys(drm.content_keys, drm.own_kids())


OWN_KID_KNOWN = [
    pytest.param(lambda: _widevine(VIDEO_KID), id="widevine-pssh-kid"),
    pytest.param(lambda: _widevine(VIDEO_KID, AUDIO_KID, kid=VIDEO_KID), id="widevine-constructor-kid"),
    pytest.param(lambda: _playready(VIDEO_KID), id="playready-pssh-kid"),
    pytest.param(lambda: _playready(VIDEO_KID, AUDIO_KID, kid=VIDEO_KID), id="playready-constructor-kid"),
]


@pytest.mark.parametrize("make", OWN_KID_KNOWN)
def test_several_keys_give_the_zero_kid_only_the_own_key(make) -> None:
    drm = _with_keys(make(), BOTH_KEYS)

    args = drm.mp4decrypt_key_args()

    assert f"{VIDEO_KID.hex}:{VIDEO_KEY}" in args and f"{AUDIO_KID.hex}:{AUDIO_KEY}" in args
    assert _zero_entries(args) == [VIDEO_KEY]
    assert _shaka_zero_entries(_shaka_keys(drm)) == [VIDEO_KEY]


@pytest.mark.parametrize("make", [_widevine, _playready])
def test_a_single_key_keeps_its_zero_kid_entry(make) -> None:
    drm = _with_keys(make(VIDEO_KID), {VIDEO_KID: VIDEO_KEY})

    assert drm.mp4decrypt_key_args() == ["--key", f"{VIDEO_KID.hex}:{VIDEO_KEY}", "--key", f"{ZERO_KID}:{VIDEO_KEY}"]
    assert _shaka_keys(drm) == (
        f"label=0:key_id={VIDEO_KID.hex}:key={VIDEO_KEY},label=1:key_id={ZERO_KID}:key={VIDEO_KEY}"
    )


@pytest.mark.parametrize("make", [_widevine, _playready])
def test_several_keys_and_no_single_own_kid_give_no_zero_kid_entry(make) -> None:
    drm = _with_keys(make(VIDEO_KID, AUDIO_KID), BOTH_KEYS)

    assert _zero_entries(drm.mp4decrypt_key_args()) == []
    assert _shaka_zero_entries(_shaka_keys(drm)) == []


def test_shaka_decrypt_passes_the_built_keys(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The shaka-packager call uses the same builder the tests above cover."""
    from unshackle.core.drm import widevine as widevine_module

    seen: list[list[str]] = []

    class StopPopen(Exception):
        pass

    def fake_popen(cmd, **kwargs):
        seen.append([str(c) for c in cmd])
        raise StopPopen

    monkeypatch.setattr(widevine_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(widevine_module.binaries, "ShakaPackager", "shaka-packager")
    monkeypatch.setattr(widevine_module.config.directories, "temp", tmp_path)
    drm = _with_keys(_widevine(VIDEO_KID), BOTH_KEYS)
    media = tmp_path / "track.mp4"
    media.write_bytes(b"x")

    with pytest.raises(StopPopen):
        drm.decrypt_with_shaka_packager(media)

    keys = seen[0][seen[0].index("--keys") + 1]
    assert _shaka_zero_entries(keys) == [VIDEO_KEY]


def test_a_rebuilt_widevine_does_not_take_the_first_pssh_kid_as_its_own() -> None:
    drm = drm_from_dict(_widevine(VIDEO_KID, AUDIO_KID).to_dict())
    drm.content_keys.update(BOTH_KEYS)

    assert _zero_entries(drm.mp4decrypt_key_args()) == []
