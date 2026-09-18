import os
import struct
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, NamedTuple, Optional
from uuid import UUID

DECRYPT_HOOK: Optional[Callable[..., None]] = None

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"mvex", b"moof", b"traf", b"sinf", b"schi"}
PIFF_TENC = bytes.fromhex("8974dbce7be74c5184f97148f9882554")
PIFF_TFXD = bytes.fromhex("6d1d9b0542d544e680e2141daff757b2")
TENC = {b"tenc", b"uuid" + PIFF_TENC}
# the fixed sample entry fields before the child boxes: visual; sound v0 (and ISO v1), QuickTime sound v1 and v2
ENTRY_FIELDS = {b"encv": (78,), b"enca": (28, 44, 64)}

Seig = tuple[bool, UUID]
Fragment = tuple[int, int, int, set[UUID]]  # track, decode time, duration, KIDs


class KidMap(NamedTuple):
    """The fragment windows of each KID in a fragmented MP4."""

    windows: dict[UUID, list[tuple[float, float]]]  # KID -> (start, end) seconds of its first, middle and last fragment
    video: bool  # the encrypted track is video


def decrypt_track(
    drm: Any,
    path: Path,
    licence: Optional[Callable] = None,
    track_kid: Optional[UUID] = None,
    decrypt: bool = True,
) -> None:
    """Decrypt ``path`` with ``drm`` through the installed hook, or directly when there is none.

    ``decrypt=False`` means the downloader already decrypted the segments in place; the hook
    then only checks the result, because there is no ciphertext left to retry with.
    """
    if DECRYPT_HOOK:
        DECRYPT_HOOK(drm, path, licence, track_kid, decrypt)
    elif decrypt:
        drm.decrypt(path)


def children(data: bytes, start: int = 0, end: Optional[int] = None) -> Iterator[tuple[bytes, int, int]]:
    """Yield (type, body start, box end) for each box in ``data[start:end]``."""
    end = len(data) if end is None else end
    while start + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, start)
        head = 8
        if size == 1:
            size, head = struct.unpack_from(">Q", data, start + 8)[0], 16
        elif size == 0:
            size = end - start
        if size < head or start + size > end:
            return
        if kind == b"uuid":
            kind, head = b"uuid" + data[start + head : start + head + 16], head + 16
        yield kind, start + head, start + size
        start += size


def walk(data: bytes, start: int = 0, end: Optional[int] = None) -> Iterator[tuple[bytes, int, int]]:
    """Yield every box below ``data[start:end]``, descending into the container boxes."""
    for kind, body, box_end in children(data, start, end):
        yield kind, body, box_end
        if kind in CONTAINERS:
            yield from walk(data, body, box_end)


def seig_entries(data: bytes, body: int) -> Optional[list[Seig]]:
    """Parse an ``sgpd`` box. Returns its seig entries as (protected, KID), or None for another grouping."""
    version = data[body]
    if data[body + 4 : body + 8] != b"seig":
        return None
    pos, default_length = body + 8, 0
    if version >= 1:
        default_length = struct.unpack_from(">I", data, pos)[0]
        pos += 4
    if version >= 2:
        pos += 4  # default_group_description_index
    count = struct.unpack_from(">I", data, pos)[0]
    pos += 4
    entries = []
    for _ in range(count):
        length = default_length
        if version >= 1 and not length:
            length = struct.unpack_from(">I", data, pos)[0]
            pos += 4
        protected, iv_size = data[pos + 2], data[pos + 3]
        if version == 0:
            # version 0 gives no entry length; a constant IV follows the KID when samples carry no IV
            length = 20 + (1 + data[pos + 20] if protected and not iv_size else 0)
        entries.append((bool(protected), UUID(bytes=data[pos + 4 : pos + 20])))
        pos += length
    return entries


def sbgp_runs(data: bytes, body: int) -> Optional[list[tuple[int, int]]]:
    """Parse an ``sbgp`` box. Returns its seig runs as (sample count, group index), or None for another grouping."""
    version = data[body]
    if data[body + 4 : body + 8] != b"seig":
        return None
    pos = body + (12 if version == 1 else 8)
    count = struct.unpack_from(">I", data, pos)[0]
    if count * 8 > len(data) - pos - 4:
        raise ValueError("sbgp entry count is larger than the box")
    runs = struct.unpack_from(f">{count * 2}I", data, pos + 4)
    return list(zip(runs[::2], runs[1::2]))


def entry_kid(data: bytes, kind: bytes, entry: int, end: int) -> Optional[UUID]:
    """Return the default KID of an encrypted sample entry, or None for a clear one."""
    for fields in ENTRY_FIELDS.get(kind, ()):
        boxes = list(walk(data, entry + fields, end))
        if not any(box_kind == b"sinf" for box_kind, _, _ in boxes):
            continue
        for box_kind, body, _ in boxes:
            if box_kind in TENC:
                # CENC isProtected and the last byte of the PIFF AlgorithmID share this offset; both are 0 for clear
                return UUID(bytes=data[body + 8 : body + 24]) if data[body + 6] else None
        return None
    return None


