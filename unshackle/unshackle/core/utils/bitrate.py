from __future__ import annotations

import json
import logging
import subprocess
import time
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Hashable, Optional, Union
from urllib.parse import urljoin

from requests import Session

from unshackle.core.binaries import FFProbe
from unshackle.core.session import RnetSession
from unshackle.core.utils.subprocess import log_tool_run

if TYPE_CHECKING:
    from unshackle.core.tracks import Track

# Default ISM timescale (ticks per second) per the Smooth Streaming spec.
ISM_DEFAULT_TIMESCALE = 10_000_000

# Bytes fetched to locate an mp4 moov box when probing duration via ffprobe.
MOOV_PROBE_BYTES = 4 * 1024 * 1024

# Network timeout (seconds) for probe requests.
PROBE_TIMEOUT = 15


@dataclass
class Segment:
    """One probe target: a media URL, optional byte range, its size, and duration."""

    url: str
    # The original byte-range string (e.g. "0-1023"), preserved as the segment's
    # identity so distinct ranges of one file are never confused with each other.
    byte_range: Optional[str]
    # Size in bytes when derivable without a request (from a byte range); else None.
    known_size: Optional[int]
    duration: float


def measure_real_bitrate(
    track: "Track",
    session: Union[Session, RnetSession],
    *,
    samples: int = 40,
    log: logging.Logger,
) -> Optional[int]:
    """
    Probe a track's actual media size to compute its real average bitrate.

    Manifests often declare an inaccurate bandwidth (DASH ``@bandwidth`` is a
    leaky-bucket ceiling, not an average). This measures the true bitrate
    (bits/sec) from real media byte sizes and durations using ``bytes * 8 / sec``.

    Single-file tracks are measured exactly. Segmented tracks probe up to
    ``samples`` segments spread across the track and extrapolate; byte-range
    segments need no request. Returns bits/sec, or ``None`` if it cannot be
    measured. Never raises — a probe failure must not abort a download.
    """
    from unshackle.core.tracks.track import Track

    try:
        if track.descriptor == Track.Descriptor.DASH:
            segments = extract_dash(track, session)
        elif track.descriptor == Track.Descriptor.HLS:
            segments = extract_hls(track, session)
        elif track.descriptor == Track.Descriptor.ISM:
            segments = extract_ism(track, session)
        else:
            # Descriptor.URL: a single file. Some services (e.g. AMZN) parse a DASH
            # manifest then collapse each representation to its single BaseURL and
            # flip the descriptor to URL, leaving the manifest (and its duration) in
            # track.data — recover the duration from there, else probe the file.
            segments = extract_url(track, session, log=log)
            if not segments:
                log.debug(f"{track.id}: cannot measure real bitrate (no known duration)")
                return None
    except Exception as e:
        log.warning(f"{track.id}: failed to derive segments for real bitrate ({e})")
        return None

    if not segments:
        return None

    items = dedupe(segments)
    chosen = pick_samples(items, samples)

    total_bytes = 0
    total_seconds = 0.0
    for segment in chosen:
        if segment.duration <= 0:
            continue
        size = segment.known_size if segment.known_size is not None else probe_size(segment, session)
        if not size:
            continue
        total_bytes += size
        total_seconds += segment.duration

    log.debug(
        f"{track.id}: real-bitrate probe desc={track.descriptor.name} "
        f"n_seg={len(segments)} n_unique={len(items)} n_chosen={len(chosen)} "
        f"sampled_bytes={total_bytes} sampled_seconds={round(total_seconds, 4)}"
    )

    if total_seconds <= 0 or total_bytes <= 0:
        log.warning(f"{track.id}: real bitrate probe returned no usable data")
        return None

    return round(total_bytes * 8 / total_seconds)


