from __future__ import annotations

import logging
import struct
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Callable, Optional, TypeVar, Union
from urllib.parse import urljoin

from requests import Session

from unshackle.core.manifests.ism_init import find_box, iter_boxes, remove_emulation_prevention
from unshackle.core.session import RnetSession, utf8_text
from unshackle.core.utils.bitrate import PROBE_TIMEOUT, ranged_get

if TYPE_CHECKING:
    from unshackle.core.service import Service
    from unshackle.core.titles import Title_T
    from unshackle.core.tracks import Audio, Track, Tracks, Video

PROBE_BYTES = 64 * 1024
HDR10PLUS_T35 = b"\xb5\x00\x3c\x00\x01\x04"
CUVA_T35 = b"\x26\x00\x04\x00\x05"
TS_PACKET = 188
T = TypeVar("T")
R = TypeVar("R")


def map_probes(probe: Callable[[T], R], items: list[T], workers: int) -> list[R]:
    """``probe`` over ``items`` in order, with no more than ``workers`` in flight."""
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as executor:
        return list(executor.map(probe, items))


def is_ts(data: bytes) -> bool:
    """Whether the bytes start as MPEG-TS packets."""
    return data[:1] == b"\x47" and (len(data) <= TS_PACKET or data[TS_PACKET : TS_PACKET + 1] == b"\x47")


def ts_streams(data: bytes) -> Optional[list[tuple[int, bytes]]]:
    """The ``stream_type`` and registration format identifier of each PMT entry, or None without a PMT.

    PAT program 0 names the network information table, so it is skipped. Lengths that run past the packet
    are cut to it, so bad bytes give fewer entries, never an exception.
    """
    # reads only a PAT and PMT that fit in one packet, as packagers write them; reassemble
    # sections across packets if a source with a longer PMT appears

    def pid(b: bytes, i: int) -> int:
        return ((b[i] & 0x1F) << 8) | b[i + 1]

    def length(b: bytes, i: int) -> int:
        return ((b[i] & 0x0F) << 8) | b[i + 1]

    def section(packet: bytes, table_id: int) -> bytes:
        """The packet's ``table_id`` section up to its CRC, or b"" when the packet holds no such section."""
        adaptation = (packet[3] >> 4) & 3
        payload = packet[5 + packet[4] if adaptation & 2 else 4 :] if adaptation & 1 else b""
        body = payload[1 + payload[0] :] if payload else b""
        body = body[: max(0, 3 + length(body, 1) - 4)] if len(body) >= 3 else b""
        return body if len(body) >= 12 and body[0] == table_id else b""

    pmt_pid = None
    for pos in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        packet = data[pos : pos + TS_PACKET]
        if packet[0] != 0x47 or not packet[1] & 0x40:
            continue
        if pid(packet, 1) == 0 and pmt_pid is None:
            pat = section(packet, 0x00)
            pmt_pid = next((pid(pat, k + 2) for k in range(8, len(pat) - 3, 4) if pat[k] or pat[k + 1]), None)
        elif pid(packet, 1) == pmt_pid:
            pmt = section(packet, 0x02)
            if not pmt:
                return None
            i, streams = 12 + length(pmt, 10), []
            while i + 5 <= len(pmt):
                info_end = min(i + 5 + length(pmt, i + 3), len(pmt))
                registration, j = b"", i + 5
                while j + 2 <= info_end:
                    if pmt[j] == 0x05 and j + 6 <= info_end:
                        registration = pmt[j + 2 : j + 6]
                    j += 2 + pmt[j + 1]
                streams.append((pmt[i], registration))
                i = info_end
            return streams
    return None


def sei_number(rbsp: bytes, i: int) -> tuple[Optional[int], int]:
    """Read the SEI payload type or size that starts at ``i``: 0xFF bytes add 255 each. Returns (value, next)."""
    value = 0
    while i < len(rbsp) and rbsp[i] == 0xFF:
        value += 255
        i += 1
    if i >= len(rbsp):
        return None, i
    return value + rbsp[i], i + 1


