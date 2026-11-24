"""Drop the audio samples that run past a period join in a merged fragmented MP4."""

from __future__ import annotations

import logging
import mmap
from pathlib import Path
from typing import Collection, NamedTuple, Optional, cast

from unshackle.core.drm.verify import ENTRY_FIELDS, children, walk
from unshackle.core.manifests.ism_init import box

log = logging.getLogger("DASH")

TRIMMABLE_TRAF = {b"tfhd", b"tfdt", b"trun", b"senc", b"saiz", b"saio", b"sbgp", b"sgpd"}
GROUPING = (b"sbgp", b"sgpd")
CENC = {b"senc", b"saiz", b"saio"}

Runs = tuple[int, list[tuple[int, int]]]


def child_boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """(type, start, end) of each box in data[start:end]; unlike verify.children it stops at a size 0 or 64-bit box."""
    boxes = []
    while start + 8 <= end:
        size = int.from_bytes(data[start : start + 4], "big")
        if size < 8 or start + size > end:
            break
        boxes.append((data[start + 4 : start + 8], start, start + size))
        start += size
    return boxes


def be32(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 4], "big")


def aux_start(data: bytes, start: int) -> int:
    """The offset after the full box header and optional aux_info_type of a saiz or saio box at ``start``."""
    return start + 12 + 8 * (data[start + 11] & 0x1)


class FragmentSamples(NamedTuple):
    """The samples of a moof with one traf and one trun, and where trim_overlaps can cut them."""

    decode_time: int
    durations: list[int]
    sizes: list[int]
    traf: tuple[int, int]
    children: list[tuple[bytes, int, int]]
    fields: int
    width: int
    lead: int
    senc: list[int]
    groups: dict[int, Runs]
    trimmable: bool


def senc_ends(data: bytes, start: int, end: int, iv_size: int) -> Optional[list[int]]:
    """The end offset of each sample entry of a senc box, or None when the entries do not fill it exactly."""
    flags, pos, ends = be32(data, start + 8) & 0xFFFFFF, start + 16, []
    for _ in range(be32(data, start + 12)):
        pos += iv_size
        if flags & 0x2:
            if pos + 2 > end:
                return None
            pos += 2 + 6 * int.from_bytes(data[pos : pos + 2], "big")
        if pos > end:
            return None
        ends.append(pos)
    return ends if pos == end else None


def cenc_entries(
    data: bytes, named: dict[bytes, tuple[int, int]], moof: int, count: int, iv_size: int
) -> Optional[list[int]]:
    """The senc entry ends of a traf whose senc, saiz and saio describe ``count`` samples, saio pointing at senc."""
    if not CENC <= named.keys() or iv_size < 0:
        return None
    (senc, senc_end), (saiz, saiz_end), (saio, _) = named[b"senc"], named[b"saiz"], named[b"saio"]
    ends = senc_ends(data, senc, senc_end, iv_size)
    sizes, offsets = aux_start(data, saiz), aux_start(data, saio)
    width = 8 if data[saio + 8] == 1 else 4
    fits = (
        ends is not None
        and len(ends) == count
        and be32(data, sizes + 1) == count
        and bool(data[sizes] or sizes + 5 + count <= saiz_end)
        and be32(data, offsets) == 1
        and int.from_bytes(data[offsets + 4 : offsets + 4 + width], "big") == senc - moof + 16
    )
    return ends if fits else None


def group_runs(data: bytes, start: int, end: int) -> Optional[Runs]:
    """The entry count offset and (sample count, group index) runs of an sbgp box, or None when they overrun it."""
    at = start + 16 + 4 * (data[start + 8] == 1)
    count = be32(data, at)
    if at + 4 + 8 * count != end:
        return None
    return at, [(be32(data, at + 4 + 8 * n), be32(data, at + 8 + 8 * n)) for n in range(count)]


