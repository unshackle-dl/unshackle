"""Tests for the paged AAC container: its pages, the ADTS stream and the cut of an M4A file (``unshackle.core.drm.paged_aac``)."""

from __future__ import annotations

import struct

import pytest

from unshackle.core.drm.paged_aac import (
    PAGED_AAC,
    adts_header,
    audio_config,
    checksum,
    cut,
    first_frame,
    frame_sizes,
    pages,
    to_adts,
)

HEADER_PAGE = bytes.fromhex(
    "53704143800318700901006d7034614cd13b000244ac00ff1300080400eb8a08"
    "00020000f47790698988807c798188788a6a9c6e8174947c7b897f778b7b7c88"
    "7d7f807c8288777d7f79857789798177817c798479887a74857f837979828576"
    "8076897a7d7c7e847a77916c8678867184818d72797792778c7c8277907f8479"
    "7f758f67967c1d000000f03b0007200400030000000000000000c7b02a9d"
)
CONFIG = bytes.fromhex("eb8a0800")
HEADER, SHARED = 0x80, 0x01


def page(flags: int, records: list[bytes], number: bytes = b"", shared: bytes = b"") -> bytes:
    """One page of a paged file around ``records``: with ``shared``, each record is stored without its first byte."""
    stored = [record[len(shared) :] for record in records]
    table = b"".join(bytes([255] * (len(record) // 255) + [len(record) % 255]) for record in stored)
    body = PAGED_AAC + bytes([flags, len(table)]) + number + table + shared + b"".join(stored)
    return body + struct.pack("<I", checksum(body))


def config_record(config: bytes = CONFIG, true: int = 3920204, rate: int = 44100) -> bytes:
    """The first record of a header page, with the audio config at its end."""
    return (
        b"\x01\x00mp4a"
        + struct.pack("<I", true)
        + b"\x02"
        + struct.pack("<H", rate)
        + b"\x00\xff\x13\x00\x08"
        + struct.pack("<H", len(config))
        + config
    )


def header(data: bytes = b"", total: int = 0, first_record: bytes | None = None) -> bytes:
    """A header page for ``data``.

    Its records are the audio config, ``total`` samples with the byte length of ``data``, and one more that nothing reads.
    """
    return page(
        HEADER,
        [
            config_record() if first_record is None else first_record,
            b"\x02" + bytes(103) + struct.pack("<II", total, len(data)),
            b"\x03" + bytes(8),
        ],
    )


def file(*data: bytes) -> bytes:
    """A paged file: a header page that declares the byte length of ``data``, then those pages."""
    return header(b"".join(data)) + b"".join(data)


def adts_frames(stream: bytes) -> list[bytes]:
    """The frames of an ADTS stream, without their headers."""
    frames, pos = [], 0
    while pos < len(stream):
        assert stream[pos : pos + 2] == b"\xff\xf1"
        length = ((stream[pos + 3] & 3) << 11) | (stream[pos + 4] << 3) | (stream[pos + 5] >> 5)
        frames.append(stream[pos + 7 : pos + length])
        pos += length
    return frames


def fletcher_32(data: bytes) -> int:
    """The page checksum by its definition, one word at a time."""
    total = running = 0xFFFF
    for word in (int.from_bytes(data[pos : pos + 2], "little") for pos in range(0, len(data), 2)):
        total = (total + word) % 0xFFFF or 0xFFFF
        running = (running + total) % 0xFFFF or 0xFFFF
    return running << 16 | total


FIRST = page(0, [b"\x01abc", b"\x00de"], number=b"\x00\x01")
LATER = page(0, [b"\x01xy"], number=b"\x00\x04")


def test_the_header_page_of_a_real_file_passes_its_checksum() -> None:
    assert checksum(HEADER_PAGE[:-4]) == 0x9D2AB0C7
    assert checksum(b"") == 0xFFFFFFFF


@pytest.mark.parametrize("data", [b"\x01", b"\x01\x02\x03", bytes(6), b"\xff\xff" * 300, bytes(range(256))])
def test_the_checksum_matches_its_definition(data: bytes) -> None:
    assert checksum(data) == fletcher_32(data)


def test_the_header_page_of_a_real_file_names_the_rate_and_the_channels() -> None:
    flags, number, records = next(pages(HEADER_PAGE))

    assert (flags, number) == (HEADER, 0)
    assert [len(record) for record in records] == [24, 112, 9]
    assert audio_config(records) == (7, 1)


def test_the_header_page_of_a_real_file_declares_the_length_of_its_data_pages() -> None:
    with pytest.raises(ValueError, match="truncated: 158 bytes of 270501"):
        to_adts(HEADER_PAGE)


def test_the_header_page_of_a_real_file_gives_the_cut_of_its_m4a_file() -> None:
    assert cut(HEADER_PAGE) == (6081, 3920204, 44100)


@pytest.mark.parametrize(
    ("config", "true", "total", "expected"),
    [
        pytest.param(CONFIG, 1000, 7081, (6081, 1000, 44100), id="fits"),
        pytest.param(CONFIG, 1000, 7080, None, id="one-sample-short"),
        pytest.param(CONFIG, 0, 7081, None, id="no-samples"),
        pytest.param(bytes.fromhex("138800"), 1000, 7081, None, id="aac-lc"),
    ],
)
def test_the_cut_needs_he_aacv2_and_a_sample_count_that_fits(
    config: bytes, true: int, total: int, expected: tuple[int, int, int] | None
) -> None:
    assert cut(header(FIRST, total=total, first_record=config_record(config, true=true)) + FIRST) == expected


def test_the_cut_reads_the_rate_of_the_header_page() -> None:
    data = header(FIRST, total=7081, first_record=config_record(true=1000, rate=48000)) + FIRST

    assert cut(data) == (6081, 1000, 48000)


def test_no_cut_comes_from_a_header_page_without_the_sample_counts() -> None:
    assert cut(page(HEADER, [config_record(), b"\x02" + bytes(7)]) + FIRST) is None


def test_each_frame_gets_the_adts_header_of_the_header_page() -> None:
    long = b"\x01" + bytes(range(256)) * 2
    second = page(SHARED, [b"\x01xy", b"\x01z"], number=b"\x00\x03", shared=b"\x01")

    stream = to_adts(file(page(0, [b"\x01abc", long], number=b"\x00\x01"), second))

    assert stream[:7] == bytes.fromhex("fff15c40017ffc")
    assert adts_frames(stream) == [b"\x01abc", long, b"\x01xy", b"\x01z"]


def test_a_stored_frame_of_255_bytes_laces_as_255_then_0() -> None:
    shared = page(SHARED, [b"\xff" + bytes(255)], number=b"\x00\x01", shared=b"\xff")

    assert shared[8:11] == bytes([255, 0, 255])
    assert adts_frames(to_adts(file(shared))) == [b"\xff" + bytes(255)]


def test_a_lacing_entry_of_255_continues_the_frame() -> None:
    assert frame_sizes(bytes([255, 3, 7])) == [258, 7]


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        (b"\x00\x01", (1, 2)),
        (b"\x81\x00\x00", (0x10000, 3)),
        (b"\xc1\x00\x00\x00", (0x1000000, 4)),
        (b"\xe0\x12\x34\x56\x78", (0x12345678, 5)),
    ],
)
def test_the_number_of_the_first_frame_grows_with_its_value(field: bytes, expected: tuple[int, int]) -> None:
    assert first_frame(field, 0) == expected


def test_a_frame_too_long_for_adts_raises() -> None:
    with pytest.raises(ValueError, match="too long for ADTS"):
        adts_header(0x1FFF, 7, 1)


def test_an_empty_file_has_no_page() -> None:
    assert list(pages(b"")) == []


@pytest.mark.parametrize("padding", [1, 2, 3, 4, 4096])
def test_zero_bytes_after_the_last_page_are_dropped(padding: int) -> None:
    assert to_adts(file(FIRST) + bytes(padding)) == to_adts(file(FIRST))


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        pytest.param(file(FIRST, FIRST), "out of sequence", id="repeated-page"),
        pytest.param(file(FIRST, LATER), "out of sequence", id="skipped-page"),
        pytest.param(file(FIRST)[:-1], "is truncated", id="cut-inside-a-page"),
        pytest.param(file(FIRST, LATER)[: -len(LATER)], "is truncated", id="cut-at-a-page-boundary"),
        pytest.param(file(FIRST)[:-4] + bytes(4), "Checksum mismatch", id="wrong-checksum"),
        pytest.param(
            file(FIRST).replace(PAGED_AAC + b"\x00", PAGED_AAC + b"\x02", 1), "no valid page", id="unknown-flag"
        ),
        pytest.param(
            file(page(HEADER, [bytes(10)], number=b"\x00\x01")), "no valid page", id="header-flag-on-a-data-page"
        ),
        pytest.param(FIRST + header(), "no valid page", id="no-header-on-the-first-page"),
        pytest.param(
            header(FIRST + bytes(20)) + FIRST + bytes(20), "no valid page", id="zeros-inside-the-declared-length"
        ),
        pytest.param(
            file(PAGED_AAC + b"\x00\x01\x00\x01\xff" + bytes(255) + bytes(4)),
            "continues past its page",
            id="frame-past-the-page",
        ),
        pytest.param(header(FIRST, first_record=bytes(24)) + FIRST, "no AudioSpecificConfig", id="no-audio-config"),
        pytest.param(
            header(FIRST, first_record=config_record(bytes.fromhex("200800"))) + FIRST,
            "cannot carry",
            id="aac-that-adts-cannot-carry",
        ),
        pytest.param(header(), "no AAC frame", id="no-data-page"),
        pytest.param(
            file(page(0, [b"\x01" + bytes(0x1FFF)], number=b"\x00\x01")), "too long for ADTS", id="frame-too-long"
        ),
        pytest.param(b"OggS" + bytes(200), "no valid page", id="not-a-paged-file"),
        pytest.param(header()[:-1], "runs past the end", id="truncated-header"),
        pytest.param(file(FIRST) + LATER, "not zero after", id="page-after-the-declared-end"),
        pytest.param(file(FIRST) + b"\x01", "not zero after", id="byte-after-the-declared-end"),
        pytest.param(page(HEADER, [config_record()]) + FIRST, "declares no length", id="no-length-record"),
    ],
)
def test_a_file_that_is_not_well_formed_raises(data: bytes, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        to_adts(data)
