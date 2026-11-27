"""A decrypted whole file in a paged AAC container: its pages, the ADTS stream that players and FFmpeg read, and the cut of an M4A file."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from itertools import accumulate

PAGED_AAC = b"SpAC"
HEADER, SHARED = 0x80, 0x01
CODER_DELAY = 6081


def checksum(data: bytes) -> int:
    """The Fletcher-32 of a page: the sum of its 16-bit little-endian words and of their running sums, both from 0xFFFF."""
    words = [word for (word,) in struct.iter_unpack("<H", data + b"\0" * (len(data) & 1))]
    total = (0xFFFF + sum(words)) % 0xFFFF or 0xFFFF
    running = (0xFFFF * (len(words) + 1) + sum(accumulate(words))) % 0xFFFF or 0xFFFF
    return running << 16 | total


def first_frame(data: bytes, pos: int) -> tuple[int, int]:
    """The number of the first frame of a page and the length of its field: big-endian, with the length in its first byte."""
    field = data[pos : pos + 5]
    if len(field) < 2:
        raise ValueError("The paged file ends inside the number of a page")
    lead = field[0]
    if lead < 0x80:
        size, number = 2, lead
    elif lead < 0xC0:
        size, number = 3, lead & 0x3F
    elif lead < 0xE0:
        size, number = 4, lead & 0x1F
    else:
        size, number = 5, 0
    if len(field) < size:
        raise ValueError("The paged file ends inside the number of a page")
    return number << (8 * (size - 1)) | int.from_bytes(field[1:size], "big"), size


def frame_sizes(table: bytes) -> list[int]:
    """The sizes that the lacing table of a page lists: an entry of 255 continues a frame, any other entry ends it."""
    sizes: list[int] = []
    pending = 0
    for entry in table:
        pending += entry
        if entry < 255:
            sizes.append(pending)
            pending = 0
    if pending:
        raise ValueError("A frame of the paged file continues past its page")
    return sizes


def pages_end(data: bytes, header_end: int, records: list[bytes]) -> int:
    """Where the data pages end: the header page plus the byte length that its second record declares.

    The file must hold every page, and only zero bytes may follow the last one.
    """
    record = records[1] if len(records) > 1 else b""
    if len(record) < 5 or record[0] != 2:
        raise ValueError("The second record of the header page of the paged file declares no length")
    end = header_end + struct.unpack("<I", record[-4:])[0]
    if end > len(data):
        raise ValueError(f"The paged file is truncated: {len(data)} bytes of {end}")
    if data[end:].strip(b"\0"):
        raise ValueError(
            f"The paged file holds {len(data) - end} bytes that are not zero after its last page at offset {end}"
        )
    return end


def pages(data: bytes) -> Iterator[tuple[int, int, list[bytes]]]:
    """Yield the flags, the number of the first frame (0 for the header page) and the frames of each page.

    The header page declares the byte length of the data pages: they must end exactly where ``pages_end`` says.
    A page with unknown flags, a truncated page and a page that fails its checksum raise ``ValueError``.
    """
    pos = 0
    stop = len(data)
    while pos < stop:
        head = data[pos : pos + 6]
        flags = head[4] if len(head) == 6 else -1
        if len(head) < 6 or head[:4] != PAGED_AAC or flags & ~(HEADER | SHARED) or bool(flags & HEADER) != (pos == 0):
            raise ValueError(f"The paged file holds no valid page at offset {pos}")
        cursor = pos + 6
        number = 0
        if not flags & HEADER:
            number, size = first_frame(data, cursor)
            cursor += size
        count = head[5]
        sizes = frame_sizes(data[cursor : cursor + count])
        cursor += count
        shared = data[cursor : cursor + 1] if flags & SHARED else b""
        cursor += len(shared)
        end = cursor + sum(sizes) + 4
        if end > stop:
            raise ValueError(f"The page at offset {pos} of the paged file runs past the end of the pages")
        if checksum(data[pos : end - 4]) != int.from_bytes(data[end - 4 : end], "little"):
            raise ValueError(f"Checksum mismatch on the page at offset {pos} of the paged file")
        frames: list[bytes] = []
        for size in sizes:
            frames.append(shared + data[cursor : cursor + size])
            cursor += size
        yield flags, number, frames
        pos = end
        if flags & HEADER:
            stop = pages_end(data, pos, frames)


def cut(data: bytes) -> tuple[int, int, int] | None:
    """The cut of the M4A file made from a paged file: the coder delay, the sample count and the rate that its header page names.

    ``CODER_DELAY`` is the delay of the HE-AACv2 coder. None unless the audio is HE-AACv2 and the cut fits its samples.
    """
    records = next(pages(data), (0, 0, []))[2]
    first = records[0] if records else b""
    second = records[1] if len(records) > 1 else b""
    if len(first) < 21 or first[20] >> 3 != 29 or len(second) < 108:
        return None
    true, rate = struct.unpack_from("<I", first, 6)[0], struct.unpack_from("<H", first, 11)[0]
    if not 0 < true <= struct.unpack_from("<I", second, 104)[0] - CODER_DELAY or not rate:
        return None
    return CODER_DELAY, true, rate


def audio_config(records: list[bytes]) -> tuple[int, int]:
    """The sampling rate index and the channel configuration that the first record of the header page names."""
    record = records[0] if records else b""
    config = record[20:]
    length = struct.unpack_from("<H", record, 18)[0] if len(record) >= 20 else -1
    if record[:6] != b"\x01\x00mp4a" or length != len(config) or length < 3:
        raise ValueError("The header page of the paged file holds no AudioSpecificConfig")
    kind, rate, channels = config[0] >> 3, ((config[0] & 7) << 1) | (config[1] >> 7), (config[1] >> 3) & 0xF
    if kind not in (2, 5, 29) or rate > 12 or not 1 <= channels <= 7:
        raise ValueError(
            f"The paged file holds AAC that ADTS cannot carry: object type {kind}, rate index {rate}, {channels} channels"
        )
    return rate, channels


def adts_header(size: int, rate: int, channels: int) -> bytes:
    """The 7-byte ADTS header of one AAC-LC frame: SBR and PS stay implicit, as ADTS cannot name them."""
    length = size + 7
    if length > 0x1FFF:
        raise ValueError(f"An AAC frame of {size} bytes is too long for ADTS")
    return bytes(
        (
            0xFF,
            0xF1,
            1 << 6 | rate << 2 | channels >> 2,
            (channels & 3) << 6 | length >> 11,
            length >> 3 & 0xFF,
            (length & 7) << 5 | 0x1F,
            0xFC,
        )
    )


def to_adts(data: bytes) -> bytes:
    """Return one ADTS stream: a 7-byte header before every AAC frame of the paged file."""
    out: list[bytes] = []
    rate = channels = 0
    expected = 1
    for flags, number, frames in pages(data):
        if flags & HEADER:
            rate, channels = audio_config(frames)
            continue
        if number != expected:
            raise ValueError(
                f"The page that starts at frame {number} of the paged file is out of sequence: frame {expected} is due"
            )
        expected += len(frames)
        out.extend(adts_header(len(frame), rate, channels) + frame for frame in frames)
    if expected == 1:
        raise ValueError("The paged file holds no AAC frame")
    return b"".join(out)
