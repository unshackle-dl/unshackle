import io
import random
import struct
import zlib
from pathlib import Path

import pytest
import segno

from unshackle.core.console import read_qr_image

URL = "https://login.example/scan?session=EXAMPLE&code=0123456789abcdef"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "qr"
INK, PAPER = (20, 40, 120), (250, 240, 200)


def segno_png(data: str = URL, **options) -> bytes:
    make = {k: options.pop(k) for k in ("version", "error", "mask", "eci") if k in options}
    buf = io.BytesIO()
    segno.make_qr(data, **make).save(buf, kind="png", **options)
    return buf.getvalue()


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png_file(header: bytes, raw: bytes, extra: bytes = b"") -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    return sig + chunk(b"IHDR", header) + extra + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def filtered(kind: int, line: bytes, prev: bytes, bpp: int) -> bytes:
    out = bytearray()
    for i, x in enumerate(line):
        a, b, c = (line[i - bpp], prev[i], prev[i - bpp]) if i >= bpp else (0, prev[i], 0)
        out.append((x - (0, a, b, (a + b) // 2, paeth(a, b, c))[kind]) & 255)
    return bytes(out)


def encode_png(
    dark_px: tuple,
    light_px: tuple,
    colour: int,
    depth: int = 8,
    scale: float = 3,
    border: int = 4,
    flip: tuple = (),
    caption: bool = False,
    **chunks: bytes,
) -> bytes:
    """A PNG of URL in any colour type and bit depth, each row with the next of the five filters.

    *flip* lists symbol modules (row, column) to invert; *caption* adds a dark bar under the code.
    """
    matrix = [list(row) for row in segno.make_qr(URL).matrix_iter(border=border)]
    for r, c in flip:
        matrix[r + border][c + border] ^= 1
    if caption:
        width = len(matrix[0])
        matrix += (
            [[0] * width] * 2 + [[int(width // 3 < x < 2 * width // 3) for x in range(width)]] * 2 + [[0] * width] * 2
        )
    side = int(len(matrix[0]) * scale)
    channels = len(dark_px)
    bpp = max(1, channels * depth // 8)
    raw, prev = bytearray(), b""
    for y in range(int(len(matrix) * scale)):
        samples = [v for x in range(side) for v in (dark_px if matrix[int(y / scale)][int(x / scale)] else light_px)]
        if depth == 16:
            line = b"".join(v.to_bytes(2, "big") for v in samples)
        elif depth == 8:
            line = bytes(samples)
        else:
            bits = "".join(format(v, f"0{depth}b") for v in samples)
            line = int(bits.ljust(-(-len(bits) // 8) * 8, "0"), 2).to_bytes(-(-len(bits) // 8), "big")
        prev = prev or bytes(len(line))
        raw += bytes([y % 5]) + filtered(y % 5, line, prev, bpp)
        prev = line
    header = struct.pack(">IIBBBBB", side, int(len(matrix) * scale), depth, colour, 0, 0, 0)
    extra = b"".join(chunk(kind.encode(), body) for kind, body in chunks.items())
    return png_file(header, bytes(raw), extra)


@pytest.mark.parametrize("version", [1, 2, 7, 10, 25, 40])
def test_each_version_gives_its_text(version: int) -> None:
    assert read_qr_image(segno_png("LOGIN-EXAMPLE", version=version, scale=2)) == "LOGIN-EXAMPLE"


@pytest.mark.parametrize("error", ["L", "M", "Q", "H"])
def test_each_error_correction_level_gives_its_text(error: str) -> None:
    assert read_qr_image(segno_png(error=error)) == URL


@pytest.mark.parametrize("mask", range(8))
def test_each_mask_gives_its_text(mask: int) -> None:
    assert read_qr_image(segno_png(mask=mask)) == URL


@pytest.mark.parametrize("scale", [1, 3, 8])
@pytest.mark.parametrize("border", [0, 4])
def test_each_scale_and_border_gives_its_text(scale: int, border: int) -> None:
    assert read_qr_image(segno_png(scale=scale, border=border)) == URL


@pytest.mark.parametrize("scale", [1.37, 2.6, 4.5])
def test_a_resize_by_a_fraction_gives_its_text(scale: float) -> None:
    assert read_qr_image(encode_png((0,), (255,), colour=0, scale=scale)) == URL


@pytest.mark.parametrize(
    "png",
    [
        pytest.param(lambda: segno_png(), id="grey-1bit"),
        pytest.param(lambda: encode_png((0,), (3,), colour=0, depth=2), id="grey-2bit"),
        pytest.param(lambda: encode_png((0,), (255,), colour=0), id="grey-8bit"),
        pytest.param(lambda: encode_png((0x00FF,), (0xFF00,), colour=0, depth=16), id="grey-16bit"),
        pytest.param(lambda: encode_png((128,), (0,), colour=0, tRNS=struct.pack(">H", 0)), id="grey-key-clear-black"),
        pytest.param(lambda: segno_png(dark="darkblue", light="#ffff99"), id="palette-coloured"),
        pytest.param(lambda: segno_png(light=None), id="palette-transparent"),
        pytest.param(
            lambda: encode_png((1,), (0,), colour=3, depth=4, PLTE=bytes((0, 0, 0) + INK), tRNS=b"\x00"),
            id="palette-4bit-clear-black",
        ),
        pytest.param(lambda: encode_png(INK, PAPER, colour=2), id="rgb-coloured"),
        pytest.param(
            lambda: encode_png(INK, (0, 0, 0), colour=2, tRNS=struct.pack(">3H", 0, 0, 0)), id="rgb-key-clear-black"
        ),
        pytest.param(lambda: encode_png((5000, 9000, 30000), (65535,) * 3, colour=2, depth=16), id="rgb-16bit"),
        pytest.param(lambda: encode_png((0, 255), (0, 0), colour=4), id="grey-alpha-clear-black"),
        pytest.param(lambda: encode_png((0, 0, 0, 255), (255, 255, 255, 255), colour=6), id="rgba-black-on-white"),
        pytest.param(lambda: encode_png((*INK, 255), (0, 0, 0, 0), colour=6), id="rgba-coloured-on-clear-black"),
    ],
)
def test_each_png_colour_type_gives_its_text(png) -> None:
    assert read_qr_image(png()) == URL


@pytest.mark.parametrize(
    "text",
    [
        "0123456789012",
        "HELLO WORLD $%*+-./:",
        "caf\u00e9 latin-1",
        "\u20ac utf-8 \U0001f511",
        "\u6f22\u5b57\u30c6\u30b9\u30c8\u4e55\u500f\u51dc",
    ],
    ids=["numeric", "alphanumeric", "latin-1", "utf-8", "kanji"],
)
def test_each_data_mode_gives_its_text(text: str) -> None:
    assert read_qr_image(segno_png(text)) == text


def test_an_eci_header_is_skipped() -> None:
    assert read_qr_image(segno_png("\u20ac utf-8", eci=True)) == "\u20ac utf-8"


@pytest.mark.parametrize("name", ["qrcode_pypng.png", "qrcode_pillow_rgba.png"])
def test_png_from_another_encoder_gives_its_text(name: str) -> None:
    """One 1-bit PNG from a pure encoder; one coloured RGBA PNG resized 1.37x with bilinear smoothing."""
    assert (
        read_qr_image((FIXTURES / name).read_bytes())
        == "https://login.example/activate?session=EXAMPLE&code=QRCODE-1234"
    )


def test_a_damaged_payload_module_gives_none_not_wrong_text() -> None:
    size = segno.make_qr(URL).symbol_size(border=0)[0]
    assert read_qr_image(encode_png((0,), (255,), colour=0, flip=[(size - 11, size - 1)])) is None


def test_a_damaged_top_left_format_copy_reads_from_the_other_copy() -> None:
    assert read_qr_image(encode_png((0,), (255,), colour=0, flip=[(8, c) for c in range(6)])) == URL


def test_format_information_reads_with_two_wrong_bits_in_each_copy() -> None:
    size = segno.make_qr(URL).symbol_size(border=0)[0]
    flip = [(8, 0), (8, 2), (8, size - 1), (8, size - 3)]
    assert read_qr_image(encode_png((0,), (255,), colour=0, flip=flip)) == URL


def test_a_caption_under_the_code_is_ignored() -> None:
    assert read_qr_image(encode_png((0,), (255,), colour=0, caption=True)) == URL


def test_an_image_over_the_size_limit_is_not_decoded(monkeypatch) -> None:
    from unshackle.core.utils import qr_read

    def no_unfilter(*args: object) -> None:
        raise AssertionError("decoded an image over the limit")

    monkeypatch.setattr(qr_read, "_unfilter", no_unfilter)
    side = 2100
    header = struct.pack(">IIBBBBB", side, side, 8, 6, 0, 0, 0)
    assert read_qr_image(png_file(header, bytes((side * 4 + 1) * side))) is None


def interlaced(png: bytes) -> bytes:
    return png[:28] + b"\x01" + png[29:]


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(None, id="none"),
        pytest.param(b"not an image", id="text"),
        pytest.param(segno_png()[:60], id="truncated"),
        pytest.param(interlaced(segno_png()), id="interlaced"),
        pytest.param(encode_png((255,), (255,), colour=0), id="blank"),
        pytest.param(
            encode_png((0,), (255,), colour=0)[:16] + struct.pack(">II", 100000, 100000) + b"\x08\x00", id="huge"
        ),
        pytest.param(segno.make_micro("12345").png_data_uri().encode(), id="data-uri"),
    ],
)
def test_bytes_that_hold_no_readable_code_give_none(data: bytes) -> None:
    assert read_qr_image(data) is None


def test_a_dark_square_with_no_code_gives_none() -> None:
    side = 40
    raw = b"".join(b"\x00" + bytes(0 if 5 <= x < 35 and 5 <= y < 35 else 255 for x in range(side)) for y in range(side))
    header = struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0)
    assert read_qr_image(png_file(header, raw)) is None


def test_corrupted_bytes_never_raise() -> None:
    rng = random.Random(187)
    png = encode_png((0,), (255,), colour=0, scale=1)
    for _ in range(300):
        data = bytearray(png)
        for _ in range(rng.randint(1, 8)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        read_qr_image(bytes(data))
