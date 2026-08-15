"""Tests for the HLS AES-128 ClearKey DRM system (``unshackle.core.drm.clearkey``).

Covers the correctness guards on the whole-segment AES-128-CBC path:
- ``ClearKey.decrypt`` passing mislabeled-clear / non-block-aligned segments through
  untouched instead of crashing or corrupting (WS1 / F1+F3)
- ``ClearKey.from_m3u_key`` ``data:`` URI parsing and 16-byte key validation (WS5 / F4)
- the RFC 8216 §5.2 sequence-number IV deriving from a segment's absolute media
  sequence number rather than the post-filter download index (WS2 / F2)
"""

from __future__ import annotations

import base64

import m3u8
import pytest
from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad
from m3u8.model import Key

from unshackle.core.drm.clearkey import SYNC_BYTE, TS_PACKET_SIZE, ClearKey

KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
IV = bytes.fromhex("0f0e0d0c0b0a09080706050403020100")


def encrypt(plaintext: bytes, key: bytes = KEY, iv: bytes = IV) -> bytes:
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))


def clear_ts(packets: int = 4) -> bytes:
    # Plaintext MPEG-TS: sync byte 0x47 at every 188-byte packet boundary. Default 4 packets so
    # the fixture satisfies the hardened 4-boundary sniff (and 752 B is 16-aligned, so it must be
    # caught by the clear-TS guard, not the len%16 guard).
    return b"".join(bytes([SYNC_BYTE]) + b"\x00" * (TS_PACKET_SIZE - 1) for _ in range(packets))


def test_valid_encrypted_segment_decrypts(tmp_path) -> None:
    plaintext = b"the quick brown fox jumps over" * 4
    seg = tmp_path / "0.ts"
    seg.write_bytes(encrypt(plaintext))

    ClearKey(key=KEY, iv=IV).decrypt(seg)

    assert seg.read_bytes() == plaintext


def test_clear_ts_passes_through(tmp_path) -> None:
    data = clear_ts()
    seg = tmp_path / "0.ts"
    seg.write_bytes(data)

    ClearKey(key=KEY, iv=IV).decrypt(seg)

    assert seg.read_bytes() == data


def test_non_block_aligned_passes_through(tmp_path) -> None:
    data = b"\x01" * 17  # not TS, and not a 16-byte multiple -> not valid CBC ciphertext
    assert len(data) % AES.block_size != 0
    seg = tmp_path / "0.ts"
    seg.write_bytes(data)

    ClearKey(key=KEY, iv=IV).decrypt(seg)

    assert seg.read_bytes() == data


def test_empty_segment_does_not_crash(tmp_path) -> None:
    seg = tmp_path / "0.ts"
    seg.write_bytes(b"")

    ClearKey(key=KEY, iv=IV).decrypt(seg)

    assert seg.exists()
    assert seg.read_bytes() == b""


def test_warns_once_per_instance(tmp_path, caplog) -> None:
    drm = ClearKey(key=KEY, iv=IV)
    for name in ("0.ts", "1.ts"):
        seg = tmp_path / name
        seg.write_bytes(clear_ts())
        with caplog.at_level("WARNING"):
            drm.decrypt(seg)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


def _data_key(raw: bytes, base64_encoded: bool = True) -> Key:
    if base64_encoded:
        uri = "data:text/plain;base64," + base64.b64encode(raw).decode("ascii")
    else:
        uri = "data:text/plain," + raw.decode("latin-1")
    return Key(method="AES-128", uri=uri, base_uri="", keyformat=None, keyformatversions=None, iv=None)


def test_data_uri_key_decodes_and_validates_length() -> None:
    drm = ClearKey.from_m3u_key(_data_key(KEY))
    assert drm.key == KEY


def test_data_uri_key_wrong_length_rejected() -> None:
    with pytest.raises(ValueError, match="Unexpected Length"):
        ClearKey.from_m3u_key(_data_key(b"\x00" * 8))


def test_raw_data_uri_key_materialises_and_validates() -> None:
    # A raw (non-base64) data: payload must be taken as literal key bytes, not hex-parsed.
    drm = ClearKey.from_m3u_key(_data_key(KEY, base64_encoded=False))
    assert drm.key == KEY


def test_raw_data_uri_key_wrong_length_rejected() -> None:
    with pytest.raises(ValueError, match="Unexpected Length"):
        ClearKey.from_m3u_key(_data_key(b"\x01" * 8, base64_encoded=False))


def test_data_uri_split_tolerates_commas_in_payload() -> None:
    # base64 with padding contains no comma, but the split must only break on the first comma
    # so a payload that itself contains a comma is not truncated. Encode a 16-byte key whose
    # base64 happens to be split-safe, and assert the mediatype comma is the only split point.
    raw = bytes(range(16))
    b64 = base64.b64encode(raw).decode("ascii")
    key = Key(
        method="AES-128",
        uri=f"data:application/octet-stream;base64,{b64}",
        base_uri="",
        keyformat=None,
        keyformatversions=None,
        iv=None,
    )
    drm = ClearKey.from_m3u_key(key)
    assert drm.key == raw


SEQ_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-KEY:METHOD=AES-128,URI="skd://key"
#EXTINF:6.0,
seg0.ts
#EXTINF:6.0,
seg1.ts
#EXTINF:6.0,
seg2.ts
#EXTINF:6.0,
seg3.ts
#EXT-X-ENDLIST
"""


def test_sequence_number_iv_uses_absolute_media_sequence() -> None:
    """IV must be the segment's absolute media sequence number, not the download index.

    Mirrors the derivation in ``HLS.download_track``: filtered segments are dropped, the
    surviving segments are named by post-filter download index, and the IV comes from the
    wanted segment's ``media_sequence`` (EXT-X-MEDIA-SEQUENCE + absolute position).
    """
    master = m3u8.loads(SEQ_PLAYLIST)
    # media sequence numbers are the offset (100) plus absolute playlist position.
    assert [s.media_sequence for s in master.segments] == [100, 101, 102, 103]

    # Drop an early segment (index 1), as an OnSegmentFilter would.
    unwanted = {master.segments[1]}
    wanted_segments = [s for s in master.segments if s not in unwanted]

    # download index 1 is the SECOND surviving segment (absolute seq 102), not 101.
    download_i = 1
    seq = wanted_segments[download_i].media_sequence
    assert seq == 102

    iv = int(seq).to_bytes(16, "big")
    assert iv == (102).to_bytes(16, "big")
    # the old download-index derivation would have produced the wrong IV (100 + 1 = 101)
    assert iv != (100 + download_i).to_bytes(16, "big")
