"""Decrypt cbcs fragmented MP4 in-process, for the file shapes the external decrypters get wrong.

mp4decrypt and shaka-packager select a content key per sample entry through the `tenc` default KID.
Some cbcs audio ships one track with two `enca` sample entries, each carrying an all-zero
default KID, its own constant IV, and its own content key. Both tools then give every sample
entry the same key and output noise for the samples of the other entry. shaka-packager also
rejects some codec 4CCs (`alac`) before it looks at the encryption at all.

This module reads the layout the tools cannot act on and decrypts each sample with the content key
of the sample entry its fragment names. The rewrite keeps every box size: `enca`/`encv` get
their original 4CC back from `frma`, and `sinf`, `pssh`, `senc`, `saiz` and `saio` become
`free` boxes in place. Sample group overrides (`seig`) are not read, so a file that rotates
keys per sample group needs an external decrypter.
"""

from __future__ import annotations

import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Optional, Union
from uuid import UUID

from Cryptodome.Cipher import AES

BLOCK = 16
ZERO_KID = UUID(int=0)

# Sample entry types whose child boxes (`sinf` among them) start at a fixed offset.
_AUDIO_ENTRY_TYPES = {b"enca"}
_VIDEO_ENTRY_TYPES = {b"encv"}

_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"mvex", b"moof", b"traf", b"schi", b"sinf"}


@dataclass
class SampleEntry:
    """One `stsd` entry and the `tenc` protection it declares."""

    index: int
    entry_type: str
    data_format: str
    scheme: str
    protected: bool
    kid: UUID
    iv_size: int
    constant_iv: bytes
    crypt_block: int
    skip_block: int
    offset: int
    sinf_offset: Optional[int]


@dataclass
class TrackLayout:
    track_id: int
    entries: list[SampleEntry] = field(default_factory=list)
    default_entry_index: int = 1

    @property
    def protected_entries(self) -> list[SampleEntry]:
        return [entry for entry in self.entries if entry.protected]


@dataclass
class Layout:
    """What the `moov` says about protection, per track."""

    tracks: dict[int, TrackLayout] = field(default_factory=dict)
    pssh_offsets: list[int] = field(default_factory=list)

    @property
    def codecs(self) -> set[str]:
        return {entry.data_format for track in self.tracks.values() for entry in track.entries}

    @property
    def schemes(self) -> set[str]:
        return {entry.scheme for track in self.tracks.values() for entry in track.protected_entries}


def iter_boxes(data: bytes, start: int = 0, end: Optional[int] = None) -> Iterator[tuple[bytes, int, int, int]]:
    """Yield (type, header_size, box_start, box_end) for each box in data[start:end]."""
    if end is None:
        end = len(data)
    pos = start
    while pos + 8 <= end:
        size, box_type = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            (size,) = struct.unpack_from(">Q", data, pos + 8)
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            raise ValueError(f"Malformed box {box_type!r} at offset {pos}: size {size}")
        yield box_type, header, pos, min(pos + size, end)
        pos += size


def _find_child(data: bytes, start: int, end: int, wanted: bytes) -> Optional[tuple[int, int, int]]:
    for box_type, header, box_start, box_end in iter_boxes(data, start, end):
        if box_type == wanted:
            return header, box_start, box_end
    return None


def _parse_tenc(data: bytes, start: int, end: int) -> tuple[bool, UUID, int, bytes, int, int]:
    version = data[start]
    pos = start + 4
    crypt_block = skip_block = 0
    if version == 0:
        pos += 2
    else:
        pattern = data[pos + 1]
        crypt_block, skip_block = pattern >> 4, pattern & 0x0F
        pos += 2
    protected = data[pos] == 1
    iv_size = data[pos + 1]
    kid = UUID(bytes=bytes(data[pos + 2 : pos + 18]))
    pos += 18
    constant_iv = b""
    if protected and iv_size == 0 and pos < end:
        constant_iv_size = data[pos]
        constant_iv = bytes(data[pos + 1 : pos + 1 + constant_iv_size])
    return protected, kid, iv_size, constant_iv, crypt_block, skip_block


def _sample_entry_children_offset(data: bytes, entry_type: bytes, body: int) -> int:
    """Offset of the first child box inside a sample entry whose body starts at `body`."""
    if entry_type in _VIDEO_ENTRY_TYPES:
        return body + 78
    if entry_type in _AUDIO_ENTRY_TYPES:
        (version,) = struct.unpack_from(">H", data, body + 8)
        if version == 1:
            return body + 44
        if version == 2:
            return body + 64
        return body + 28
    return body + 8