def read_samples(
    data: bytes, moof: tuple[int, int], mdat: Optional[tuple[int, int]], defaults: tuple[int, int], iv_size: int
) -> Optional[FragmentSamples]:
    """Parse a moof and the mdat right after it; None when it has no single traf with a tfdt and one bounded trun."""
    start, end = moof
    trafs = [b for b in child_boxes(data, start + 8, end) if b[0] == b"traf"]
    if len(trafs) != 1:
        return None
    traf = trafs[0][1:]
    kids = child_boxes(data, traf[0] + 8, traf[1])
    named = {k: (s, e) for k, s, e in kids}
    truns = [c for c in kids if c[0] == b"trun"]
    if b"tfdt" not in named or b"tfhd" not in named or len(truns) != 1 or mdat is None:
        return None
    tfdt, tfdt_end = named[b"tfdt"]
    tfdt_width = 8 if data[tfdt + 8] == 1 else 4
    tfhd, tfhd_end = named[b"tfhd"]
    tfhd_flags = be32(data, tfhd + 8) & 0xFFFFFF
    _, trun, trun_end = truns[0]
    flags, count = be32(data, trun + 8) & 0xFFFFFF, be32(data, trun + 12)
    fields = trun + 16 + 4 * bool(flags & 0x1) + 4 * bool(flags & 0x4)
    width = 4 * bin(flags & 0xF00).count("1")
    duration, size = defaults
    at = tfhd + 16 + 8 * bool(tfhd_flags & 0x1) + 4 * bool(tfhd_flags & 0x2)
    if tfhd_flags & 0x8:
        duration, at = be32(data, at), at + 4
    if tfhd_flags & 0x10:
        size, at = be32(data, at), at + 4
    payload = mdat[1] - mdat[0] - 8
    if (
        tfdt + 12 + tfdt_width > tfdt_end
        or at > tfhd_end
        or fields + count * width > trun_end
        or (not width and count * max(size, 1) > payload)
    ):
        return None
    decode_time = int.from_bytes(data[tfdt + 12 : tfdt + 12 + tfdt_width], "big")
    durations, sizes = [], []
    for entry in range(fields, fields + count * width, width) if width else [fields] * count:
        durations.append(be32(data, entry) if flags & 0x100 else duration)
        sizes.append(be32(data, entry + 4 * bool(flags & 0x100)) if flags & 0x200 else size)

    lead = int.from_bytes(data[trun + 16 : trun + 20], "big", signed=True) - (end - start) - 8
    groups = {s: runs for k, s, e in kids if k == b"sbgp" and (runs := group_runs(data, s, e))}
    single = [k for k, _, _ in kids if k not in GROUPING]
    ends = cenc_entries(data, named, start, count, iv_size) if CENC & named.keys() else []
    trimmable = (
        ends is not None
        and set(named) <= TRIMMABLE_TRAF
        and len(single) == len(set(single))
        and len(groups) == sum(k == b"sbgp" for k, _, _ in kids)
        and all(sum(n for n, _ in runs) == count for _, runs in groups.values())
        and not tfhd_flags & 0x1
        and bool(flags & 0x1)
        and 0 <= lead
        and lead + sum(sizes) <= payload
    )
    return FragmentSamples(
        decode_time, durations, sizes, traf, kids, fields, width, lead, ends or [], groups, trimmable
    )