def sei_has_t35(nal: bytes, t35: bytes = HDR10PLUS_T35) -> bool:
    """Whether an HEVC SEI NAL holds an ST 2094-40 message, or the T.35 message that ``t35`` starts."""
    rbsp = remove_emulation_prevention(nal[2:])
    i = 0
    while i < len(rbsp) and rbsp[i] != 0x80:
        payload_type, i = sei_number(rbsp, i)
        payload_size, i = sei_number(rbsp, i)
        if payload_type is None or payload_size is None:
            return False
        if payload_type == 4 and rbsp[i : i + len(t35)] == t35:
            return True
        i += payload_size
    return False


def fragment_has_hdr10plus(data: bytes, t35: bytes = HDR10PLUS_T35) -> Optional[bool]:
    """Whether the first sample of an fMP4 fragment carries HDR10+ SEI, or None when the bytes cannot tell.

    ``t35`` picks another T.35 message, such as ``CUVA_T35`` for HDR Vivid.

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
            if sei_has_t35(data[pos + 4 : pos + 4 + size], t35):
                return True
        pos += 4 + size
    return None


def first_segment(track: "Track", session: Union[Session, RnetSession]) -> Optional[tuple[str, int]]:
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


def first_fragment(track: "Track", session: Union[Session, RnetSession]) -> Optional[bytes]:
    """The first PROBE_BYTES of the track's first media fragment, or of its first segment when that is MPEG-TS."""
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
        if is_ts(data):
            return data
        skip = 0
        for kind, _, _, end in iter_boxes(data, 0, len(data)):
            if kind in (b"styp", b"moof", b"mdat"):
                return data
            skip = end
        if not skip:
            return data
        start += skip
    return None


def probe_dynamic_hdr(
    videos: list["Video"],
    session: Union[Session, RnetSession],
    *,
    log: logging.Logger,
    workers: int,
    on_start: Optional[Callable[[], None]] = None,
) -> None:
    """Relabel HEVC tracks by the dynamic HDR metadata of their first sample, and flag DV tracks with HDR10+.

    HDR10+ SEI makes an HDR10 track HDR10P. CUVA SEI, or the ``cuvv`` registration of an MPEG-TS stream,
    makes an HDR10 or HLG track VIVID; ``PROBED_RANGES`` holds which label each range takes. A DV track
    keeps its DV range and gets ``hdr10plus`` set, so selection and hybrid pairing are unchanged. HDR10,
    HLG and DV are separate ladders. A ladder with one track per resolution is sampled at both ends first.
    Two tracks at one resolution can be an HDR10 and an HDR10+ encode side by side, so such a ladder is
    probed whole. The rules are in the ``hdr10plus_probe`` section of docs/reference/configuration/download.md.
    A failed probe never changes a label. ``workers`` caps the probes in flight, and ``on_start`` runs once
    before the first.
    """
    from unshackle.core.tracks import Video
    from unshackle.core.tracks.video import PROBED_RANGES

    ladders = [
        sorted(
            (v for v in videos if v.range == range_ and v.codec == Video.Codec.HEVC),
            key=lambda v: v.bitrate or 0,
            reverse=True,
        )
        for range_ in (*PROBED_RANGES, Video.Range.DV)
    ]
    if any(ladders) and on_start:
        on_start()

    def probe(video: "Video") -> Union["Video.Range", bool, None]:
        """The range the first segment proves (HDR10P or VIVID), False for plain HDR10, None when it cannot tell."""
        found: Union[Video.Range, bool, None] = None
        try:
            data = first_fragment(video, video.session or session)
            if data and is_ts(data):
                if any(registration == b"cuvv" for _, registration in ts_streams(data) or []):
                    found = Video.Range.VIVID
            elif data and fragment_has_hdr10plus(data, CUVA_T35):
                found = Video.Range.VIVID
            elif data:
                hdr10plus = fragment_has_hdr10plus(data)
                found = Video.Range.HDR10P if hdr10plus else hdr10plus
        except Exception as e:
            log.debug(f"{video.id}: dynamic HDR probe failed ({e})")
            return None
        log.debug(f"{video.id}: dynamic HDR probe {found}")
        return found

    def probe_all(tracks: list["Video"]) -> dict[int, Union["Video.Range", bool, None]]:
        return dict(zip(map(id, tracks), map_probes(probe, tracks, workers)))

    plans: list[tuple[list["Video"], list["Video"], list["Video"]]] = []
    for candidates in ladders:
        if len(candidates) <= 4 or len({(v.width, v.height) for v in candidates}) < len(candidates):
            plans.append((candidates, candidates, []))
        else:
            plans.append((candidates, candidates[:2] + candidates[-2:], candidates[2:-2]))

    # one round for the samples of every ladder, then one for the rest of each mixed ladder
    found = probe_all([v for _, samples, _ in plans for v in samples])
    labels = [{found[id(s)] for s in samples} for _, samples, _ in plans]
    found.update(
        probe_all([v for (_, _, rest), seen in zip(plans, labels) if any(seen) and len(seen) > 1 for v in rest])
    )
    for (candidates, _, _), seen in zip(plans, labels):
        whole = next(iter(seen)) if len(seen) == 1 else None
        for video in candidates:
            label = found.get(id(video), whole)
            if video.range == Video.Range.DV:
                video.hdr10plus = video.hdr10plus or label == Video.Range.HDR10P
            elif isinstance(label, Video.Range) and label in PROBED_RANGES.get(video.range, ()):
                video.range = label