def _parse_sample_entry(data: bytes, index: int, entry_type: bytes, header: int, start: int, end: int) -> SampleEntry:
    body = start + header
    entry = SampleEntry(
        index=index,
        entry_type=entry_type.decode("latin-1"),
        data_format=entry_type.decode("latin-1"),
        scheme="",
        protected=False,
        kid=ZERO_KID,
        iv_size=0,
        constant_iv=b"",
        crypt_block=0,
        skip_block=0,
        offset=start,
        sinf_offset=None,
    )
    children = _sample_entry_children_offset(data, entry_type, body)
    sinf = None
    if children < end:
        try:
            sinf = _find_child(data, children, end, b"sinf")
        except ValueError:
            sinf = None
    if sinf is None:
        # An unusual sample entry version puts the children elsewhere; a scan for a
        # well-formed sinf inside the entry still finds it.
        probe = data.find(b"sinf", body, end)
        while probe != -1 and sinf is None:
            (size,) = struct.unpack_from(">I", data, probe - 4)
            if 8 <= size <= end - (probe - 4):
                sinf = (8, probe - 4, probe - 4 + size)
            else:
                probe = data.find(b"sinf", probe + 4, end)
    if sinf is None:
        return entry
    sinf_header, sinf_start, sinf_end = sinf
    entry.sinf_offset = sinf_start
    frma = _find_child(data, sinf_start + sinf_header, sinf_end, b"frma")
    if frma:
        entry.data_format = data[frma[1] + frma[0] : frma[1] + frma[0] + 4].decode("latin-1")
    schm = _find_child(data, sinf_start + sinf_header, sinf_end, b"schm")
    if schm:
        entry.scheme = data[schm[1] + schm[0] + 4 : schm[1] + schm[0] + 8].decode("latin-1")
    schi = _find_child(data, sinf_start + sinf_header, sinf_end, b"schi")
    if schi:
        tenc = _find_child(data, schi[1] + schi[0], schi[2], b"tenc")
        if tenc:
            (
                entry.protected,
                entry.kid,
                entry.iv_size,
                entry.constant_iv,
                entry.crypt_block,
                entry.skip_block,
            ) = _parse_tenc(data, tenc[1] + tenc[0], tenc[2])
    return entry


def _walk(data: bytes, start: int, end: int, visit: Callable[[bytes, int, int, int], None]) -> None:
    for box_type, header, box_start, box_end in iter_boxes(data, start, end):
        visit(box_type, header, box_start, box_end)
        if box_type in _CONTAINERS:
            _walk(data, box_start + header, box_end, visit)


def parse_moov(data: bytes, start: int = 0, end: Optional[int] = None) -> Layout:
    """Read the tracks, sample entries and `tenc` boxes out of a `moov` box at data[start:end]."""
    layout = Layout()
    current: list[TrackLayout] = []

    def visit(box_type: bytes, header: int, box_start: int, box_end: int) -> None:
        if box_type == b"trak":
            current.append(TrackLayout(track_id=0))
        elif box_type == b"tkhd" and current:
            version = data[box_start + header]
            id_offset = box_start + header + 4 + (16 if version == 1 else 8)
            (current[-1].track_id,) = struct.unpack_from(">I", data, id_offset)
        elif box_type == b"stsd" and current:
            (count,) = struct.unpack_from(">I", data, box_start + header + 4)
            pos = box_start + header + 8
            for index, (entry_type, entry_header, entry_start, entry_end) in enumerate(
                iter_boxes(data, pos, box_end), start=1
            ):
                if index > count:
                    break
                current[-1].entries.append(
                    _parse_sample_entry(data, index, entry_type, entry_header, entry_start, entry_end)
                )
            layout.tracks[current[-1].track_id] = current[-1]
        elif box_type == b"trex":
            track_id, default_index = struct.unpack_from(">II", data, box_start + header + 4)
            if track_id in layout.tracks and default_index:
                layout.tracks[track_id].default_entry_index = default_index
        elif box_type == b"pssh":
            layout.pssh_offsets.append(box_start)

    _walk(data, start, end if end is not None else len(data), visit)
    return layout


