from __future__ import annotations

import logging
import struct
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Callable, Optional, Union
from urllib.parse import urljoin

from requests import Session

from unshackle.core.manifests.ism_init import find_box, iter_boxes, remove_emulation_prevention
from unshackle.core.session import RnetSession, utf8_text
from unshackle.core.utils.bitrate import PROBE_TIMEOUT, ranged_get

if TYPE_CHECKING:
    from unshackle.core.service import Service
    from unshackle.core.titles import Title_T
    from unshackle.core.tracks import Tracks, Video

PROBE_BYTES = 64 * 1024
HDR10PLUS_T35 = b"\xb5\x00\x3c\x00\x01\x04"


def sei_number(rbsp: bytes, i: int) -> tuple[Optional[int], int]:
    """Read the SEI payload type or size that starts at ``i``: 0xFF bytes add 255 each. Returns (value, next)."""
    value = 0
    while i < len(rbsp) and rbsp[i] == 0xFF:
        value += 255
        i += 1
    if i >= len(rbsp):
        return None, i
    return value + rbsp[i], i + 1


def sei_has_hdr10plus(nal: bytes) -> bool:
    """Whether an HEVC SEI NAL holds an ST 2094-40 message."""
    rbsp = remove_emulation_prevention(nal[2:])
    i = 0
    while i < len(rbsp) and rbsp[i] != 0x80:
        payload_type, i = sei_number(rbsp, i)
        payload_size, i = sei_number(rbsp, i)
        if payload_type is None or payload_size is None:
            return False
        if payload_type == 4 and rbsp[i : i + len(HDR10PLUS_T35)] == HDR10PLUS_T35:
            return True
        i += payload_size
    return False


def fragment_has_hdr10plus(data: bytes) -> Optional[bool]:
    """Whether the first sample of an fMP4 fragment carries HDR10+ SEI, or None when the bytes cannot tell.

    The walk ends at the first VCL NAL, because a prefix SEI comes before it. CENC and cbcs keep
    non-VCL NALs and every NAL length in the clear, so this needs no content key. A sample that is
    encrypted whole can read as False as well as None; both leave the label alone.
    """
    # 4-byte NAL lengths: reading lengthSizeMinusOne from hvcC would cost an init-segment fetch
    mdat = find_box(data, 0, len(data), b"mdat")
    if not mdat:
        return None
    pos = mdat[0]
    while pos + 6 <= len(data):
        size = struct.unpack(">I", data[pos : pos + 4])[0]
        nal_type = (data[pos + 4] >> 1) & 0x3F
        if size < 2 or nal_type >= 48:
            return None  # not a NAL: the sample is fully encrypted or the walk lost sync
        if nal_type < 32:
            return False
        if nal_type == 39:
            if pos + 4 + size > len(data):
                return None
            if sei_has_hdr10plus(data[pos + 4 : pos + 4 + size]):
                return True
        pos += 4 + size
    return None


def first_segment(track: "Video", session: Union[Session, RnetSession]) -> Optional[tuple[str, int]]:
    """The URL and start offset of the track's first media segment, looked up without a full parse."""
    from unshackle.core.manifests import DASH
    from unshackle.core.tracks.track import Track

    url = track.url if isinstance(track.url, str) else track.url[0]
    dash = track.data.get("dash") or {}
    # a service can collapse a DASH representation to a one-file URL track and keep the manifest
    if dash.get("manifest") is not None and dash.get("period") is not None:
        _, segments, *_ = DASH.get_period_segments(
            period=dash["period"],
            adaptation_set=dash["adaptation_set"],
            representation=dash["representation"],
            manifest=dash["manifest"],
            track=track,
            track_url=url,
            session=session,
            probe_kid=False,
        )
        if not segments:
            return None
        seg_url, byte_range = segments[0]
        return seg_url, int((byte_range or "").partition("-")[0] or 0)
    if track.descriptor == Track.Descriptor.HLS:
        res = session.get(url, timeout=PROBE_TIMEOUT)
        if getattr(res, "status_code", 0) != 200:
            return None
        offset = 0
        for line in utf8_text(res).splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-BYTERANGE:") and "@" in line:
                offset = int(line.rpartition("@")[2])
            elif line and not line.startswith("#"):
                return urljoin(url, line), offset
        return None
    if track.descriptor == Track.Descriptor.URL:
        return url, 0
    return None