def apply_real_bitrates(
    tracks: list["Track"],
    session: Union[Session, RnetSession],
    *,
    log: logging.Logger,
    group_key: Callable[["Track"], Hashable],
    per_group: int = 5,
    workers: int = 8,
) -> None:
    """
    Probe real bitrates and overwrite ``track.bitrate`` for the tracks worth probing.

    Probing every rendition is slow when a service exposes dozens. Tracks are
    grouped by ``group_key`` (a quality tier), and only the ``per_group`` highest
    declared-bitrate tracks per group are probed, in parallel. Each group is then
    extended downward: while the lowest probed bitrate in a group sits below the
    next unprobed track's declared bitrate (so that track could outrank a probed
    one), the next track is probed too — until the probed set is safely above the
    rest. Unprobed tracks keep their manifest-declared bitrate.
    """
    groups: defaultdict[Hashable, list["Track"]] = defaultdict(list)
    for track in tracks:
        groups[group_key(track)].append(track)
    for group in groups.values():
        group.sort(key=lambda t: getattr(t, "bitrate", None) or 0, reverse=True)

    # Initial pass: top per_group of every group, all probed concurrently.
    initial = [track for group in groups.values() for track in group[:per_group]]
    probe_batch(initial, session, log=log, workers=workers)

    # Extend each group downward until unprobed tracks can't outrank probed ones.
    for group in groups.values():
        probed = min(per_group, len(group))
        while probed < len(group):
            lowest_probed = min((getattr(t, "bitrate", None) or 0) for t in group[:probed])
            next_declared = getattr(group[probed], "bitrate", None) or 0
            if next_declared <= lowest_probed:
                break
            probe_batch([group[probed]], session, log=log, workers=workers)
            probed += 1


def probe_batch(
    tracks: list["Track"],
    session: Union[Session, RnetSession],
    *,
    log: logging.Logger,
    workers: int,
) -> None:
    """Probe each track concurrently and overwrite its bitrate with the measured value."""
    if not tracks:
        return

    def probe_one(track: "Track") -> tuple["Track", Optional[int]]:
        return track, measure_real_bitrate(track, track.session or session, log=log)

    with ThreadPoolExecutor(max_workers=min(workers, len(tracks))) as executor:
        for track, measured in executor.map(probe_one, tracks):
            if not measured:
                continue
            declared = getattr(track, "bitrate", None)
            if declared and declared != measured:
                log.debug(f"{track.id}: bitrate {declared // 1000} → {measured // 1000} kb/s (real)")
            setattr(track, "bitrate", measured)


def dedupe(segments: list[Segment]) -> list[Segment]:
    """
    Collapse segments that address the same bytes so each object is measured once.

    Manifests sometimes wrap a single file in several segment entries sharing one
    URL — with no byte range (a ``SegmentTemplate`` whose media pattern has no
    ``$Number$``) or with the same range. Each resolves to the whole file, so
    counting them all would multiply the size by the segment count. Segments
    sharing the same ``(url, byte_range)`` are merged into one entry whose duration
    is the sum they cover. Distinct byte ranges of one file (different offsets) are
    kept individual so their sizes still add up to the full track.
    """
    merged: OrderedDict[tuple[str, Optional[str]], Segment] = OrderedDict()
    for segment in segments:
        key = (segment.url, segment.byte_range)
        existing = merged.get(key)
        if existing is None:
            merged[key] = Segment(segment.url, segment.byte_range, segment.known_size, segment.duration)
        else:
            existing.duration += segment.duration
    return list(merged.values())


def pick_samples(segments: list[Segment], samples: int) -> list[Segment]:
    """Pick up to ``samples`` segments spread evenly across the track."""
    count = len(segments)
    if count <= samples:
        return segments
    step = count / samples
    indices = sorted({int(i * step) for i in range(samples)})
    return [segments[i] for i in indices]


def probe_size(segment: Segment, session: Union[Session, RnetSession]) -> Optional[int]:
    """Return a segment's byte size via HEAD, falling back to a ranged GET. Validates status."""
    try:
        res = session.head(segment.url, allow_redirects=True, timeout=PROBE_TIMEOUT)
        if getattr(res, "status_code", 0) in (200, 206):
            content_length = res.headers.get("Content-Length")
            if content_length:
                return int(content_length)
    except Exception:
        pass

    # Some hosts block or mishandle HEAD; ask for a single byte and read the total.
    # Require a 206 so a server that ignores Range (returning the whole 200 body)
    # is not mistaken for a valid size or downloaded wholesale.
    try:
        res = session.get(segment.url, headers={"Range": "bytes=0-0"}, timeout=PROBE_TIMEOUT)
        if getattr(res, "status_code", 0) == 206:
            content_range = res.headers.get("Content-Range")
            if content_range and "/" in content_range:
                total = content_range.rsplit("/", 1)[-1].strip()
                if total.isdigit():
                    return int(total)
    except Exception:
        pass

    return None


