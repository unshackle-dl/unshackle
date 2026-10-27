"""Tests for the Blowfish CBC stripe cipher (``unshackle.core.drm.blowfish_stripe``)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import requests
from Cryptodome.Cipher import Blowfish

from unshackle.core.api.handlers import serialize_audio_track, serialize_drm
from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.drm import BlowfishStripe
from unshackle.core.remote_service import build_tracks, reconstruct_drm
from unshackle.core.tracks import Audio, Track
from unshackle.core.tracks import track as track_module

KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
CHUNK = BlowfishStripe.CHUNK


def stripe_encrypt(data: bytes, key: bytes = KEY) -> bytes:
    """Reference encryption: every third full chunk, each with a fresh CBC run."""
    out = bytearray(data)
    for offset in range(0, len(data), CHUNK * 3):
        chunk = data[offset : offset + CHUNK]
        if len(chunk) == CHUNK:
            out[offset : offset + CHUNK] = Blowfish.new(key, Blowfish.MODE_CBC, iv=BlowfishStripe.IV).encrypt(chunk)
    return bytes(out)


def test_decrypt_round_trip(tmp_path: Path) -> None:
    plain = os.urandom(CHUNK * 10 + 123)
    path = tmp_path / "track.flac"
    path.write_bytes(stripe_encrypt(plain))
    assert path.read_bytes() != plain

    BlowfishStripe(KEY).decrypt(path)

    assert path.read_bytes() == plain
    assert list(tmp_path.iterdir()) == [path]


def test_short_trailing_chunk_untouched(tmp_path: Path) -> None:
    # the last stripe starts at CHUNK * 3 and holds only 100 bytes, so it stays clear
    plain = os.urandom(CHUNK * 3 + 100)
    encrypted = stripe_encrypt(plain)
    assert encrypted[CHUNK * 3 :] == plain[CHUNK * 3 :]
    path = tmp_path / "track.mp3"
    path.write_bytes(encrypted)

    BlowfishStripe(KEY).decrypt(path)

    assert path.read_bytes() == plain


def test_serialize_round_trip_keeps_key() -> None:
    serialized = serialize_drm([BlowfishStripe(KEY)])
    assert serialized == [{"type": "blowfishstripe", "key": KEY.hex()}]

    rebuilt = reconstruct_drm(serialized)

    assert len(rebuilt) == 1
    assert isinstance(rebuilt[0], BlowfishStripe)
    assert rebuilt[0].key == KEY


def test_remote_tracks_keep_the_stripe_drm() -> None:
    track = Audio(
        id_="a1", url="https://example.test/a1", language="en", codec=Audio.Codec.FLAC, drm=[BlowfishStripe(KEY)]
    )

    rebuilt = build_tracks({"audio": [serialize_audio_track(track, include_url=True)]})

    assert isinstance(rebuilt.audio[0].drm[0], BlowfishStripe)
    assert rebuilt.audio[0].drm[0].key == KEY


def test_url_download_decrypts_once_without_a_kid_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.directories, "temp", tmp_path)
    monkeypatch.setattr(Track, "get_key_id", lambda *a, **k: pytest.fail("a stripe-cipher file has no KID to probe"))
    monkeypatch.setattr(
        track_module, "assert_fragments_decrypted", lambda path: pytest.fail("a stripe-cipher file has no fragments")
    )
    plain = os.urandom(CHUNK * 7 + 5)

    def downloader(**kwargs: Any) -> Iterator[dict[str, Any]]:
        (Path(kwargs["output_dir"]) / kwargs["filename"]).write_bytes(stripe_encrypt(plain))
        yield {"file_downloaded": Path(kwargs["output_dir"]) / kwargs["filename"]}

    track = Audio(
        id_="a1",
        url="https://example.test/a1.flac",
        language="en",
        codec=Audio.Codec.FLAC,
        drm=[BlowfishStripe(KEY)],
        downloader=downloader,
    )
    try:
        track.download(
            session=requests.Session(), prepare_drm=partial(lambda *a, **kw: None), progress=partial(lambda **kw: None)
        )
    finally:
        DOWNLOAD_CANCELLED.clear()

    assert track.path is not None and track.path.read_bytes() == plain
    assert track.drm is None