def inspect(path: Path) -> Optional[Layout]:
    """Return the protection layout of a fragmented MP4, or None when it has no `moov`."""
    with path.open("rb") as fh:
        for box_type, header, start, size in _top_level(fh):
            if box_type == b"moov":
                fh.seek(start)
                return parse_moov(fh.read(size))
    return None


def _top_level(fh: BinaryIO) -> Iterator[tuple[bytes, int, int, int]]:
    """Yield (type, header_size, start, size) for each top-level box, reading headers only."""
    fh.seek(0, 2)
    file_end = fh.tell()
    pos = 0
    while pos + 8 <= file_end:
        fh.seek(pos)
        head = fh.read(16)
        size, box_type = struct.unpack_from(">I4s", head, 0)
        header = 8
        if size == 1:
            (size,) = struct.unpack_from(">Q", head, 8)
            header = 16
        elif size == 0:
            size = file_end - pos
        if size < header:
            raise ValueError(f"Malformed box {box_type!r} at offset {pos}: size {size}")
        yield box_type, header, pos, size
        pos += size


@dataclass
class _Fragment:
    """Sample positions of one `traf`, resolved to absolute file offsets."""

    track_id: int
    entry_index: int
    samples: list[tuple[int, int]]
    ivs: list[bytes]
    subsamples: list[list[tuple[int, int]]]
    neutralise: list[int]


def _parse_moof(data: bytes, moof_offset: int, layout: Layout) -> list[_Fragment]:
    fragments: list[_Fragment] = []
    (_, moof_header, _, moof_end) = next(iter_boxes(data, 0, len(data)))
    for box_type, header, traf_start, traf_end in iter_boxes(data, moof_header, moof_end):
        if box_type == b"pssh":
            fragments.append(_Fragment(0, 0, [], [], [], [traf_start]))
        if box_type != b"traf":
            continue
        track_id = 0
        entry_index = 0
        default_size = 0
        base_offset: Optional[int] = None
        samples: list[tuple[int, int]] = []
        ivs: list[bytes] = []
        subsamples: list[list[tuple[int, int]]] = []
        neutralise: list[int] = []
        next_offset: Optional[int] = None
        senc: Optional[tuple[int, int, int]] = None
        for child, child_header, child_start, child_end in iter_boxes(data, traf_start + header, traf_end):
            # tfhd, trun and senc are full boxes: the payload follows version and flags.
            body = child_start + child_header + 4
            if child == b"tfhd":
                (flags,) = struct.unpack_from(">I", data, child_start + 8)
                flags &= 0xFFFFFF
                (track_id,) = struct.unpack_from(">I", data, body)
                pos = body + 4
                if flags & 0x1:
                    (base_offset,) = struct.unpack_from(">Q", data, pos)
                    pos += 8
                if flags & 0x2:
                    (entry_index,) = struct.unpack_from(">I", data, pos)
                    pos += 4
                if flags & 0x8:
                    pos += 4
                if flags & 0x10:
                    (default_size,) = struct.unpack_from(">I", data, pos)
                    pos += 4
            elif child == b"trun":
                (flags,) = struct.unpack_from(">I", data, child_start + 8)
                flags &= 0xFFFFFF
                (count,) = struct.unpack_from(">I", data, body)
                pos = body + 4
                data_offset = 0
                if flags & 0x1:
                    (data_offset,) = struct.unpack_from(">i", data, pos)
                    pos += 4
                if flags & 0x4:
                    pos += 4
                # Without an explicit base the samples follow the moof (default-base-is-moof
                # or the first traf), and a trun without data_offset continues after the last.
                base = base_offset if base_offset is not None else moof_offset
                if flags & 0x1:
                    start = base + data_offset
                elif next_offset is not None:
                    start = next_offset
                else:
                    start = base
                for _ in range(count):
                    size = default_size
                    if flags & 0x100:
                        pos += 4
                    if flags & 0x200:
                        (size,) = struct.unpack_from(">I", data, pos)
                        pos += 4
                    if flags & 0x400:
                        pos += 4
                    if flags & 0x800:
                        pos += 4
                    samples.append((start, size))
                    start += size
                next_offset = start
            elif child == b"senc":
                senc = (child_start, body, child_end)
                neutralise.append(child_start)
            elif child in {b"saiz", b"saio"}:
                neutralise.append(child_start)
        track = layout.tracks.get(track_id)
        if track is None:
            continue
        if not entry_index:
            entry_index = track.default_entry_index
        entry = next((e for e in track.entries if e.index == entry_index), None)
        if senc and entry:
            _, body, senc_end = senc
            (flags,) = struct.unpack_from(">I", data, senc[0] + 8)
            flags &= 0xFFFFFF
            (count,) = struct.unpack_from(">I", data, body)
            pos = body + 4
            for _ in range(count):
                ivs.append(bytes(data[pos : pos + entry.iv_size]))
                pos += entry.iv_size
                pairs: list[tuple[int, int]] = []
                if flags & 0x2:
                    (pair_count,) = struct.unpack_from(">H", data, pos)
                    pos += 2
                    for _ in range(pair_count):
                        clear, protected = struct.unpack_from(">HI", data, pos)
                        pairs.append((clear, protected))
                        pos += 6
                subsamples.append(pairs)
        fragments.append(_Fragment(track_id, entry_index, samples, ivs, subsamples, neutralise))
    return fragments


