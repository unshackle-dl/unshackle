"""Self-check for ``assert_fragments_decrypted``: after decryption every moof must have lost
its standard senc. A survivor means the tool silently skipped that fragment (the
tfhd.sample_description_index ceiling bug), which the moov-scoped has_encrypted_sample_entry
structurally cannot see. A leftover PIFF uuid survives a good decrypt too, because content
shipping both boxes keeps it: a decrypter detaches only the atom it consumed.
The guard never reads mdat, so stray byte runs there leave it unmoved.

Run: uv run pytest tests/tracks/test_fragment_decrypted.py
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from unshackle.core.manifests.ism_init import PIFF_SENC_UUID
from unshackle.core.tracks.track import assert_fragments_decrypted


def _box(fmt: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + fmt + payload


def _senc(sample_count: int = 4) -> bytes:
    return _box(b"senc", b"\x00" * 4 + struct.pack(">I", sample_count) + b"\x00" * (8 * sample_count))


def _piff_senc(sample_count: int = 4) -> bytes:
    body = PIFF_SENC_UUID + b"\x00" * 4 + struct.pack(">I", sample_count) + b"\x00" * (8 * sample_count)
    return _box(b"uuid", body)


def _moof(*children: bytes, sdi: int = 1) -> bytes:
    tfhd = _box(b"tfhd", b"\x00\x00\x00\x02" + struct.pack(">II", 1, sdi))  # sample-description-index-present
    return _box(b"moof", _box(b"mfhd", b"\x00" * 8) + _box(b"traf", tfhd + b"".join(children)))


FTYP = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
MOOV = _box(b"moov", _box(b"mvex", _box(b"trex", b"\x00" * 24)))
MDAT = _box(b"mdat", b"\x00" * 64)
# a compressed payload can contain anything, including plausible-looking box runs
STRAY = _box(b"mdat", struct.pack(">I", 64) + b"senc" + b"\xbe\xef" * 8 + PIFF_SENC_UUID)


def _write(tmp_path: Path, name: str, *boxes: bytes) -> Path:
    f = tmp_path / name
    f.write_bytes(b"".join(boxes))
    return f


def test_all_fragments_decrypted_passes(tmp_path: Path) -> None:
    f = _write(tmp_path, "clean.mp4", FTYP, MOOV, _moof(), MDAT, _moof(sdi=2), MDAT)
    assert_fragments_decrypted(f)


def test_surviving_senc_raises(tmp_path: Path) -> None:
    # the real bug: fragment 1 (index 1) decrypted, the rest (index 2) silently skipped
    boxes = [FTYP, MOOV, _moof(), MDAT]
    for _ in range(3):
        boxes += [_moof(_senc(), sdi=2), MDAT]
    f = _write(tmp_path, "skipped.mp4", *boxes)
    with pytest.raises(ValueError, match=r"3/4 fragment"):
        assert_fragments_decrypted(f)


def test_surviving_cenc_senc_raises(tmp_path: Path) -> None:
    f = _write(tmp_path, "cenc.mp4", FTYP, MOOV, _moof(_senc()), MDAT)
    with pytest.raises(ValueError, match=r"1/1 fragment"):
        assert_fragments_decrypted(f)


def test_leftover_piff_uuid_after_senc_removed_does_not_raise(tmp_path: Path) -> None:
    # live DASH shape: the traf shipped senc+saiz+saio plus a duplicate PIFF uuid. mp4decrypt
    # consumed senc, detached senc/saiz/saio, and left the uuid on a fully decrypted fragment.
    boxes = [FTYP, MOOV]
    for _ in range(3):
        boxes += [_moof(_piff_senc()), MDAT]
    f = _write(tmp_path, "piff_leftover.mp4", *boxes)
    assert_fragments_decrypted(f)


def test_stray_senc_bytes_in_mdat_ignored(tmp_path: Path) -> None:
    f = _write(tmp_path, "stray.mp4", FTYP, MOOV, _moof(), STRAY, _moof(sdi=2), STRAY)
    assert_fragments_decrypted(f)


def test_senc_text_without_plausible_size_ignored(tmp_path: Path) -> None:
    # "senc" inside a free-box string has no valid box size before it
    f = _write(tmp_path, "text.mp4", FTYP, MOOV, _moof(_box(b"free", b"presence sencoded here")), MDAT)
    assert_fragments_decrypted(f)


def test_senc_bytes_inside_trun_payload_ignored(tmp_path: Path) -> None:
    # a trun sample entry spelling "senc" after a size-looking duration; only a byte scan bites
    trun = _box(b"trun", b"\x00\x00\x03\x01" + struct.pack(">I", 2) + struct.pack(">I", 512) + b"senc")
    f = _write(tmp_path, "trun.mp4", FTYP, MOOV, _moof(trun), MDAT)
    assert_fragments_decrypted(f)


def test_empty_senc_on_clear_fragment_ignored(tmp_path: Path) -> None:
    # sample_count 0 marks a fragment with no protected samples, so the guard ignores it
    f = _write(tmp_path, "empty.mp4", FTYP, MOOV, _moof(_senc(sample_count=0)), MDAT)
    assert_fragments_decrypted(f)


def _senc_override(sample_count: int) -> bytes:
    # flags&1 => AlgorithmID(3) + IV_size(1) + KID(16) precede sample_count
    head = b"\x00\x00\x00\x01" + b"\x00\x00\x08" + b"\x08" + b"\xaa" * 16
    return _box(b"senc", head + struct.pack(">I", sample_count) + b"\x00" * (8 * sample_count))


def test_override_flag_shifts_sample_count_offset(tmp_path: Path) -> None:
    clear = _write(tmp_path, "ovr_clear.mp4", FTYP, MOOV, _moof(_senc_override(0)), MDAT)
    assert_fragments_decrypted(clear)
    f = _write(tmp_path, "ovr.mp4", FTYP, MOOV, _moof(_senc_override(5)), MDAT)
    with pytest.raises(ValueError, match=r"1/1 fragment"):
        assert_fragments_decrypted(f)


def test_senc_outside_traf_ignored(tmp_path: Path) -> None:
    # a decrypted moof whose only "senc" sits in a sibling box outside any traf
    f = _write(tmp_path, "sibling.mp4", FTYP, MOOV, _box(b"moof", _box(b"mfhd", b"\x00" * 8) + _senc()), MDAT)
    assert_fragments_decrypted(f)


def test_senc_nested_under_non_traf_ignored(tmp_path: Path) -> None:
    # senc two levels down under a non-traf container: only traf children count
    meta = _box(b"meta", _senc())
    f = _write(tmp_path, "nested.mp4", FTYP, MOOV, _box(b"moof", _box(b"mfhd", b"\x00" * 8) + meta), MDAT)
    assert_fragments_decrypted(f)


def test_undersized_largesize_box_does_not_desync_walk(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # size==1 reads a 16-byte header, so a largesize of 12 clears the 8-byte floor but
    # still stalls the cursor onto payload; only a header-aware floor catches it
    runt = struct.pack(">I", 1) + b"free" + struct.pack(">Q", 12)
    f = _write(tmp_path, "runt.mp4", FTYP, MOOV, runt, _moof(_senc()), MDAT, _moof(_senc()), MDAT)
    with caplog.at_level("WARNING"):
        assert_fragments_decrypted(f)
    assert "malformed box size" in caplog.text


def test_largesize_and_zero_size_boxes_walked(tmp_path: Path) -> None:
    frag = _moof(_senc())
    large = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 16 + 64) + b"\x00" * 64
    trailing = struct.pack(">I", 0) + b"mdat" + b"\x00" * 64  # size 0 => runs to EOF
    f = _write(tmp_path, "large.mp4", FTYP, MOOV, frag, large, trailing)
    with pytest.raises(ValueError, match=r"1/1 fragment"):
        assert_fragments_decrypted(f)


def test_malformed_box_size_warns_and_does_not_raise(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # failing open is deliberate, but the abort must be logged so it cannot pass as clean
    overrun = struct.pack(">I", 1 << 30) + b"free" + b"\x00" * 8
    f = _write(tmp_path, "overrun.mp4", FTYP, MOOV, overrun, _moof(_senc()), MDAT)
    with caplog.at_level("WARNING"):
        assert_fragments_decrypted(f)
    assert "malformed box size" in caplog.text


def test_missing_or_bogus_file_is_noop(tmp_path: Path) -> None:
    assert_fragments_decrypted(tmp_path / "nope.mp4")
    assert_fragments_decrypted(_write(tmp_path, "bogus.bin", b"\x00\x01\x02\x03not-an-mp4"))
    truncated = _write(tmp_path, "cut.mp4", FTYP, MOOV, _moof(_senc())[:20])
    assert_fragments_decrypted(truncated)