def range_size(byte_range: Optional[str]) -> Optional[int]:
    """Size in bytes of a ``start-end`` media range, inclusive."""
    if not byte_range or "-" not in byte_range:
        return None
    start_s, _, end_s = byte_range.partition("-")
    try:
        start = int(start_s) if start_s else 0
        if not end_s:
            return None
        return int(end_s) - start + 1
    except ValueError:
        return None


def uniform_segments(
    raw_segments: list[tuple[str, Optional[str]]],
    total_duration: Optional[float],
) -> list[Segment]:
    """
    Build Segments giving each an equal share of the total duration.

    Used for DASH: ``DASH._get_period_segments`` returns timeline *start times*
    rather than per-segment durations, so they cannot be trusted. Segment lengths
    are near-uniform in practice, so the track duration (from
    ``mediaPresentationDuration``) split evenly is both correct and timeline-safe.
    """
    count = len(raw_segments)
    if not count or not total_duration or total_duration <= 0:
        return []
    per_segment = total_duration / count
    return [Segment(url, byte_range, range_size(byte_range), per_segment) for url, byte_range in raw_segments]


def extract_dash(track: "Track", session: Union[Session, RnetSession]) -> list[Segment]:
    from unshackle.core.manifests import DASH

    data = track.data["dash"]
    manifest = data["manifest"]
    rep_id = data.get("representation_id") or data["representation"].get("id")
    filtered_period_ids = data.get("filtered_period_ids", [])
    track_url = track.url if isinstance(track.url, str) else track.url[0]

    content_periods = [p for p in manifest.findall("Period") if DASH._is_content_period(p, filtered_period_ids)]

    raw_segments: list[tuple[str, Optional[str]]] = []
    for period in content_periods:
        matched_rep = matched_as = None
        for as_ in period.findall("AdaptationSet"):
            if DASH.is_trick_mode(as_):
                continue
            for rep in as_.findall("Representation"):
                if rep.get("id") == rep_id:
                    matched_rep, matched_as = rep, as_
                    break
            if matched_rep is not None:
                break
        if matched_rep is None or matched_as is None:
            continue

        _, period_segments, _, _, _ = DASH._get_period_segments(
            period=period,
            adaptation_set=matched_as,
            representation=matched_rep,
            manifest=manifest,
            track=track,
            track_url=track_url,
            session=session,
        )
        raw_segments.extend(period_segments)

    total_duration: Optional[float] = None
    mpd_duration = manifest.get("mediaPresentationDuration")
    if mpd_duration:
        total_duration = DASH.pt_to_sec(mpd_duration)

    return uniform_segments(raw_segments, total_duration)


def extract_hls(track: "Track", session: Union[Session, RnetSession]) -> list[Segment]:
    import m3u8

    playlist_url = track.url if isinstance(track.url, str) else track.url[0]
    res = session.get(playlist_url, timeout=PROBE_TIMEOUT)
    playlist = m3u8.loads(res.text, uri=playlist_url)

    out: list[Segment] = []
    for segment in playlist.segments:
        url = urljoin(segment.base_uri or "", segment.uri)
        byte_range = segment.byterange  # "<length>[@<offset>]"
        known_size: Optional[int] = None
        if byte_range:
            length = byte_range.split("@")[0].strip()
            if length.isdigit():
                known_size = int(length)
        # EXTINF durations are reliable, so they are used directly (unlike DASH).
        out.append(Segment(url, byte_range, known_size, float(segment.duration or 0)))
    return out