def rebuild_fragment(
    data: bytes, moof: tuple[int, int], mdat: tuple[int, int], fragment: FragmentSamples, kept: int
) -> bytes:
    """The moof and mdat of a fragment cut to its first ``kept`` samples, with trun, senc, saiz, saio and sbgp to match."""
    count = kept.to_bytes(4, "big")
    pieces: list[bytes] = []
    at: dict[bytes, int] = {}
    for kind, s, e in fragment.children:
        at[kind] = sum(map(len, pieces))
        if kind == b"trun":
            piece = box(kind, data[s + 8 : s + 12] + count + data[s + 16 : fragment.fields + kept * fragment.width])
        elif kind == b"senc":
            piece = box(kind, data[s + 8 : s + 12] + count + data[s + 16 : fragment.senc[kept - 1]])
        elif kind == b"saiz":
            p = aux_start(data, s)
            piece = box(kind, data[s + 8 : p + 1] + count + (b"" if data[p] else data[p + 5 : p + 5 + kept]))
        elif kind == b"sbgp":
            p, runs = fragment.groups[s]
            cut, left = b"", kept
            for samples, group in runs:
                take = min(samples, left)
                if not take:
                    break
                cut += take.to_bytes(4, "big") + group.to_bytes(4, "big")
                left -= take
            piece = box(kind, data[s + 8 : p] + (len(cut) // 8).to_bytes(4, "big") + cut)
        else:
            piece = data[s:e]
        pieces.append(piece)
    traf_start, traf_end = fragment.traf
    out = bytearray(data[moof[0] : traf_start] + box(b"traf", b"".join(pieces)) + data[traf_end : moof[1]])
    out[0:4] = len(out).to_bytes(4, "big")
    traf_body = traf_start - moof[0] + 8
    trun = traf_body + at[b"trun"] + 16
    out[trun : trun + 4] = (len(out) + 8 + fragment.lead).to_bytes(4, "big", signed=True)
    if b"saio" in at:
        saio = traf_body + at[b"saio"]
        field, width = aux_start(out, saio) + 4, 8 if out[saio + 8] == 1 else 4
        out[field : field + width] = (traf_body + at[b"senc"] + 16).to_bytes(width, "big")
    payload = data[mdat[0] + 8 : mdat[0] + 8 + fragment.lead + sum(fragment.sizes[:kept])]
    return bytes(out) + box(b"mdat", payload)


def sample_iv_size(data: bytes, start: int, end: int) -> int:
    """The per-sample IV size in the first CENC tenc of a moov, or -1 when it has none."""
    for kind, body, box_end in walk(data, start, end):
        if kind != b"stsd":
            continue
        for entry_kind, entry, entry_end in children(data, body + 8, box_end):
            for fields in ENTRY_FIELDS.get(entry_kind, ()):
                boxes = list(walk(data, entry + fields, entry_end))
                if any(k == b"sinf" for k, _, _ in boxes):
                    return next((data[b + 7] for k, b, e in boxes if k == b"tenc" and e - b >= 8), -1)
    return -1


def trim_overlaps(path: Path, joins: Collection[int]) -> int:
    """Drop the samples of a fragment that start after the period join that follows it; returns how many it dropped.

    A restarted period begins at the manifest end of the one before it, but that period's audio can run past
    its end, and a player plays every sample in turn, so each join would push the audio later. ``joins`` holds
    the decode time of each period start; a sample before one stays while its middle falls before it. A file
    it cannot read to the end stays as it is, and a sidx becomes a free box, as the fragment sizes change.
    """
    if not joins or not path.stat().st_size:
        return 0
    temp = path.with_suffix(path.suffix + ".trim")
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            dropped = write_trimmed(cast(bytes, mapped), temp, joins)
        if dropped:
            temp.replace(path)
        return dropped
    finally:
        temp.unlink(missing_ok=True)


def write_trimmed(data: bytes, temp: Path, joins: Collection[int]) -> int:
    """Write ``data`` with the samples past each join dropped to ``temp``; returns 0 and writes nothing when none."""
    top = child_boxes(data, 0, len(data))
    moov = next(((s, e) for k, s, e in top if k == b"moov"), None)
    if not moov or top[-1][2] != len(data):
        return 0
    trex = next((b for k, b, _ in walk(data, moov[0] + 8, moov[1]) if k == b"trex"), None)
    defaults = (be32(data, trex + 12), be32(data, trex + 16)) if trex is not None else (0, 0)
    iv_size = sample_iv_size(data, moov[0] + 8, moov[1])

    fragments: list[tuple[int, FragmentSamples]] = []
    for i, (kind, start, end) in enumerate(top):
        if kind == b"moof":
            mdat = top[i + 1][1:] if i + 1 < len(top) and top[i + 1][0] == b"mdat" else None
            if fragment := read_samples(data, (start, end), mdat, defaults, iv_size):
                fragments.append((i, fragment))

    keep: dict[int, int] = {}
    untrimmable = 0
    for (i, fragment), (_, following) in zip(fragments, fragments[1:]):
        join = following.decode_time
        if join not in joins or fragment.decode_time + sum(fragment.durations) <= join:
            continue
        if not fragment.trimmable:
            untrimmable += 1
            continue
        kept, at = 0, fragment.decode_time
        for duration in fragment.durations:
            if at + duration / 2 >= join:
                break
            kept, at = kept + 1, at + duration
        if kept < len(fragment.durations):
            keep[i] = kept
    if untrimmable:
        log.debug(f"Left {untrimmable} overlapping audio fragments it cannot rewrite: {temp.stem}")
    if not keep:
        return 0

    by_index = dict(fragments)
    with open(temp, "wb") as out:
        for i, (kind, start, end) in enumerate(top):
            if i - 1 in keep and kind == b"mdat":
                continue
            if kind == b"sidx":
                out.write(data[start : start + 4] + b"free" + data[start + 8 : end])
            elif i in keep:
                if keep[i]:
                    out.write(rebuild_fragment(data, (start, end), top[i + 1][1:], by_index[i], keep[i]))
            else:
                out.write(data[start:end])
    return sum(len(by_index[i].durations) - kept for i, kept in keep.items())
