import struct
from pathlib import Path
from typing import Optional
from uuid import UUID

from unshackle.core.drm.verify import PIFF_TENC, PIFF_TFXD, KidMap, kid_windows, pick_windows

A, B, C = UUID(int=0xA), UUID(int=0xB), UUID(int=0xC)


def box(kind: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), kind) + body


def full(kind: bytes, payload: bytes, version: int = 0, flags: int = 0) -> bytes:
    return box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def encv(kid: UUID, piff: bool = False) -> bytes:
    tenc_body = b"\x00\x00\x01\x08" + kid.bytes
    tenc = box(b"uuid", PIFF_TENC, bytes(4) + tenc_body) if piff else full(b"tenc", tenc_body)
    return box(b"encv", bytes(78), box(b"sinf", box(b"frma", b"avc1"), box(b"schi", tenc)))


def enca(kid: UUID) -> bytes:
    return box(b"enca", bytes(28), box(b"sinf", box(b"schi", full(b"tenc", b"\x00\x00\x01\x08" + kid.bytes))))


def seig(protected: bool, kid: UUID) -> bytes:
    return bytes([0, 0, int(protected), 8]) + kid.bytes


def sgpd(*entries: bytes, version: int = 1) -> bytes:
    head = b"seig"
    if version >= 1:
        head += struct.pack(">I", 20)
    if version >= 2:
        head += struct.pack(">I", 0)
    return full(b"sgpd", head + struct.pack(">I", len(entries)) + b"".join(entries), version=version)


def sbgp(*runs: tuple[int, int]) -> bytes:
    return full(b"sbgp", b"seig" + struct.pack(">I", len(runs)) + b"".join(struct.pack(">II", *run) for run in runs))


def movie(*sample_entries: bytes, handler: bytes = b"vide", stbl: bytes = b"") -> bytes:
    stsd = full(b"stsd", struct.pack(">I", len(sample_entries)) + b"".join(sample_entries))
    hdlr = full(b"hdlr", bytes(4) + handler + bytes(12))
    trak = box(
        b"trak",
        full(b"tkhd", bytes(8) + struct.pack(">I", 1)),
        box(b"mdia", full(b"mdhd", bytes(8) + struct.pack(">I", 1000)), hdlr, box(b"minf", box(b"stbl", stsd, stbl))),
    )
    return box(b"ftyp", b"iso6") + box(b"moov", trak, box(b"mvex", full(b"trex", struct.pack(">III", 1, 1, 0))))


def moof(
    entry: int,
    time: int,
    *groups: bytes,
    samples: int = 48,
    duration: Optional[int] = None,
    tfxd: bool = False,
) -> bytes:
    flags = 0x02 | (0x08 if duration is not None else 0)
    tfhd = struct.pack(">II", 1, entry) + (struct.pack(">I", duration) if duration is not None else b"")
    if tfxd:
        timing = box(b"uuid", PIFF_TFXD, bytes([1, 0, 0, 0]) + struct.pack(">QQ", time, 0))
    else:
        timing = full(b"tfdt", struct.pack(">Q", time), version=1)
    trun = full(b"trun", struct.pack(">I", samples))
    return box(b"moof", box(b"traf", full(b"tfhd", tfhd, flags=flags), timing, trun, *groups)) + box(b"mdat", bytes(64))


def local(kid: UUID, protected: bool = True) -> tuple[bytes, bytes]:
    return sgpd(seig(protected, kid)), sbgp((48, 0x10001))


def test_each_kid_maps_to_its_own_fragments(tmp_path: Path) -> None:
    path = tmp_path / "enc.mp4"
    path.write_bytes(
        movie(encv(A), encv(B))
        + moof(1, 5000)  # lead under A
        + moof(2, 7000)  # the rest under B
        + moof(2, 9000)
        + moof(1, 11000, *local(C))  # a seig group overrides the sample entry's KID
        + moof(1, 13000, *local(A, protected=False))  # a clear seig group names no KID
    )
    assert kid_windows(path) == KidMap({A: [(0.0, 2.0)], B: [(2.0, 6.0), (4.0, 6.0)], C: [(6.0, 8.0)]}, True)


def test_windows_run_through_a_kid_but_stop_at_another_or_after_the_longest() -> None:
    long_run = [(t * 2.0, t * 2.0 + 2) for t in range(20)]
    assert pick_windows(long_run, longest=10.0) == [(0.0, 10.0), (20.0, 30.0), (30.0, 40.0)]
    assert pick_windows(long_run) == [(0.0, 4.0), (20.0, 24.0), (36.0, 40.0)]
    # a gap is another KID or a clear stretch: no window crosses it, and the last one ends the last run
    assert pick_windows([(0, 2), (2, 4), (10, 12), (12, 14), (14, 16)], longest=10.0) == [(0, 4), (10, 16)]
    assert pick_windows([(0, 2), (2, 4), (10, 12), (12, 14), (14, 16)]) == [(0, 4), (10, 14.0), (12, 16)]