def probe_audio_codecs(
    audio: list["Audio"], session: Union[Session, RnetSession], *, log: logging.Logger, workers: int
) -> None:
    """Name the codec of each audio track that has none from the PMT of its MPEG-TS first segment."""
    from unshackle.core.tracks import Audio

    def probe(track: "Audio") -> None:
        try:
            data = first_fragment(track, track.session or session)
            streams = (ts_streams(data) if data and is_ts(data) else None) or []
        except Exception as e:
            log.debug(f"{track.id}: audio codec probe failed ({e})")
            return
        codec = next(filter(None, (Audio.Codec.from_ts(*stream) for stream in streams)), None)
        log.debug(f"{track.id}: audio codec probe {codec}")
        if codec:
            track.codec = codec

    map_probes(probe, [a for a in audio if not a.codec], workers)


def probed_tracks(
    service: "Service",
    title: "Title_T",
    *,
    workers: Optional[int] = None,
    on_probe: Optional[Callable[[], None]] = None,
) -> "Tracks":
    """The title's tracks from ``service.get_tracks``, with HDR10+ and HDR Vivid found in HDR10 and HLG tracks.

    Audio with no codec gets one from the MPEG-TS ``stream_type``. ``workers`` defaults to ``dl.workers`` from
    the config. A remote service's tracks come back from a server that already probed them.
    """
    from unshackle.core.config import config
    from unshackle.core.downloaders import default_max_workers
    from unshackle.core.remote_service import RemoteService

    tracks = service.get_tracks(title)
    codecless_audio = any(not a.codec for a in tracks.audio)
    if config.hdr10plus_probe and (tracks.videos or codecless_audio) and not isinstance(service, RemoteService):
        # int() as click does for --workers, so a quoted dl.workers works on the REST path too
        workers = workers or int(config.dl.get("workers") or 0) or default_max_workers()
        if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
            raise ValueError(f"workers must be a whole number of 1 or more, not {workers!r}")
        log = logging.getLogger("hdr10plus")
        probe_dynamic_hdr(tracks.videos, service.session, log=log, workers=workers, on_start=on_probe)
        probe_audio_codecs(tracks.audio, service.session, log=log, workers=workers)
    return tracks
