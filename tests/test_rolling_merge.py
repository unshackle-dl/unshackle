import io
import random
from concurrent.futures import Future
from pathlib import Path

from unshackle.core.manifests.dash import RollingMerge, append_segment


def write_segments(save_dir: Path, payloads: list[bytes]) -> int:
    save_dir.mkdir()
    width = len(str(len(payloads)))
    for i, payload in enumerate(payloads):
        (save_dir / f"{i:0{width}d}.mp4").write_bytes(payload)
    return width


def expected(init: bytes, payloads: list[bytes], is_text_subtitle: bool, tmp_path: Path) -> bytes:
    scratch = tmp_path / "scratch"
    width = write_segments(scratch, payloads)
    out = io.BytesIO()
    out.write(init)
    for i in range(len(payloads)):
        append_segment(out, scratch / f"{i:0{width}d}.mp4", is_text_subtitle)
    return out.getvalue()


def test_out_of_order_segments_match_whole_merge(tmp_path: Path) -> None:
    rng = random.Random(7)
    payloads = [rng.randbytes(rng.randint(1, 4096)) for _ in range(23)]
    init = b"ftyp-moov"
    want = expected(init, payloads, False, tmp_path)

    save_dir = tmp_path / "segments"
    width = write_segments(save_dir, payloads)
    dst = io.BytesIO()
    dst.write(init)
    merger = RollingMerge(dst)

    order = list(range(len(payloads)))
    rng.shuffle(order)
    for index in order:
        future: Future = Future()
        future.set_result(None)
        merger.add(index, save_dir / f"{index:0{width}d}.mp4", future if index % 2 else None)
        # the cursor never rests on a segment that is ready: every contiguous run is drained
        assert merger.merged not in merger.ready

    assert merger.merged == len(payloads)
    assert dst.getvalue() == want
    assert dst.getvalue().startswith(init)
    assert list(save_dir.iterdir()) == []