def extract_ism(track: "Track", session: Union[Session, RnetSession]) -> list[Segment]:
    data = track.data["ism"]
    segments: list[str] = data.get("segments") or []
    manifest = data["manifest"]

    timescale = int(manifest.get("TimeScale") or ISM_DEFAULT_TIMESCALE)
    duration_ticks = int(manifest.get("Duration") or 0)
    total_duration = (duration_ticks / timescale) if timescale else 0.0

    return uniform_segments([(url, None) for url in segments], total_duration)


def extract_url(track: "Track", session: Union[Session, RnetSession], *, log: logging.Logger) -> list[Segment]:
    """Single-file track: one whole-file URL with the duration from leftover manifest data."""
    url = track.url if isinstance(track.url, str) else (track.url[0] if track.url else None)
    if not url:
        return []

    duration: Optional[float] = None
    dash_data = track.data.get("dash")
    if dash_data and dash_data.get("manifest") is not None:
        from unshackle.core.manifests import DASH

        mpd_duration = dash_data["manifest"].get("mediaPresentationDuration")
        if mpd_duration:
            duration = DASH.pt_to_sec(mpd_duration)
    else:
        ism_data = track.data.get("ism")
        if ism_data and ism_data.get("manifest") is not None:
            manifest = ism_data["manifest"]
            timescale = int(manifest.get("TimeScale") or ISM_DEFAULT_TIMESCALE)
            duration_ticks = int(manifest.get("Duration") or 0)
            if timescale and duration_ticks:
                duration = duration_ticks / timescale

    if not duration or duration <= 0:
        # Services like AMZN clear the manifest data after collapsing to a single
        # file; fall back to reading the duration straight from the remote file.
        duration = ffprobe_duration(url, session, log=log)

    if not duration or duration <= 0:
        return []
    return [Segment(url, None, None, duration)]


def ffprobe_duration(url: str, session: Union[Session, RnetSession], *, log: logging.Logger) -> Optional[float]:
    """
    Read a single-file track's duration (seconds) without a manifest.

    The bundled ffprobe segfaults on network input, so the file's ``moov`` box is
    fetched over HTTP with the session (keeping the service's proxy/headers) and
    piped to ffprobe as local bytes. The head of the file is tried first (VOD is
    usually faststart), then the tail as a fallback for moov-at-end files.
    """
    head = ranged_get(url, session, f"bytes=0-{MOOV_PROBE_BYTES - 1}")
    duration = probe_bytes_duration(head, log)
    if duration:
        return duration

    size = probe_size(Segment(url, None, None, 0.0), session)
    if size and size > MOOV_PROBE_BYTES:
        tail = ranged_get(url, session, f"bytes={size - MOOV_PROBE_BYTES}-{size - 1}")
        duration = probe_bytes_duration(tail, log)
    return duration


def ranged_get(url: str, session: Union[Session, RnetSession], byte_range: str) -> Optional[bytes]:
    """Fetch a byte range, only accepting a real 206 partial response (never a full 200 body)."""
    try:
        res = session.get(url, headers={"Range": byte_range}, timeout=PROBE_TIMEOUT)
        if getattr(res, "status_code", 0) != 206:
            return None
        content = getattr(res, "content", None)
        return content if content else None
    except Exception:
        return None


def probe_bytes_duration(data: Optional[bytes], log: logging.Logger) -> Optional[float]:
    """Pipe media bytes to ffprobe and return the format/stream duration in seconds."""
    if not data:
        return None
    ffprobe_bin = str(FFProbe) if FFProbe else "ffprobe"
    try:
        probe_start = time.monotonic()
        result = subprocess.run(
            [ffprobe_bin, "-v", "error", "-show_entries", "format=duration:stream=duration", "-of", "json", "pipe:"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        log_tool_run(
            "ffprobe duration probe",
            "ffprobe",
            result.returncode,
            duration_ms=round((time.monotonic() - probe_start) * 1000, 1),
        )
        info = json.loads(result.stdout or b"{}")
        candidates = [info.get("format", {}).get("duration")]
        candidates += [s.get("duration") for s in info.get("streams", [])]
        for value in candidates:
            if value:
                return float(value)
        log.debug(f"ffprobe found no duration (rc={result.returncode}): {result.stderr.decode(errors='replace')[:160]}")
        return None
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError) as e:
        log.debug(f"ffprobe duration error: {e}")
        return None
