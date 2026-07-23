"""Self-check for ``has_encrypted_sample_entry``: a finalized MP4 whose moov still carries
an encv/enca sample entry means the decrypt tool skipped/failed the track (frma was never
applied), so the detector must flag it before the file reaches the muxer. Detection is
scoped to moov, so stray 4CC byte runs in mdat must never trip (or mask) it.

Run: uv run pytest tests/tracks/test_encrypted_sample_entry.py
"""

from __future__ import annotations

import struct
from pathlib import Path

from unshackle.core.tracks.track import has_encrypted_sample_entry


def _box(fmt: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + fmt + payload


def _sample_entry(fmt: bytes, payload: bytes = b"") -> bytes:
    body = b"\x00" * 6 + struct.pack(">H", 1) + payload  # reserved + data_reference_index
    return struct.pack(">I", 8 + len(body)) + fmt + body


def _stsd(entries: list[bytes]) -> bytes:
    body = b"\x00" * 4 + struct.pack(">I", len(entries)) + b"".join(entries)  # version/flags + count
    return _box(b"stsd", body)


def _moov(*children: bytes) -> bytes:
    stbl = _box(b"stbl", b"".join(children))
    return _box(b"moov", _box(b"trak", _box(b"mdia", _box(b"minf", stbl))))


FTYP = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
# stray [size][encv] byte run as compressed A/V payload could randomly contain
STRAY_ENCV = b"\xde\xad" * 4 + struct.pack(">I", 100) + b"encv" + b"\xbe\xef" * 8


def _write(tmp_path: Path, name: str, *boxes: bytes) -> Path:
    f = tmp_path / name
    f.write_bytes(b"".join(boxes))
    return f


def test_dual_stsd_encv_and_avc1_detected(tmp_path: Path) -> None:
    sinf = _box(b"sinf", _box(b"frma", b"avc1"))
    avc1 = _sample_entry(b"avc1", b"\x00" * 70)
    encv = _sample_entry(b"encv", b"\x00" * 70 + sinf)
    f = _write(tmp_path, "dual.mp4", FTYP, _moov(_stsd([avc1, encv])), _box(b"mdat", b"\x00" * 32))
    assert has_encrypted_sample_entry(f) is True


def test_single_avc1_not_detected(tmp_path: Path) -> None:
    avc1 = _sample_entry(b"avc1", b"\x00" * 70)
    f = _write(tmp_path, "clear.mp4", FTYP, _moov(_stsd([avc1])), _box(b"mdat", b"\x00" * 32))
    assert has_encrypted_sample_entry(f) is False


def test_stray_encv_in_mdat_not_detected(tmp_path: Path) -> None:
    # clean moov, mdat happens to contain a plausible [size][encv] run -> must NOT flag
    avc1 = _sample_entry(b"avc1", b"\x00" * 70)
    f = _write(tmp_path, "stray.mp4", FTYP, _moov(_stsd([avc1])), _box(b"mdat", STRAY_ENCV))
    assert has_encrypted_sample_entry(f) is False


def test_leading_mdat_does_not_mask_real_encv(tmp_path: Path) -> None:
    # progressive layout (mdat before moov) with a stray run must not hide a genuine encv
    enca = _sample_entry(b"enca", b"\x00" * 20 + _box(b"sinf", _box(b"frma", b"mp4a")))
    f = _write(tmp_path, "masked.mp4", FTYP, _box(b"mdat", STRAY_ENCV), _moov(_stsd([enca])))
    assert has_encrypted_sample_entry(f) is True


def test_text_containing_enca_in_moov_not_detected(tmp_path: Path) -> None:
    # "enca" inside a metadata string has no plausible box size before it -> must NOT flag
    avc1 = _sample_entry(b"avc1", b"\x00" * 70)
    udta = _box(b"udta", b"data is encapsulated here")
    f = _write(tmp_path, "text.mp4", FTYP, _box(b"moov", _stsd([avc1]) + udta))
    assert has_encrypted_sample_entry(f) is False


def test_missing_or_bogus_file(tmp_path: Path) -> None:
    assert has_encrypted_sample_entry(tmp_path / "nope.mp4") is False
    bogus = _write(tmp_path, "bogus.bin", b"\x00\x01\x02\x03not-an-mp4")
    assert has_encrypted_sample_entry(bogus) is False