def test_fragments_longer_than_the_longest_and_empty_spans() -> None:
    assert pick_windows([(0.0, 60.0)]) == [(0.0, 4.0)]
    assert pick_windows([(0, 15), (15, 30), (30, 45), (45, 60)]) == [(0, 4), (30, 34), (45, 49)]
    assert pick_windows([(0.0, 5.0), (5.0, 5.0)]) == [(0.0, 4.0)]
    assert pick_windows([(5.0, 5.0)]) == []


def test_nested_box_bomb_and_oversized_sbgp(tmp_path: Path) -> None:
    body = b""
    for _ in range(5000):
        body = box(b"trak", body)
    (tmp_path / "bomb.mp4").write_bytes(box(b"moov", body))
    assert kid_windows(tmp_path / "bomb.mp4") is None
    huge = full(b"sbgp", b"seig" + struct.pack(">I", 0x00FFFFFF) + bytes(16))
    (tmp_path / "sbgp.mp4").write_bytes(movie(encv(A)) + moof(1, 0, huge))
    assert kid_windows(tmp_path / "sbgp.mp4") is None


def test_first_middle_and_last_fragment(tmp_path: Path) -> None:
    path = tmp_path / "enc.mp4"
    path.write_bytes(movie(encv(A)) + b"".join(moof(1, t * 1000) for t in range(5)))
    assert kid_windows(path) == KidMap({A: [(0.0, 4.0), (2.0, 6.0), (3.0, 7.0)]}, True)


def test_last_fragment_ends_after_its_sample_durations(tmp_path: Path) -> None:
    path = tmp_path / "enc.mp4"
    path.write_bytes(movie(encv(A)) + moof(1, 0, samples=10, duration=250))
    assert kid_windows(path) == KidMap({A: [(0.0, 2.5)]}, True)


def test_audio_track_is_not_video(tmp_path: Path) -> None:
    path = tmp_path / "enc.m4a"
    path.write_bytes(movie(enca(A), handler=b"soun") + moof(1, 0))
    assert kid_windows(path) == KidMap({A: [(0.0, 3.0)]}, False)


def test_moov_seig_table_survives_another_grouping(tmp_path: Path) -> None:
    roll = full(b"sgpd", b"roll" + struct.pack(">II", 2, 1) + b"\x00\x01", version=1)
    path = tmp_path / "enc.mp4"
    path.write_bytes(movie(encv(A), stbl=sgpd(seig(True, C)) + roll) + moof(1, 0, sbgp((48, 1))))
    assert kid_windows(path) == KidMap({C: [(0.0, 3.0)]}, True)


def test_sgpd_version_2(tmp_path: Path) -> None:
    path = tmp_path / "enc.mp4"
    path.write_bytes(movie(encv(A)) + moof(1, 0, sgpd(seig(True, C), version=2), sbgp((48, 0x10001))))
    assert kid_windows(path) == KidMap({C: [(0.0, 3.0)]}, True)


def test_sgpd_version_0_with_constant_iv(tmp_path: Path) -> None:
    constant_iv = bytes([0, 0, 1, 0]) + B.bytes + b"\x08" + bytes(8)
    groups = sgpd(constant_iv, seig(True, C), version=0)
    path = tmp_path / "enc.mp4"
    path.write_bytes(movie(encv(A)) + moof(1, 0, groups, sbgp((24, 0x10001), (24, 0x10002))))
    assert kid_windows(path) == KidMap({B: [(0.0, 3.0)], C: [(0.0, 3.0)]}, True)


def test_samples_past_the_sbgp_runs_take_the_sample_entry_kid(tmp_path: Path) -> None:
    path = tmp_path / "enc.mp4"
    path.write_bytes(movie(encv(A)) + moof(1, 0, sgpd(seig(True, C)), sbgp((10, 0x10001))))
    assert kid_windows(path) == KidMap({A: [(0.0, 3.0)], C: [(0.0, 3.0)]}, True)


def test_piff_tenc_and_tfxd(tmp_path: Path) -> None:
    path = tmp_path / "enc.ismv"
    path.write_bytes(movie(encv(A, piff=True)) + moof(1, 0, tfxd=True) + moof(1, 2000, tfxd=True))
    assert kid_windows(path) == KidMap({A: [(0.0, 4.0), (2.0, 5.0)]}, True)


def test_oversized_box(tmp_path: Path) -> None:
    path = tmp_path / "enc.mp4"
    path.write_bytes(movie(encv(A)) + moof(1, 0) + struct.pack(">I4s", 0x7FFFFFFF, b"moof") + bytes(16))
    assert kid_windows(path) is None


def test_not_a_fragmented_mp4(tmp_path: Path) -> None:
    path = tmp_path / "video.ts"
    path.write_bytes(b"\x47" * 188 * 4)
    assert kid_windows(path) is None
