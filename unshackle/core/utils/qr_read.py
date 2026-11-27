"""Read the text of a QR code from a PNG image, with the standard library and the QR tables of segno."""

import struct
import zlib

from segno import consts, encoder

MAX_RAW_BYTES = 1 << 24
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def read_qr_image(data: bytes) -> str | None:
    """Give the text of the QR code in a PNG image, or None when the bytes hold no QR code that reads.

    The code must be axis-aligned and dark on a light background, as an API renders it. Transparency counts
    as white. An interlaced PNG gives None, and so does a damaged code: there is no error correction.
    """
    try:
        dark = _dark_rows(_grey_rows(data))
        box = _bounding_box(dark)
        size = _symbol_size(dark, *box)
        return _decode(_sample(dark, *box, size))
    except (ValueError, IndexError, KeyError, TypeError, struct.error, zlib.error):
        return None


def _grey_rows(data: bytes) -> list[bytes]:
    """The luminance of each pixel, row by row, with alpha composited over white."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, header, palette, trns, idat = 8, b"", b"", b"", []
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = body
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", header)
    channels = CHANNELS[colour]
    bpp = max(1, channels * depth // 8)
    stride = (width * channels * depth + 7) // 8
    if interlace or depth not in (1, 2, 4, 8, 16) or not width or not 0 < (stride + 1) * height <= MAX_RAW_BYTES:
        raise ValueError("unsupported PNG")
    raw = zlib.decompressobj().decompress(b"".join(idat), (stride + 1) * height)
    if len(raw) < (stride + 1) * height:
        raise ValueError("PNG data ends early")
    if colour == 3:
        alpha = trns.ljust(len(palette) // 3, b"\xff")
        lut = bytes(_over(_luma(*palette[i * 3 : i * 3 + 3]), alpha[i]) for i in range(len(palette) // 3))
    else:
        lut = bytes(v * 255 // ((1 << depth) - 1) for v in range(1 << depth)) if depth < 8 else bytes(range(256))
    key = (
        tuple(v >> 8 if depth == 16 else v for v in struct.unpack(f">{len(trns) // 2}H", trns))
        if colour in (0, 2)
        else ()
    )
    if colour == 0 and key:
        lut = lut[: key[0]] + b"\xff" + lut[key[0] + 1 :]
    lut = lut.ljust(256, b"\x00")
    per, mask = max(1, 8 // depth), (1 << depth) - 1
    rows, prev = [], bytes(stride)
    for y in range(height):
        line = _unfilter(
            raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)]), prev, bpp
        )
        prev = line
        s = bytes(line[::2] if depth == 16 else line)
        if depth < 8:
            s = bytes(s[i // per] >> (8 - depth * (i % per + 1)) & mask for i in range(width))
        if colour in (0, 3):
            rows.append(s.translate(lut))
        elif colour == 2:
            rows.append(bytes(255 if px == key else _luma(*px) for px in zip(s[0::3], s[1::3], s[2::3], strict=True)))
        elif colour == 4:
            rows.append(bytes(map(_over, s[0::2], s[1::2])))
        else:
            rows.append(bytes(map(_over, map(_luma, s[0::4], s[1::4], s[2::4]), s[3::4])))
    return rows


def _unfilter(kind: int, line: bytearray, prev: bytes, bpp: int) -> bytearray:
    """Undo one of the five PNG scanline filters."""
    if kind == 1:
        for i in range(bpp, len(line)):
            line[i] = (line[i] + line[i - bpp]) & 255
    elif kind == 2:
        for i in range(len(line)):
            line[i] = (line[i] + prev[i]) & 255
    elif kind == 3:
        for i in range(len(line)):
            line[i] = (line[i] + ((line[i - bpp] if i >= bpp else 0) + prev[i]) // 2) & 255
    elif kind == 4:
        for i in range(len(line)):
            a, b, c = (line[i - bpp], prev[i], prev[i - bpp]) if i >= bpp else (0, prev[i], 0)
            pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
            line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
    elif kind:
        raise ValueError("unknown PNG filter")
    return line


def _luma(r: int, g: int, b: int) -> int:
    return (299 * r + 587 * g + 114 * b) // 1000


def _over(grey: int, alpha: int) -> int:
    return (grey * alpha + 255 * (255 - alpha)) // 255


def _dark_rows(rows: list[bytes]) -> list[bytes]:
    """Each pixel as 1 for dark or 0 for light, split at the middle of the darkest and lightest grey."""
    low, high = min(map(min, rows)), max(map(max, rows))
    if high - low < 32:
        raise ValueError("no contrast")
    table = bytes(int(v < (low + high + 1) // 2) for v in range(256))
    return [row.translate(table) for row in rows]


def _bounding_box(dark: list[bytes]) -> tuple[int, int, int, int]:
    """Left, top, width and height of the symbol, from the top edges of its two upper finder patterns.

    The width comes from the top dark row and the height from the left edge, so a caption under the code does not count.
    """
    top = min(y for y, row in enumerate(dark) if 1 in row)
    left, right = dark[top].find(1), dark[top].rfind(1)
    bottom = bytes(row[left] for row in dark).rfind(1)
    return left, top, right - left + 1, bottom - top + 1


def _symbol_size(dark: list[bytes], left: int, top: int, width: int, height: int) -> int:
    """Modules per side: the size whose row 6 and column 6 sample as finder, timing pattern and finder."""
    for size in range(21, 178, 4):
        line = bytes(1 if c < 7 or c >= size - 7 else 0 if c in (7, size - 8) else (c + 1) % 2 for c in range(size))
        row = dark[top + 13 * height // (2 * size)]
        x6 = left + 13 * width // (2 * size)
        if _sample_line(row, left, width, size) == line == _sample_line(bytes(r[x6] for r in dark), top, height, size):
            return size
    raise ValueError("no QR timing pattern")


def _sample_line(pixels: bytes, start: int, length: int, size: int) -> bytes:
    """The *size* module centres of a line of pixels."""
    return bytes(pixels[start + (2 * i + 1) * length // (2 * size)] for i in range(size))


def _sample(dark: list[bytes], left: int, top: int, width: int, height: int, size: int) -> list[bytes]:
    """The module grid, read at the centre of each module."""
    return [_sample_line(dark[top + (2 * r + 1) * height // (2 * size)], left, width, size) for r in range(size)]


def _decode(matrix: list[bytes]) -> str | None:
    """The text of a sampled module grid; None for a symbol with no data."""
    size = len(matrix)
    version = (size - 17) // 4
    top_left = [(i, 8) for i in range(6)] + [(7, 8), (8, 8), (8, 7)] + [(8, 14 - i) for i in range(9, 15)]
    other = [(8, size - 1 - i) for i in range(8)] + [(size - 15 + i, 8) for i in range(8, 15)]
    formats = [sum(matrix[r][c] << i for i, (r, c) in enumerate(cells)) for cells in (top_left, other)]
    distance, index = min(((v ^ code).bit_count(), i) for v in formats for i, code in enumerate(consts.FORMAT_INFO))
    if distance > 3:
        raise ValueError("unreadable format information")
    level, mask = divmod(index, 8)

    layout = encoder.make_matrix(size, size)
    encoder.add_finder_patterns(layout, size, size)
    encoder.add_alignment_patterns(layout, size, size)
    masked = encoder.get_data_mask_functions(False)[mask]
    bits, upward = [], True
    for right in [*range(size - 1, 7, -2), *range(5, 0, -2)]:
        for r in range(size - 1, -1, -1) if upward else range(size):
            bits += [matrix[r][c] ^ masked(r, c) for c in (right, right - 1) if layout[r][c] == 0x2]
        upward = not upward
    codewords = int("".join(map(str, bits[: len(bits) // 8 * 8])), 2).to_bytes(len(bits) // 8, "big")

    groups = consts.ECC[version][level]  # type: ignore[index]
    blocks = [(ec.num_data, ec.num_total - ec.num_data) for ec in groups for _ in range(ec.num_blocks)]
    data = [bytearray() for _ in blocks]
    ecc = [bytearray() for _ in blocks]
    pos = 0
    for i in range(max(d for d, _ in blocks)):
        for block, (d, _) in zip(data, blocks, strict=True):
            if i < d:
                block.append(codewords[pos])
                pos += 1
    for _ in range(blocks[0][1]):
        for block in ecc:
            block.append(codewords[pos])
            pos += 1
    message = encoder.Buffer()
    for byte in b"".join(data):
        message.append_bits(byte, 8)
    if encoder.make_blocks(groups, message)[1] != ecc:
        raise ValueError("QR error correction check failed")
    return _parse(b"".join(data), version)


def _parse(data: bytes, version: int) -> str | None:
    """The text of the data bit stream: numeric, alphanumeric, byte and kanji segments, ECI skipped."""
    value, left = int.from_bytes(data, "big"), len(data) * 8

    def read(n: int) -> int:
        nonlocal left
        if n > left:
            raise ValueError("QR data ends early")
        left -= n
        return (value >> left) & ((1 << n) - 1)

    chars = consts.ALPHANUMERIC_CHARS.decode()
    text = []
    while left >= 4 and (mode := read(4)):
        if mode == consts.MODE_ECI:
            first = read(8)
            read(0 if first < 0x80 else 8 if first < 0xC0 else 16)
            continue
        if mode == consts.MODE_STRUCTURED_APPEND:
            read(16)
            continue
        count = read(consts.CHAR_COUNT_INDICATOR_LENGTH[mode][encoder.version_range(version)])
        if mode == consts.MODE_NUMERIC:
            groups = [3] * (count // 3) + [count % 3] * bool(count % 3)
            text += [f"{read(3 * k + 1):0{k}d}" for k in groups]
        elif mode == consts.MODE_ALPHANUMERIC:
            for _ in range(count // 2):
                pair = read(11)
                text.append(chars[pair // 45] + chars[pair % 45])
            if count % 2:
                text.append(chars[read(6)])
        elif mode == consts.MODE_BYTE:
            raw = bytes(read(8) for _ in range(count))
            try:
                text.append(raw.decode("utf-8"))
            except UnicodeDecodeError:
                text.append(raw.decode("latin-1"))
        elif mode == consts.MODE_KANJI:
            pairs = [divmod(read(13), 0xC0) for _ in range(count)]
            words = [(a << 8 | b) + (0x8140 if (a << 8 | b) < 0x1F00 else 0xC140) for a, b in pairs]
            text.append(b"".join(w.to_bytes(2, "big") for w in words).decode(consts.KANJI_ENCODING))
        else:
            raise ValueError("unsupported QR mode")
    return "".join(text) or None