def sample_entries(data: bytes, body: int, end: int) -> list[Optional[UUID]]:
    """Parse an ``stsd`` box. Returns the default KID of each sample entry, None for a clear one."""
    return [entry_kid(data, kind, entry, entry_end) for kind, entry, entry_end in children(data, body + 8, end)]


def read_top_boxes(f: BinaryIO) -> Iterator[tuple[bytes, Optional[bytes]]]:
    """Yield each top-level box type, with the body of a moov or moof. Every other body is skipped.

    Raises ValueError when a moov or moof claims more bytes than the file has left.
    """
    file_size = os.fstat(f.fileno()).st_size
    while True:
        head = f.read(8)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head)
        body_size = size - 8
        if size == 1:
            body_size = struct.unpack(">Q", f.read(8))[0] - 16
        elif size == 0:
            return
        if body_size < 0:
            return
        if kind in (b"moov", b"moof"):
            if body_size > file_size - f.tell():
                raise ValueError(f"{kind!r} box is larger than the rest of the file")
            yield kind, f.read(body_size)
        else:
            f.seek(body_size, 1)
            yield kind, None


def kid_windows(path: Path) -> Optional[KidMap]:
    """Map each KID that encrypts part of a fragmented MP4 to a few fragment time windows, in seconds.

    Reads only the moov and moof boxes of the ciphertext and skips every mdat, so a large
    file costs little. A fragment takes the KID of its sample entry's tenc (or PIFF tenc),
    unless a seig sample group names another KID or marks the samples clear. For each KID
    the result holds up to three (start, end) windows from ``pick_windows``, relative to
    the first fragment of the track. A fragment ends where the next fragment of the track
    starts. The last fragment ends after its sample durations, or after 3 seconds when
    they are not known. Returns None when the file has no encrypted fragment this reader
    can place in time, so the caller must use another check.
    """
    timescales: dict[int, int] = {}
    handlers: dict[int, bytes] = {}
    entries: dict[int, list[Optional[UUID]]] = {}
    defaults: dict[int, tuple[int, int]] = {}
    moov_groups: dict[int, list[Seig]] = {}
    fragments: list[Fragment] = []
    clock: dict[int, int] = {}
    try:
        with path.open("rb") as f:
            for kind, box in read_top_boxes(f):
                if kind == b"moov" and box:
                    for trak_kind, trak, trak_end in children(box):
                        if trak_kind == b"mvex":
                            for kind_, body, _ in children(box, trak, trak_end):
                                if kind_ == b"trex":
                                    track, index, duration = struct.unpack_from(">III", box, body + 4)
                                    defaults[track] = (index, duration)
                        if trak_kind != b"trak":
                            continue
                        track = 0
                        for kind_, body, box_end in walk(box, trak, trak_end):
                            if kind_ == b"tkhd":
                                track = struct.unpack_from(">I", box, body + (20 if box[body] == 1 else 12))[0]
                            elif kind_ == b"mdhd":
                                timescale = struct.unpack_from(">I", box, body + (20 if box[body] == 1 else 12))[0]
                                timescales[track] = timescale
                            elif kind_ == b"hdlr":
                                # the mdia handler comes first; a QuickTime minf can hold a data handler after it
                                handlers.setdefault(track, box[body + 8 : body + 12])
                            elif kind_ == b"stsd":
                                entries[track] = sample_entries(box, body, box_end)
                            elif kind_ == b"sgpd":
                                groups = seig_entries(box, body)
                                if groups is not None:
                                    moov_groups[track] = groups
                elif kind == b"moof" and box:
                    fragments.extend(read_fragment(box, entries, defaults, moov_groups, clock))
    except (OSError, struct.error, IndexError, ValueError, MemoryError, OverflowError, RecursionError):
        return None

    tracks: dict[int, list[Fragment]] = {}
    for fragment in fragments:
        tracks.setdefault(fragment[0], []).append(fragment)
    spans: dict[UUID, list[tuple[float, float]]] = {}
    video = False
    for track, track_fragments in tracks.items():
        timescale = timescales.get(track)
        if not timescale:
            return None
        track_fragments.sort(key=lambda fragment: fragment[1])
        first = track_fragments[0][1]
        for i, (_, time, duration, kids) in enumerate(track_fragments):
            if not kids:
                continue
            start = (time - first) / timescale
            if i + 1 < len(track_fragments):
                end = (track_fragments[i + 1][1] - first) / timescale
            else:
                end = start + (duration / timescale if duration else 3.0)
            video = video or handlers.get(track) == b"vide"
            for kid in kids:
                spans.setdefault(kid, []).append((start, end))
    if not spans:
        return None
    return KidMap({kid: pick_windows(fragment_spans) for kid, fragment_spans in spans.items()}, video)