def first_fragment(track: "Video", session: Union[Session, RnetSession]) -> Optional[bytes]:
    """The first PROBE_BYTES of the track's first media fragment."""
    segment = first_segment(track, session)
    if not segment:
        return None
    url, start = segment
    # A one-file track opens with ftyp, moov and sidx, which can outgrow PROBE_BYTES. Fetch again past
    # them only when the fragment does not start inside this window.
    for _ in range(4):
        data = ranged_get(url, session, f"bytes={start}-{start + PROBE_BYTES - 1}")
        if not data:
            return None
        skip = 0
        for kind, _, _, end in iter_boxes(data, 0, len(data)):
            if kind in (b"styp", b"moof", b"mdat"):
                return data
            skip = end
        if not skip:
            return data
        start += skip
    return None


def probe_hdr10plus(
    videos: list["Video"],
    session: Union[Session, RnetSession],
    *,
    log: logging.Logger,
    workers: int,
    on_start: Optional[Callable[[], None]] = None,
) -> None:
    """Relabel HEVC HDR10 tracks whose first sample carries HDR10+ SEI as HDR10P.

    A ladder with one track per resolution is sampled at both ends first. Two tracks at one resolution
    can be an HDR10 and an HDR10+ encode side by side, so such a ladder is probed whole. The rules are
    in the ``hdr10plus_probe`` section of docs/reference/configuration/download.md. A failed probe never
    changes a label. ``workers`` caps the probes in flight, and ``on_start`` runs once before the first.
    """
    from unshackle.core.tracks import Video

    candidates = sorted(
        (v for v in videos if v.range == Video.Range.HDR10 and v.codec == Video.Codec.HEVC),
        key=lambda v: v.bitrate or 0,
        reverse=True,
    )
    if candidates and on_start:
        on_start()
    if len(candidates) <= 4 or len({(v.width, v.height) for v in candidates}) < len(candidates):
        samples, rest = candidates, []
    else:
        samples, rest = candidates[:2] + candidates[-2:], candidates[2:-2]

    def probe(video: "Video") -> Optional[bool]:
        try:
            data = first_fragment(video, video.session or session)
            found = fragment_has_hdr10plus(data) if data else None
        except Exception as e:
            log.debug(f"{video.id}: HDR10+ probe failed ({e})")
            return None
        log.debug(f"{video.id}: HDR10+ probe {found}")
        return found

    def probe_all(tracks: list["Video"]) -> list[Optional[bool]]:
        if not tracks:
            return []
        with ThreadPoolExecutor(max_workers=min(workers, len(tracks))) as executor:
            return list(executor.map(probe, tracks))

    results = probe_all(samples)
    if any(results) and not all(results):
        samples, results = samples + rest, results + probe_all(rest)
    hits = candidates if all(results) else [v for v, found in zip(samples, results) if found]
    for video in hits:
        video.range = Video.Range.HDR10P


def probed_tracks(
    service: "Service",
    title: "Title_T",
    *,
    workers: Optional[int] = None,
    on_probe: Optional[Callable[[], None]] = None,
) -> "Tracks":
    """The title's tracks from ``service.get_tracks``, with HDR10+ found in tracks labelled HDR10.

    ``workers`` defaults to ``dl.workers`` from the config. A remote service's tracks come back from a
    server that already probed them.
    """
    from unshackle.core.config import config
    from unshackle.core.downloaders import default_max_workers
    from unshackle.core.remote_service import RemoteService

    tracks = service.get_tracks(title)
    if config.hdr10plus_probe and tracks.videos and not isinstance(service, RemoteService):
        # int() as click does for --workers, so a quoted dl.workers works on the REST path too
        workers = workers or int(config.dl.get("workers") or 0) or default_max_workers()
        if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
            raise ValueError(f"workers must be a whole number of 1 or more, not {workers!r}")
        probe_hdr10plus(
            tracks.videos,
            service.session,
            log=logging.getLogger("hdr10plus"),
            workers=workers,
            on_start=on_probe,
        )
    return tracks