def decrypt_sample(
    buf: bytearray, offset: int, size: int, key: bytes, iv: bytes, entry: SampleEntry, pairs: list
) -> None:
    """Decrypt one cbcs sample in place. The IV restarts at every subsample."""
    ranges = pairs or [(0, size)]
    pos = offset
    for clear, protected in ranges:
        pos += clear
        cipher = AES.new(key, AES.MODE_CBC, iv)
        end = pos + protected
        if entry.crypt_block == 0:
            run = (protected // BLOCK) * BLOCK
            if run:
                buf[pos : pos + run] = cipher.decrypt(bytes(buf[pos : pos + run]))
        else:
            cursor = pos
            while end - cursor >= BLOCK:
                run = min(entry.crypt_block * BLOCK, ((end - cursor) // BLOCK) * BLOCK)
                buf[cursor : cursor + run] = cipher.decrypt(bytes(buf[cursor : cursor + run]))
                cursor += run + entry.skip_block * BLOCK
        pos = end


def _rename_box(buf: bytearray, offset: int, new_type: bytes) -> None:
    buf[offset + 4 : offset + 8] = new_type


def strip_moov_protection(moov: bytearray, layout: Layout) -> None:
    """Give `enca`/`encv` their codec 4CC back and turn `sinf`/`pssh` into `free`, in place."""
    for track in layout.tracks.values():
        for entry in track.entries:
            if entry.sinf_offset is None:
                continue
            moov[entry.offset + 4 : entry.offset + 8] = entry.data_format.encode("latin-1")
            _rename_box(moov, entry.sinf_offset, b"free")
    for offset in layout.pssh_offsets:
        _rename_box(moov, offset, b"free")


SampleDecrypter = Callable[[list[bytes]], list[bytes]]
"""Decrypts the protected samples of one fragment outside this module and returns them in order."""

KeyResolver = Callable[[TrackLayout, SampleEntry], Union[bytes, SampleDecrypter]]


def decrypt_file(path: Path, key_for: KeyResolver) -> None:
    """Decrypt every cbcs fragment of `path` in place, with the content key `key_for` names per sample entry.

    `key_for` gets the track and the `stsd` entry a fragment uses and returns the 16-byte key, or a
    `SampleDecrypter` for an entry whose key stays outside unshackle. A `SampleDecrypter` gets, per
    fragment and in sample order, the protected bytes of each sample cut to whole AES blocks, and
    must return plaintexts of the same lengths; a sample with no whole block is not passed.
    Raises NotImplementedError for a scheme other than cbcs, or for pattern encryption under a
    `SampleDecrypter`.
    """
    layout = inspect(path)
    if layout is None:
        raise ValueError(f"{path.name} has no moov box to decrypt against")
    unsupported = layout.schemes - {"cbcs"}
    if unsupported:
        raise NotImplementedError(f"In-process decryption handles cbcs only, not {', '.join(sorted(unsupported))}")

    keys: dict[tuple[int, int], Union[bytes, SampleDecrypter]] = {}
    for track in layout.tracks.values():
        for entry in track.protected_entries:
            resolved = key_for(track, entry)
            if callable(resolved) and entry.crypt_block:
                raise NotImplementedError(
                    f"A sample decrypter handles full-sample cbcs only, not the {entry.crypt_block}:"
                    f"{entry.skip_block} pattern of stsd entry {entry.index} of track {track.track_id}"
                )
            keys[(track.track_id, entry.index)] = resolved

    work_path = path.with_stem(f"{path.stem}_decrypted")
    shutil.copyfile(path, work_path)
    with work_path.open("r+b") as fh:
        for box_type, header, start, size in list(_top_level(fh)):
            if box_type == b"moov":
                fh.seek(start)
                moov = bytearray(fh.read(size))
                strip_moov_protection(moov, parse_moov(moov))
                fh.seek(start)
                fh.write(moov)
            elif box_type == b"moof":
                fh.seek(start)
                moof = bytearray(fh.read(size))
                fragments = _parse_moof(moof, start, layout)
                for fragment in fragments:
                    for offset in fragment.neutralise:
                        _rename_box(moof, offset, b"free")
                fh.seek(start)
                fh.write(moof)
                for fragment in fragments:
                    _decrypt_fragment(fh, fragment, layout, keys)
    path.unlink()
    shutil.move(work_path, path)


def _decrypt_fragment(
    fh: BinaryIO, fragment: _Fragment, layout: Layout, keys: dict[tuple[int, int], Union[bytes, SampleDecrypter]]
) -> None:
    if not fragment.samples:
        return
    track = layout.tracks[fragment.track_id]
    entry = next((e for e in track.entries if e.index == fragment.entry_index), None)
    if entry is None:
        raise ValueError(
            f"Fragment of track {fragment.track_id} names stsd entry {fragment.entry_index}, "
            f"which the moov does not have"
        )
    if not entry.protected:
        return
    key = keys[(track.track_id, entry.index)]
    span_start = min(offset for offset, _ in fragment.samples)
    span_end = max(offset + size for offset, size in fragment.samples)
    fh.seek(span_start)
    buf = bytearray(fh.read(span_end - span_start))
    if callable(key):
        _decrypt_with(key, buf, span_start, fragment)
    else:
        for number, (offset, size) in enumerate(fragment.samples):
            iv = fragment.ivs[number] if number < len(fragment.ivs) and fragment.ivs[number] else entry.constant_iv
            if len(iv) < BLOCK:
                iv = iv.ljust(BLOCK, b"\x00")
            pairs = fragment.subsamples[number] if number < len(fragment.subsamples) else []
            decrypt_sample(buf, offset - span_start, size, key, iv, entry, pairs)
    fh.seek(span_start)
    fh.write(buf)


def block_ranges(offset: int, size: int, pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return the (start, length) runs of one sample's protected bytes, cut to whole AES blocks in total.

    The runs follow the clear bytes of each subsample; the cbcs tail past the last whole block stays clear.
    """
    subsamples = pairs or [(0, size)]
    budget = (sum(protected for _, protected in subsamples) // BLOCK) * BLOCK
    runs: list[tuple[int, int]] = []
    pos = offset
    for clear, protected in subsamples:
        pos += clear
        take = min(protected, budget)
        if take:
            runs.append((pos, take))
            budget -= take
        pos += protected
    return runs


def _decrypt_with(decrypter: SampleDecrypter, buf: bytearray, span_start: int, fragment: _Fragment) -> None:
    """Hand the protected runs of every sample in `buf` to `decrypter` and write its plaintexts back in place."""
    samples: list[list[tuple[int, int]]] = []
    for number, (offset, size) in enumerate(fragment.samples):
        pairs = fragment.subsamples[number] if number < len(fragment.subsamples) else []
        runs = block_ranges(offset - span_start, size, pairs)
        if runs:
            samples.append(runs)
    if not samples:
        return
    ciphertexts = [b"".join(bytes(buf[start : start + length]) for start, length in runs) for runs in samples]
    plaintexts = decrypter(ciphertexts)
    if len(plaintexts) != len(ciphertexts):
        raise ValueError(f"The sample decrypter returned {len(plaintexts)} samples for {len(ciphertexts)}")
    for number, (cipher, plain) in enumerate(zip(ciphertexts, plaintexts)):
        if len(plain) != len(cipher):
            raise ValueError(f"The sample decrypter returned {len(plain)} bytes for sample {number}, not {len(cipher)}")
    for runs, plain in zip(samples, plaintexts):
        cursor = 0
        for start, length in runs:
            buf[start : start + length] = plain[cursor : cursor + length]
            cursor += length