def pick_windows(spans: list[tuple[float, float]], longest: float = 4.0) -> list[tuple[float, float]]:
    """Pick up to three windows from one KID's fragment (start, end) spans: at its first, middle and last fragment.

    A window starts at a fragment and runs on through the next fragments of the same KID, up
    to ``longest`` seconds, but never into a fragment of another KID or a clear one. A short
    window can decode noise without an error: the decoder often needs several frames to find it.
    A wrong key fails on the first inter frame in every measured case, so ``longest`` buys
    decoder frames, not certainty; every second of it is decoded in full for a right key.
    The last window ends where the KID's last run of fragments ends.
    """
    spans = sorted(span for span in spans if span[1] > span[0])  # a duplicated tfdt gives an empty span
    if not spans:
        return []
    runs: list[list[float]] = []  # [start, end] of each run of back-to-back fragments
    for start, end in spans:
        if runs and abs(start - runs[-1][1]) <= 1e-3:
            runs[-1][1] = end
        else:
            runs.append([start, end])

    def run_end(start: float) -> float:
        return next(end for run_start, end in runs if run_start <= start < end)

    last_from = max(runs[-1][0], runs[-1][1] - longest)
    last = max(i for i, (start, _) in enumerate(spans) if start <= last_from + 1e-3)
    return [(spans[i][0], min(run_end(spans[i][0]), spans[i][0] + longest)) for i in sorted({0, len(spans) // 2, last})]


def trun_totals(moof: bytes, truns: list[int], default_duration: int) -> tuple[int, int]:
    """Return the sample count and the summed sample duration of the ``trun`` boxes at the given body offsets."""
    samples = ticks = 0
    for body in truns:
        flags = int.from_bytes(moof[body + 1 : body + 4], "big")
        count = struct.unpack_from(">I", moof, body + 4)[0]
        samples += count
        if flags & 0x100:
            pos = body + 8 + (4 if flags & 0x01 else 0) + (4 if flags & 0x04 else 0)
            stride = 4 * bin(flags & 0xF00).count("1")
            ticks += sum(struct.unpack_from(">I", moof, pos + i * stride)[0] for i in range(count))
        else:
            ticks += count * default_duration
    return samples, ticks


def read_fragment(
    moof: bytes,
    entries: dict[int, list[Optional[UUID]]],
    defaults: dict[int, tuple[int, int]],
    moov_groups: dict[int, list[Seig]],
    clock: dict[int, int],
) -> Iterator[Fragment]:
    """Yield (track, decode time, duration, KIDs that encrypt samples) for each track fragment in a moof.

    A fragment without a tfdt or tfxd starts where the track's previous fragment ended, by
    the running sum of sample durations in ``clock``.
    """
    for kind, traf, traf_end in children(moof):
        if kind != b"traf":
            continue
        track, time, entry, default_duration = 0, None, 0, 0
        groups: list[Seig] = []
        runs: Optional[list[tuple[int, int]]] = None
        truns: list[int] = []
        for kind_, body, _ in children(moof, traf, traf_end):
            if kind_ == b"tfhd":
                flags = int.from_bytes(moof[body + 1 : body + 4], "big")
                track = struct.unpack_from(">I", moof, body + 4)[0]
                pos = body + 8 + (8 if flags & 0x01 else 0)
                if flags & 0x02:
                    entry = struct.unpack_from(">I", moof, pos)[0]
                    pos += 4
                if flags & 0x08:
                    default_duration = struct.unpack_from(">I", moof, pos)[0]
            elif kind_ == b"tfdt":
                time = struct.unpack_from(">Q" if moof[body] == 1 else ">I", moof, body + 4)[0]
            elif kind_ == b"uuid" + PIFF_TFXD and time is None:
                time = struct.unpack_from(">Q" if moof[body] == 1 else ">I", moof, body + 4)[0]
            elif kind_ == b"trun":
                truns.append(body)
            elif kind_ == b"sgpd":
                groups = seig_entries(moof, body) or groups
            elif kind_ == b"sbgp":
                runs = sbgp_runs(moof, body) or runs
        if track not in entries:
            continue
        default_entry, trex_duration = defaults.get(track, (1, 0))
        samples, duration = trun_totals(moof, truns, default_duration or trex_duration)
        if time is None:
            if not duration:
                continue
            time = clock.get(track, 0)
        clock[track] = time + duration
        track_entries = entries[track]
        entry = entry or default_entry
        base = track_entries[entry - 1] if 0 < entry <= len(track_entries) else None
        indexes = [0]
        if runs is not None:
            indexes = [index for count, index in runs if count]
            if samples > sum(count for count, _ in runs):
                indexes.append(0)  # samples past the last run take the sample entry's tenc
        kids: set[UUID] = set()
        for index in indexes:
            if index == 0:
                if base:
                    kids.add(base)
                continue
            local = index > 0x10000
            table = groups if local else moov_groups.get(track, [])
            position = (index - 0x10001) if local else index - 1
            if 0 <= position < len(table) and table[position][0]:
                kids.add(table[position][1])
        yield track, time, duration, kids
