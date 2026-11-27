"""Tests for the AES-CTR whole-file cipher (``unshackle.core.drm.clearkey_ctr``)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from Cryptodome.Cipher import AES

from tests.core.key_resolver import key_table, resolver
from tests.core.test_paged_aac import FIRST, config_record, file, header
from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_ALL_DRM, DOWNLOAD_CANCELLED
from unshackle.core.drm import PAGED_AAC, ClearKeyCTR
from unshackle.core.drm.paged_aac import to_adts
from unshackle.core.tracks import Audio, Track
from unshackle.core.tracks import track as track_module
from unshackle.core.wire import build_tracks, reconstruct_drm, serialize_audio_track, serialize_drm

KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
IV = bytes.fromhex("72e067fbddcbcf77ebe8bc643f630d93")
MAGIC = b"OggS"


def ctr_encrypt(data: bytes) -> bytes:
    """Reference encryption: one counter stream over the whole file."""
    return AES.new(KEY, AES.MODE_CTR, nonce=b"", initial_value=int.from_bytes(IV, "big")).encrypt(data)


def keyed(magic: bytes = MAGIC, skip: int = 0) -> ClearKeyCTR:
    """A cipher that holds its content key from the start."""
    return ClearKeyCTR(None, IV, magic, skip, key=KEY)


def test_decrypt_round_trip(tmp_path: Path) -> None:
    plain = MAGIC + os.urandom((1 << 20) * 3 + 123)
    path = tmp_path / "track.ogg"
    path.write_bytes(ctr_encrypt(plain))
    assert path.read_bytes() != plain

    ClearKeyCTR(lambda: KEY, IV, MAGIC).decrypt(path)

    assert path.read_bytes() == plain
    assert list(tmp_path.iterdir()) == [path]


def test_hex_strings_build_the_same_cipher(tmp_path: Path) -> None:
    plain = MAGIC + os.urandom(5000)
    path = tmp_path / "track.ogg"
    path.write_bytes(ctr_encrypt(plain))

    ClearKeyCTR(lambda: KEY.hex(), IV.hex(), MAGIC.hex()).decrypt(path)

    assert path.read_bytes() == plain


def test_the_key_is_licensed_once_on_first_use(tmp_path: Path) -> None:
    calls = []

    def licence() -> bytes:
        calls.append(1)
        return KEY

    cipher = ClearKeyCTR(licence, IV, MAGIC)
    assert cipher.key is None

    for name in ("a.ogg", "b.ogg"):
        path = tmp_path / name
        path.write_bytes(ctr_encrypt(MAGIC + os.urandom(100)))
        cipher.decrypt(path)

    assert calls == [1]
    assert cipher.key == KEY


def test_other_lengths_are_refused() -> None:
    with pytest.raises(ValueError, match="16-byte IV"):
        ClearKeyCTR(lambda: KEY, IV + IV, MAGIC)
    with pytest.raises(ValueError, match="16-byte key"):
        ClearKeyCTR(lambda: KEY[:15], IV, MAGIC).content_key()


def test_no_signature_is_refused() -> None:
    with pytest.raises(ValueError, match="signature"):
        ClearKeyCTR(lambda: KEY, IV, b"")


def test_negative_skip_is_refused() -> None:
    with pytest.raises(ValueError, match="skip"):
        keyed(skip=-1)


@pytest.mark.parametrize("header_size", [167, (1 << 20) + 167], ids=["short", "longer-than-one-chunk"])
def test_skip_drops_a_header_in_front_of_the_media(tmp_path: Path, header_size: int) -> None:
    header, plain = os.urandom(header_size), MAGIC + os.urandom((1 << 20) + 5)
    path = tmp_path / "track.ogg"
    path.write_bytes(ctr_encrypt(header + plain))

    keyed(skip=header_size).decrypt(path)

    assert path.read_bytes() == plain
    assert list(tmp_path.iterdir()) == [path]


def test_skip_past_the_end_raises_and_keeps_the_input(tmp_path: Path) -> None:
    encrypted = ctr_encrypt(MAGIC + os.urandom(10))
    path = tmp_path / "track.ogg"
    path.write_bytes(encrypted)

    with pytest.raises(ValueError, match="does not start with"):
        keyed(skip=1 << 21).decrypt(path)

    assert path.read_bytes() == encrypted


def test_magic_accepts_the_expected_signature(tmp_path: Path) -> None:
    path = tmp_path / "track.flac"
    path.write_bytes(ctr_encrypt(b"fLaC" + os.urandom(5000)))

    keyed(magic=b"fLaC").decrypt(path)

    assert path.read_bytes().startswith(b"fLaC")
    assert list(tmp_path.iterdir()) == [path]


TRIMMABLE = header(FIRST, total=7081, first_record=config_record(true=1000)) + FIRST


def test_a_paged_file_that_cannot_be_cut_becomes_one_adts_stream(tmp_path: Path) -> None:
    paged = file(FIRST)
    path = tmp_path / "track.m4a"
    path.write_bytes(ctr_encrypt(paged))

    keyed(PAGED_AAC).decrypt(path)

    assert path.read_bytes() == to_adts(paged)
    assert path.read_bytes().startswith(b"\xff\xf1")
    assert list(tmp_path.iterdir()) == [path]


def test_a_paged_file_without_mp4box_becomes_one_adts_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries, "MP4Box", None)
    path = tmp_path / "track.m4a"
    path.write_bytes(ctr_encrypt(TRIMMABLE))

    keyed(PAGED_AAC).decrypt(path)

    assert path.read_bytes() == to_adts(TRIMMABLE)
    assert list(tmp_path.iterdir()) == [path]


def test_a_paged_file_becomes_an_m4a_file_through_mp4box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    streams: list[bytes] = []

    def step(args: list[str | Path], *, output: Path | None, label: str = "") -> bytes:
        calls.append([str(arg) for arg in args])
        streams.append(Path(args[4][:-4]).read_bytes())
        output.write_bytes(b"M4A")
        return b""

    monkeypatch.setattr(binaries, "MP4Box", Path("MP4Box"))
    monkeypatch.setattr("unshackle.core.drm.clearkey_ctr.run_step", step)
    path = tmp_path / "track.m4a"
    path.write_bytes(ctr_encrypt(TRIMMABLE))

    keyed(PAGED_AAC).decrypt(path)

    adts, output = Path(calls[0][4].removesuffix(":psx")), Path(calls[0][-1])
    assert path.read_bytes() == b"M4A"
    assert streams == [to_adts(TRIMMABLE)]
    assert calls == [
        [
            "MP4Box",
            "-for-test",
            "--moovts=-1",
            "-add",
            f"{adts}:psx",
            "-edits",
            "1=re0-1000/44100,6081/44100",
            "-new",
            str(output),
        ]
    ]
    assert adts.parent == output.parent == tmp_path and adts.suffix == ".aac" and output.suffix == ".m4a"
    assert list(tmp_path.iterdir()) == [path]


def test_a_failed_mp4box_step_leaves_one_adts_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(args: list[str | Path], *, output: Path | None, label: str = "") -> bytes:
        raise RuntimeError(f"{label} failed: unknown option")

    monkeypatch.setattr(binaries, "MP4Box", Path("MP4Box"))
    monkeypatch.setattr("unshackle.core.drm.clearkey_ctr.run_step", fail)
    path = tmp_path / "track.m4a"
    path.write_bytes(ctr_encrypt(TRIMMABLE))

    with caplog.at_level("WARNING", logger="unshackle.core.drm.clearkey_ctr"):
        keyed(PAGED_AAC).decrypt(path)

    assert path.read_bytes() == to_adts(TRIMMABLE)
    assert list(tmp_path.iterdir()) == [path]
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        "MP4Box failed on track.decrypted.m4a, wrote one ADTS stream"
    ]


def test_any_failure_after_the_decrypt_keeps_only_the_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def full(path: Path) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr("unshackle.core.drm.clearkey_ctr.rewrite_paged", full)
    encrypted = ctr_encrypt(TRIMMABLE)
    path = tmp_path / "track.m4a"
    path.write_bytes(encrypted)

    with pytest.raises(OSError, match="No space"):
        keyed(PAGED_AAC).decrypt(path)

    assert list(tmp_path.iterdir()) == [path] and path.read_bytes() == encrypted


def test_a_broken_paged_file_raises_and_keeps_the_input(tmp_path: Path) -> None:
    encrypted = ctr_encrypt(file(FIRST)[:-1])
    path = tmp_path / "track.m4a"
    path.write_bytes(encrypted)

    with pytest.raises(ValueError, match="is truncated"):
        keyed(PAGED_AAC).decrypt(path)

    assert path.read_bytes() == encrypted
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("start", [b"fLaX", b"XLaC", b"OggS"])
def test_magic_mismatch_raises_and_keeps_the_input(tmp_path: Path, start: bytes) -> None:
    encrypted = ctr_encrypt(start + os.urandom(5000))
    path = tmp_path / "track.flac"
    path.write_bytes(encrypted)

    with pytest.raises(ValueError, match="does not start with"):
        keyed(magic=b"fLaC").decrypt(path)

    assert path.read_bytes() == encrypted
    assert list(tmp_path.iterdir()) == [path]


def test_serialize_carries_no_key_until_the_cipher_is_licensed() -> None:
    cipher = ClearKeyCTR(lambda: KEY, IV, MAGIC)

    assert serialize_drm([cipher]) == [{"type": "clearkeyctr", "iv": IV.hex(), "magic": MAGIC.hex(), "skip": 0}]
    assert cipher.key is None


def test_a_track_list_from_the_server_leaves_the_key_to_the_licence_step(tmp_path: Path) -> None:
    plain = MAGIC + os.urandom(100)
    rebuilt = reconstruct_drm([{"type": "clearkeyctr", "iv": IV.hex(), "magic": MAGIC.hex(), "skip": 167}])
    cipher = rebuilt[0]
    assert isinstance(cipher, ClearKeyCTR) and (cipher.key, cipher.licence, cipher.skip) == (None, None, 167)

    path = tmp_path / "track.ogg"
    path.write_bytes(ctr_encrypt(b"H" * 167 + plain))
    with pytest.raises(ValueError, match="no content key"):
        cipher.decrypt(path)
    assert path.read_bytes() != plain

    cipher.take_key(KEY)
    cipher.decrypt(path)

    assert path.read_bytes() == plain


def test_remote_tracks_keep_the_cipher() -> None:
    track = Audio(
        id_="a1",
        url="https://example.test/a1",
        language="en",
        codec=Audio.Codec.OGG,
        drm=[keyed(skip=167)],
    )

    rebuilt = build_tracks({"audio": [serialize_audio_track(track, include_url=True)]})

    drm = rebuilt.audio[0].drm[0]
    assert isinstance(drm, ClearKeyCTR)
    assert (drm.key, drm.iv, drm.skip, drm.magic) == (KEY, IV, 167, MAGIC)


@pytest.mark.parametrize("all_drm", [False, True], ids=["one-drm", "all-drm"])
def test_url_download_decrypts_once_without_a_kid_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, all_drm: bool
) -> None:
    monkeypatch.setattr(config.directories, "temp", tmp_path)
    monkeypatch.setattr(Track, "get_key_id", lambda *a, **k: pytest.fail("a counter-cipher file has no KID to probe"))
    monkeypatch.setattr(Track, "get_init_segment", lambda *a, **k: pytest.fail("a counter-cipher file has no header"))
    monkeypatch.setattr(
        track_module, "assert_fragments_decrypted", lambda path: pytest.fail("a counter-cipher file has no fragments")
    )
    plain = os.urandom(70000 + 5)

    def downloader(**kwargs: Any) -> Iterator[dict[str, Any]]:
        (Path(kwargs["output_dir"]) / kwargs["filename"]).write_bytes(ctr_encrypt(b"H" * 167 + MAGIC + plain))
        yield {"file_downloaded": Path(kwargs["output_dir"]) / kwargs["filename"]}

    track = Audio(
        id_="a1",
        url="https://example.test/a1.ogg",
        language="en",
        codec=Audio.Codec.OGG,
        drm=[ClearKeyCTR(lambda: KEY, IV, MAGIC, skip=167)],
        downloader=downloader,
    )
    if all_drm:
        DOWNLOAD_ALL_DRM.set()
    try:
        track.download(
            session=requests.Session(), prepare_drm=partial(lambda *a, **kw: None), progress=partial(lambda **kw: None)
        )
    finally:
        DOWNLOAD_CANCELLED.clear()
        DOWNLOAD_ALL_DRM.clear()

    assert track.path is not None and track.path.read_bytes() == MAGIC + plain
    assert track.drm is None


def test_the_resolver_licenses_a_lazy_cipher_at_the_licence_step() -> None:
    calls = []

    def licence() -> bytes:
        calls.append(1)
        return KEY

    cipher = ClearKeyCTR(licence, IV, MAGIC)
    key_resolver = resolver()
    track = SimpleNamespace(drm=[cipher])

    key_resolver._prepare_drm(cipher, track, SimpleNamespace(), key_table())
    assert calls == [1]
    key_resolver._prepare_drm(cipher, track, SimpleNamespace(), key_table())

    assert calls == [1]


def test_the_resolver_asks_the_server_for_the_key_of_a_pending_cipher() -> None:
    cipher = ClearKeyCTR(None, IV, MAGIC)
    asked = []
    source = SimpleNamespace(license_file=lambda track: asked.append(track) or KEY.hex())

    track = SimpleNamespace(drm=[cipher])

    resolver(source=source)._prepare_drm(cipher, track, SimpleNamespace(), key_table())

    assert asked == [track]
    assert cipher.key == KEY


def test_a_cancelled_run_licenses_no_cipher() -> None:
    from unshackle.core.constants import DownloadCancelled

    cipher = ClearKeyCTR(lambda: pytest.fail("licensed after a cancel"), IV, MAGIC)
    DOWNLOAD_CANCELLED.set()
    try:
        with pytest.raises(DownloadCancelled):
            resolver()._prepare_drm(cipher, SimpleNamespace(drm=[cipher]), SimpleNamespace(), key_table())
    finally:
        DOWNLOAD_CANCELLED.clear()
