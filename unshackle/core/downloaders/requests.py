import errno
import math
import multiprocessing
import os
import pickle
import random
import re
import socket
import statistics
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, wait
from concurrent.futures.thread import ThreadPoolExecutor
from copy import copy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar
from pathlib import Path
from queue import Empty, Queue
from string import Formatter
from typing import Any, Callable, Generator, MutableMapping, Optional, Union, cast

from requests import Session
from requests.adapters import HTTPAdapter, Retry
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ProxyError, SSLError
from rich import filesize

from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED, DownloadCancelled
from unshackle.core.utilities import get_debug_logger, get_extension, log_event
from unshackle.core.utils import run_proxy

MAX_ATTEMPTS = 5
RETRY_WAIT = 2
GAVE_UP_NOTE = "gave up after"
OUTAGE_ATTEMPTS = 9
LOCAL_DISK_ERRNOS = (errno.ENOSPC, errno.EDQUOT, errno.EROFS)
MAX_RESUMES = 50
MIN_RESUME_PROGRESS = 1024 * 1024

REQUEST_CHUNK_SIZE = 4 * 1024 * 1024
MIN_REQUEST_CHUNK = 256 * 1024
PROGRESS_WINDOW = 2

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30

PROBE_DRAIN_LIMIT = 4096

HEDGE_FACTOR = 3
HEDGE_MIN_WAIT = 5.0

MIN_CHUNK = 524_288
MAX_CHUNK = 4_194_304
DEFAULT_CHUNK = 524_288
SPEED_ROLLING_WINDOW = 10

RANGE_PARALLEL_MIN_SIZE = 64 * 1024 * 1024
RANGE_PARALLEL_MIN_PART_SIZE = 4 * 1024 * 1024
RANGE_PARALLEL_PART_SIZE = 16 * 1024 * 1024

MP_MIN_SEGMENTS = 24

TAIL_BOOST_MAX_PER_CYCLE = 4
TAIL_BOOST_MIN_SEGMENT_SIZE = 8 * 1024 * 1024
TAIL_BOOST_PART_SIZE = 4 * 1024 * 1024


class SpeedWindow:
    """Rolling-window download rate over the last SPEED_ROLLING_WINDOW seconds."""

    def __init__(self, start: float) -> None:
        self.samples: deque[tuple[float, int]] = deque([(start, 0)])

    def rate(self, now: float, total: int) -> Optional[float]:
        self.samples.append((now, total))
        while now - self.samples[0][0] > SPEED_ROLLING_WINDOW:
            self.samples.popleft()
        span = now - self.samples[0][0]
        delta = total - self.samples[0][1]
        if span <= 0 or delta <= 0:
            return None
        return delta / span


class TokenBucket:
    """Aggregate byte-rate limiter shared by every download thread."""

    def __init__(self, rate: float) -> None:
        self.rate = rate
        self.capacity = rate
        self.tokens = 0.0
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, n: int) -> None:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
            self.ts = now
            self.tokens -= n
            wait = -self.tokens / self.rate if self.tokens < 0 else 0.0
        if wait > 0:
            DOWNLOAD_CANCELLED.wait(wait)


speed_limiter: Optional[TokenBucket] = None
speed_limit_locked = False

SPEED_UNITS = {
    "": 1,
    "b": 1,
    "k": 1_000,
    "kb": 1_000,
    "m": 1_000_000,
    "mb": 1_000_000,
    "g": 1_000_000_000,
    "gb": 1_000_000_000,
}


def parse_speed_limit(value: Union[str, int, float, None]) -> Optional[float]:
    """
    Parse a human-readable speed limit into bytes/sec, or None for unlimited.

    Accepts plain numbers (exact bytes/sec) or a decimal suffix with optional
    /s, case-insensitive: "500k", "5M", "10MB/s", "1.5G", 5000000. Suffixes
    match the displayed speeds (5M = 5.0 MB/s). Values are bytes, not bits.
    "", 0, "off", "none" and "unlimited" mean no limit.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid speed limit {value!r}: must be a positive number of bytes/sec.")
        return float(value) or None
    text = value.strip().lower().removesuffix("/s")
    if text in ("", "0", "off", "none", "unlimited"):
        return None
    match = re.fullmatch(r"(\d*\.?\d+)\s*([kmg]b?|b)?", text)
    if not match:
        raise ValueError(f"Invalid speed limit {value!r}: expected 500k, 5M, 1.5G, bytes per second or 'off'")
    return float(match.group(1)) * SPEED_UNITS[match.group(2) or ""]


def format_speed(bytes_per_sec: float) -> str:
    """Format a byte rate in the same decimal units the progress bars show."""
    return f"{filesize.decimal(int(bytes_per_sec))}/s"


def set_speed_limit(bytes_per_sec: Optional[float], lock: bool = False) -> None:
    """
    Set (or clear) the global download speed limit shared by all threads.

    A locked limit (serve's global_speed_limit) wins: later unlocked calls,
    like the per-job one in dl.result(), become no-ops for the process.
    """
    global speed_limiter, speed_limit_locked
    if speed_limit_locked and not lock:
        return
    speed_limit_locked = lock
    speed_limiter = TokenBucket(bytes_per_sec) if bytes_per_sec else None


def adaptive_chunk_size(content_length: int) -> int:
    """Pick the read chunk size from ``content_length``. Benchmarked sweet spot: 512KB-4MB."""
    if content_length <= 0:
        return DEFAULT_CHUNK
    return min(MAX_CHUNK, max(MIN_CHUNK, content_length // 4))


def rnet_read_timeout() -> float:
    """The stall timeout for a segment request on rnet: ``network.read_timeout`` when configured, else READ_TIMEOUT.

    A per-request value overrides the rnet client's own, so this must give the configured one.
    """
    return config.network.get("read_timeout") or READ_TIMEOUT


ADAPTIVE_TICK = 4.0
ADAPTIVE_STEP = 2
ADAPTIVE_MIN = 2
ADAPTIVE_ERROR_BURST = 3
ADAPTIVE_PLATEAU_GAIN = 1.10


class AdaptiveWorkerController:
    """CDN-aware segment concurrency governor.

    Pure and synchronous (no threads or sockets), so the policy is unit-testable.
    ``update`` evaluates the policy once per ``tick``. It starts as soon as half a tick
    of throughput samples exists. The target starts at ``start`` (the cap by default) and
    follows AIMD from there: an error burst halves it and takes a one-tick cooldown, the
    controller reverts a probe upward that plateaus, and otherwise it climbs by
    ``ADAPTIVE_STEP``. It judges a probe on the trailing tick before and after the change, so
    both sides of the ``ADAPTIVE_PLATEAU_GAIN`` comparison measure the same span. Target is
    always clamped to ``[ADAPTIVE_MIN, cap]``.
    """

    def __init__(
        self, cap: int, start: Optional[int] = None, tick: float = ADAPTIVE_TICK, window: float = SPEED_ROLLING_WINDOW
    ) -> None:
        self.cap = max(ADAPTIVE_MIN, cap)
        base = self.cap if start is None else start
        self.target = max(ADAPTIVE_MIN, min(base, self.cap))
        self.tick = tick
        self.window = window
        self._samples: deque[tuple[float, int]] = deque()
        self._first_sample: Optional[float] = None
        self._errors = 0
        self._last_tick: Optional[float] = None
        self._last_action: Optional[str] = None
        self._speed_before_increase = 0.0
        self._target_before_increase = self.target
        self._cooldown = False

    def prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def record_bytes(self, n: int, now: float) -> None:
        if self._first_sample is None:
            self._first_sample = now
        self._samples.append((now, n))
        self.prune(now)

    def record_error(self, now: float) -> None:
        self._errors += 1

    def speed(self, now: float, span: Optional[float] = None) -> float:
        """Rolling bytes/sec over the window, or over the trailing ``span`` seconds when given."""
        self.prune(now)
        if not self._samples:
            return 0.0
        if span is not None and span > 0:
            cutoff = now - span
            return sum(n for t, n in self._samples if t > cutoff) / span
        elapsed = now - self._samples[0][0]
        if elapsed <= 0:
            return 0.0
        return sum(n for _, n in self._samples) / elapsed

    def warmed_up(self, now: float) -> bool:
        return self._first_sample is not None and (now - self._first_sample) >= self.tick / 2

    def update(self, now: float, inflight_plus_remaining: Optional[int] = None) -> int:
        """Evaluate the policy at most once per tick. Return the current target.

        ``inflight_plus_remaining`` (when given) is the count of segments still in flight or
        not yet started. If it is below the target there is not enough work to saturate the
        target, so the low measured speed reflects starvation rather than a plateau, and
        there is no probe/revert step this tick. Error-burst decreases still apply. Default ``None`` keeps the
        original behaviour for existing callers.
        """
        if self._last_tick is None:
            self._last_tick = now
            return self.target
        if now - self._last_tick < self.tick:
            return self.target
        self._last_tick = now

        if not self.warmed_up(now):
            self._errors = 0
            return self.target

        errors = self._errors
        self._errors = 0
        speed_now = self.speed(now)
        probe_speed = self.speed(now, min(self.tick, self.window))
        old = self.target

        if self._cooldown:
            self._cooldown = False
            self._last_action = None
            return self.target

        reason: Optional[str] = None
        if errors >= ADAPTIVE_ERROR_BURST:
            self.target = max(ADAPTIVE_MIN, self.target // 2)
            self._cooldown = True
            self._last_action = None
            reason = "error_burst"
        elif inflight_plus_remaining is not None and inflight_plus_remaining < self.target:
            pass
        elif self._last_action == "increase" and probe_speed < ADAPTIVE_PLATEAU_GAIN * self._speed_before_increase:
            self.target = max(ADAPTIVE_MIN, self._target_before_increase)
            self._last_action = None
            reason = "plateau"
        elif self.target < self.cap:
            self._speed_before_increase = probe_speed
            self._target_before_increase = self.target
            self.target = min(self.cap, self.target + ADAPTIVE_STEP)
            self._last_action = "increase"
            reason = "increase"
        else:
            self._last_action = None

        self.target = max(ADAPTIVE_MIN, min(self.cap, self.target))
        if self.target != old:
            debug_logger = get_debug_logger()
            if debug_logger:
                debug_logger.log(
                    level="DEBUG",
                    operation="adaptive_workers",
                    message=f"worker target {old} -> {self.target}",
                    context={"old": old, "new": self.target, "speed_bps": round(speed_now), "reason": reason},
                )
        return self.target


def permanent_status(exc: BaseException) -> Optional[int]:
    """HTTP status of ``exc`` when it is a client error that no retry can fix.

    408 and 429 do not count: they are transient, and a fan-out that trips a per-client rate
    limit can still succeed over one sequential connection.
    """
    from unshackle.core.session import failure_response

    status = getattr(failure_response(exc), "status_code", None)
    if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429):
        return status
    return None


class TailPartFailed(Exception):
    """A tail-boost range part failed. ``index`` names the segment it was split from.

    The batch loop needs the index to put that segment back on the normal single-worker
    path; the failure itself stays reachable as ``__cause__`` for ``permanent_status``.
    """

    def __init__(self, index: int) -> None:
        super().__init__(f"tail-boost part failed for segment {index}")
        self.index = index


def retry_sleep(exc: Exception, attempts: int) -> float:
    """Seconds to wait before retry number ``attempts``.

    download() owns segment retries (the HTTP session itself does not retry), so backoff
    lives here: honor the server's Retry-After when the failure carries a response,
    else exponential growth from RETRY_WAIT with jitter so concurrently rate-limited
    segments do not retry in lockstep. Capped at ``session.MAX_BACKOFF`` either way.
    """
    from unshackle.core.session import MAX_BACKOFF, failure_response

    response = failure_response(exc)
    if response is not None:
        try:
            retry_after = response.headers.get("Retry-After")
        except Exception:
            retry_after = None
        if retry_after:
            wait: Optional[float] = None
            try:
                wait = float(retry_after)
            except ValueError:
                try:
                    retry_date = parsedate_to_datetime(retry_after)
                    if retry_date.tzinfo is None:
                        retry_date = retry_date.replace(tzinfo=timezone.utc)
                    wait = (retry_date - datetime.now(timezone.utc)).total_seconds()
                except Exception:
                    wait = None
            if wait is not None and math.isfinite(wait):
                return min(max(wait, 0.0), MAX_BACKOFF)
    backoff = RETRY_WAIT * (2 ** (attempts - 1))
    return min(backoff + random.uniform(0, backoff * 0.1), MAX_BACKOFF)


def retries_exhausted(exc: BaseException) -> bool:
    """Whether ``exc`` is the error download() re-raised after it spent every attempt."""
    return any(str(note).startswith(GAVE_UP_NOTE) for note in getattr(exc, "__notes__", ()))


def is_requests_session(session: Any) -> bool:
    """Whether the HTTP session is a standard requests.Session (it has ``resp.raw``)."""
    return isinstance(session, Session)


def no_retry_session(session: Session) -> Session:
    """A shallow copy of ``session`` whose adapters never retry, sharing its state and pools.

    download() owns segment retries, so a mounted urllib3 ``Retry`` nests inside a single
    attempt: one 429 carrying ``Retry-After: 60`` costs five one-minute sleeps before
    download() even sees the failure. requests takes no per-call retry override, and track
    threads share the caller's session, so remounting it is not an option.
    """
    view = copy(session)
    view.adapters = OrderedDict()
    for prefix, adapter in session.adapters.items():
        twin = copy(adapter)
        twin.max_retries = Retry(0, read=False)  # type: ignore[attr-defined]
        if isinstance(adapter, HTTPAdapter) and isinstance(twin, HTTPAdapter):
            twin.poolmanager = adapter.poolmanager
            twin.proxy_manager = adapter.proxy_manager
        view.adapters[prefix] = twin
    return view


def is_rnet_session(session: Any) -> bool:
    """Whether the HTTP session is an RnetSession (it uses ``resp.stream()``)."""
    from unshackle.core.session import RnetSession

    return isinstance(session, RnetSession)


def is_outage(exc: BaseException) -> bool:
    """Whether a failure reached no server: a refused or unreachable connection, or a failed name lookup.

    A TLS, proxy or URL error is not an outage, so it keeps the MAX_ATTEMPTS budget.
    """
    import rnet

    for error in (exc, exc.__cause__):
        if isinstance(error, (rnet.ConnectionError, rnet.ConnectionResetError, rnet.DNSResolverError)):
            return True
        if isinstance(error, RequestsConnectionError) and not isinstance(error, (ProxyError, SSLError)):
            return True
    return False


def is_content_encoded(value: Optional[str]) -> bool:
    """
    Whether a Content-Encoding value names any coding other than absent or `identity`.

    Reading .raw skips urllib3's decoding, so an encoded body has to go through
    iter_content or the compressed bytes land on disk as the payload. Codings
    urllib3 cannot decode still count: an allowlist fails open on whatever token
    a server sends next, and the cost of guessing wrong is a corrupt file.
    """
    return any(coding.strip() not in ("", "identity") for coding in (value or "").lower().split(","))


def probe_ranged(url: str, session: Any, **kwargs: Any) -> tuple[int, bool]:
    headers = {**(kwargs.get("headers") or {}), "Range": "bytes=0-0"}
    if not any(str(k).lower() == "accept-encoding" for k in headers):
        headers["Accept-Encoding"] = "identity"
    rest = {k: v for k, v in kwargs.items() if k != "headers"}
    if is_rnet_session(session):
        rest.setdefault("read_timeout", rnet_read_timeout())
        rest.setdefault("max_retries", 0)
    else:
        rest.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
        session = no_retry_session(session)
    try:
        resp = session.get(url, stream=True, headers=headers, **rest)
    except Exception:
        return 0, False
    try:
        if resp.status_code != 206:
            return 0, False
        if is_content_encoded(resp.headers.get("Content-Encoding") or resp.headers.get("content-encoding")):
            return 0, False
        content_range = resp.headers.get("Content-Range") or resp.headers.get("content-range") or ""
        total = content_range.rsplit("/", 1)[-1].strip()
        return (int(total), True) if total.isdigit() else (0, False)
    finally:
        try:
            declared = int(resp.headers.get("Content-Length") or resp.headers.get("content-length") or 0)
            if resp.status_code == 206 and 0 < declared <= PROBE_DRAIN_LIMIT:
                _ = resp.content
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass


def has_range_header(item: dict[str, Any]) -> bool:
    """True when a URL item's per-request headers already carry a Range.

    Such an item is itself a byte-range slice of a larger resource (DASH SegmentBase,
    HLS EXT-X-BYTERANGE). Probing or part-mode would clobber that Range and fetch the
    wrong bytes, so ranged-parallel strategies must skip these items entirely.
    """
    return any(k.lower() == "range" for k in (item.get("headers") or {}))


def range_covers_full_body(headers: MutableMapping[str, Any], content_length: int) -> bool:
    """True when a 200 body is exactly the bytes the item's Range slice asked for.

    A server may ignore Range and answer 200 with the whole resource (RFC 9110
    permits this). That body is only the right bytes when the slice starts at byte 0
    and spans exactly the full body. Anything else (open-ended, multi-range, nonzero
    start, span mismatch) stays fail-closed.
    """
    value = str(next((v for k, v in headers.items() if k.lower() == "range"), ""))
    match = re.fullmatch(r"bytes=(\d+)-(\d+)", value.strip())
    if not match or content_length <= 0:
        return False
    start, end = int(match.group(1)), int(match.group(2))
    return start == 0 and content_length == end + 1


def parse_content_range(value: Any) -> Optional[tuple[int, int, Optional[int]]]:
    """Parse a 206's ``Content-Range`` into ``(start, end, total)``. Gives None when absent or unparseable.

    ``total`` is None for an unknown complete length (``*``). None fails closed: a caller
    must not place bytes at an offset it cannot prove the server agreed to.
    """
    if isinstance(value, bytes):
        value = value.decode("latin1", "replace")
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", str(value or "").strip(), re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), None if match.group(3) == "*" else int(match.group(3))


def parse_content_range_unsatisfied(value: Any) -> Optional[int]:
    """Parse a 416's ``Content-Range`` into the complete length. Gives None when absent or unknown.

    A 416 answers with ``bytes */N`` (or ``bytes */*``), which the 206 form does not cover.
    """
    if isinstance(value, bytes):
        value = value.decode("latin1", "replace")
    match = re.fullmatch(r"bytes\s+\*/(\d+)", str(value or "").strip(), re.IGNORECASE)
    return int(match.group(1)) if match else None


def range_clamped_to_end(content_range: tuple[int, int, Optional[int]], slice_end: int) -> bool:
    """Whether a 206 stops early only because the requested range ends past the resource.

    RFC 9110 clamps a range end at or beyond the complete length to the last byte, so the
    response still carries the whole slice. Some manifests author such ranges.
    """
    _, end, total = content_range
    return total is not None and end == total - 1 and slice_end >= total


def request_range(headers: MutableMapping[str, Any]) -> Optional[tuple[int, Optional[int]]]:
    """
    The request's ``Range`` as ``(start, end)``. Gives None when absent or not ``bytes=start-...``.

    ``end`` is None for an open-ended ``bytes=start-``, which the caller cannot hold a
    response to.
    """
    value = str(next((v for k, v in headers.items() if str(k).lower() == "range"), ""))
    match = re.match(r"bytes=(\d+)-(\d+)?", value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)) if match.group(2) else None


def force_unblock_stream(stream: Any) -> None:
    """Best-effort: shut the stream's socket so a thread parked in a blocking read returns now.

    A superseded hedge loser can sit in a full-chunk ``raw.read`` on a slow connection. The
    batch's wait-gated shutdown (which guarantees that every thread has closed its file before
    the merge sweep) would otherwise drain that whole slow read. ``socket.shutdown`` makes the
    read return at once so the loser unwinds and closes its ``.!dev`` handle immediately.
    Requests/urllib3 only (rnet exposes no reachable socket). This function ignores any failure,
    and the caller falls back to waiting.
    """
    try:
        stream.raw._fp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass


def split_ranges(size: int, max_parts: Optional[int], part_target: int) -> list[tuple[int, int]]:
    """Split ``[0, size)`` into inclusive ``[start, end]`` byte ranges with no gaps or overlaps.

    Parts of ~``part_target`` bytes each, at most ``max_parts`` of them when given. Pure, with no I/O, so testable.
    """
    n_parts = max(1, math.ceil(size / part_target))
    if max_parts is not None:
        n_parts = max(1, min(max_parts, n_parts))
    part_size = math.ceil(size / n_parts)
    return [
        (s, e) for i in range(n_parts) for s, e in [(i * part_size, min(size - 1, (i + 1) * part_size - 1))] if s <= e
    ]


def plan_tail_parts(size: int, spare: int) -> list[tuple[int, int]]:
    """Plan intra-segment range parts for a tail segment.

    Returns inclusive ``[start, end]`` byte ranges covering ``[0, size)`` with no gaps or
    overlaps, split into at most ``spare`` parts of ~``TAIL_BOOST_PART_SIZE``. Returns an
    empty list when the segment is smaller than ``TAIL_BOOST_MIN_SEGMENT_SIZE`` (not worth a
    probe+split) or there are fewer than two spare workers. Pure, with no I/O, so testable.
    """
    if size < TAIL_BOOST_MIN_SEGMENT_SIZE or spare < 2:
        return []
    return split_ranges(size, spare, TAIL_BOOST_PART_SIZE)


def submission_window(target: int) -> int:
    """Futures to keep queued for ``target`` download workers.

    Submitting every segment up front convoys on the pool's global lock and starves the event
    drain, so the drain loop meters submission through a window it tops up. The window is
    twice the worker target: a worker that finishes a segment finds its next one already staged
    instead of idling until the next drain cycle stages it. A pure function, so the ratio is
    unit-testable and fixed mode and adaptive mode cannot drift apart.
    """
    return target * 2


def tail_boost_engages(remaining: int, pending: int, target: int) -> bool:
    """True when idle workers outnumber the remaining whole segments.

    That is the tail condition where one download worker for each remaining segment would
    leave workers idle, so a split of those segments into range parts keeps throughput up.
    Gating on idle capacity (not a fixed segment count) is what lets the boost fire despite
    ``remaining`` draining in strides of the download worker target. Pure, so the gate is unit-testable.
    """
    spare = target - pending
    return remaining > 0 and spare >= 2 and remaining <= spare


def dispatch_parts(
    url: str,
    save_path: Path,
    session: Any,
    total_size: int,
    max_workers: int,
    **kwargs: Any,
) -> Generator[dict[str, Any], None, None]:
    save_path.parent.mkdir(parents=True, exist_ok=True)
    control_file = save_path.with_name(f"{save_path.name}.!dev")

    part_target = min(RANGE_PARALLEL_PART_SIZE, max(RANGE_PARALLEL_MIN_PART_SIZE, math.ceil(total_size / max_workers)))
    parts = split_ranges(total_size, None, part_target)

    control_file.write_bytes(b"")
    with open(save_path, "wb") as f:
        f.truncate(total_size)

    events: Queue[dict[str, Any]] = Queue()
    abort = threading.Event()

    def worker(start: int, end: int) -> None:
        for ev in download(
            url=url,
            save_path=save_path,
            session=session,
            part_offset=start,
            part_end=end,
            abort=abort,
            **kwargs,
        ):
            events.put(ev)

    pool = ThreadPoolExecutor(max_workers=min(max_workers, len(parts)))
    futures = [pool.submit(worker, s, e) for s, e in parts]
    pending = set(futures)

    yield {"total": total_size}

    total_bytes = 0
    last_report = time.time()
    speed_window = SpeedWindow(last_report)
    completed = False
    worker_error = False

    try:
        while pending:
            advance = 0
            while not events.empty():
                try:
                    ev = events.get_nowait()
                except Empty:
                    break
                a = ev.get("advance")
                if a:
                    advance += a
            if advance:
                total_bytes += advance
                yield {"advance": advance}

            now = time.time()
            if now - last_report > 0.5 and total_bytes > 0:
                rate = speed_window.rate(now, total_bytes)
                if rate:
                    yield {"downloaded": f"{filesize.decimal(math.ceil(rate))}/s"}
                last_report = now

            done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for fut in done:
                exc = fut.exception()
                if exc:
                    worker_error = True
                    abort.set()
                    raise exc

        advance = 0
        while not events.empty():
            try:
                ev = events.get_nowait()
            except Empty:
                break
            a = ev.get("advance")
            if a:
                advance += a
        if advance:
            total_bytes += advance
            yield {"advance": advance}

        if DOWNLOAD_CANCELLED.is_set() or abort.is_set():
            return
        yield {"file_downloaded": save_path, "written": total_size}
        completed = True
    except KeyboardInterrupt:
        DOWNLOAD_CANCELLED.set()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        if not completed:
            abort.set()
        pool.shutdown(wait=worker_error, cancel_futures=True)
        if completed:
            control_file.unlink(missing_ok=True)


def download(
    url: str,
    save_path: Path,
    session: Optional[Any] = None,
    segmented: bool = False,
    part_offset: Optional[int] = None,
    part_end: Optional[int] = None,
    claimed: Optional[Callable[[], bool]] = None,
    racing: Optional[Callable[[], bool]] = None,
    abort: Optional[threading.Event] = None,
    on_retry: Optional[Callable[[Exception], None]] = None,
    register: Optional[Callable[[Any, bool], None]] = None,
    **kwargs: Any,
) -> Generator[dict[str, Any], None, None]:
    """
    Download a file with optimized I/O.

    Supports both requests.Session and RnetSession for TLS fingerprinting.
    RnetSession streams natively. requests.Session reads the raw socket, except
    on a content-encoded body: only iter_content decodes such a body.

    Yields these download status updates while the segments download:

    - {total: 123} (there are 123 segments to download)
    - {total: None} (there are an unknown number of segments to download)
    - {advance: 1} (the downloader finished one segment)
    - {downloaded: "10.1 MB/s"} (currently downloading at a rate of 10.1 MB/s)
    - {file_downloaded: Path(...), written: 1024} (download finished, has the save path and size)

    Parameters:
        url: Web URL of a file to download.
        save_path: The path to save the file to. If the save path's directory does not
            exist then it will be made automatically.
        session: A requests.Session or RnetSession to make HTTP requests with.
            RnetSession preserves TLS fingerprinting for services that need it.
        segmented: If downloads are segments or parts of one bigger file.
        part_offset: Byte offset to write at within a pre-allocated file. When set
            (with `part_end`), enables part mode for parallel ranged downloads:
            no truncate, no skip-if-exists, no control file. Part mode emits only
            `advance` events, and a retry resumes mid-part with a Range header.
        part_end: Inclusive end byte of the part. Required when `part_offset` is set.
        claimed: Optional predicate checked before every try. When it returns True,
            the download stops silently (another download worker already delivered
            this file).
        racing: Optional predicate that selects the read granularity for each iteration.
            Requires `claimed`. The download uses read1 (per-network-arrival reads) only
            on the iterations where the predicate returns True, because the batch then
            hedge-races this segment and a superseded loser must notice `claimed()`
            mid-read. On every other iteration the download does a blocking full-chunk
            read, which avoids the per-record read tax on a segment with no race.
        abort: Optional local cancel signal. When set, the download stops like a cancel
            (keeps its partial, returns silently, does not raise). Used by ranged-parallel
            downloads to abort sibling parts without touching the process-global cancel.
        on_retry: Optional callback invoked with the raised exception before each retry
            wait (not on cancel or final exhaustion). Used to feed CDN error signals to
            the ``AdaptiveWorkerController``. The downloader ignores callback errors.
        register: Optional callback ``(stream, active)`` invoked with active=True while the
            downloader reads a response, and with active=False when that read ends. Lets the batch reach
            a superseded loser's socket to unblock its in-flight read at teardown (see
            force_unblock_stream) instead of waiting the slow read out.
        kwargs: Any extra keyword arguments to pass to the session.get() call. Use this
            for one-time request changes like a header, cookie, or proxy. For example,
            to request Byte-ranges use e.g., `headers={"Range": "bytes=0-128"}`.
    """
    session = session or Session()
    if is_requests_session(session):
        session = no_retry_session(session)
    part_mode = part_offset is not None and part_end is not None

    tmp_file = save_path.with_name(f"{save_path.name}.!dev")
    if not segmented:
        save_path.parent.mkdir(parents=True, exist_ok=True)

    resume_offset = 0
    if not part_mode:
        if save_path.exists():
            if tmp_file.exists():
                tmp_file.unlink()
                save_path.unlink()
            else:
                yield dict(file_downloaded=save_path, written=save_path.stat().st_size)
                return
        elif tmp_file.exists():
            resume_offset = tmp_file.stat().st_size

    _time = time.time
    use_raw = is_requests_session(session)
    item_range = has_range_header(kwargs)

    attempts = 1
    outages = 0
    failures = 0
    started = _time()
    written = 0
    resumes = 0
    request_limit: Optional[int] = None
    known_total: Optional[int] = None
    high_water = 0
    progress_seeded = False
    while True:
        if claimed is not None and claimed():
            return
        if DOWNLOAD_CANCELLED.is_set() or (abort is not None and abort.is_set()):
            return
        if not part_mode:
            written = 0
        secured = written if part_mode else resume_offset
        resume_start = resume_offset
        last_speed_refresh = _time()

        stream: Any = None
        try:
            use_rnet = is_rnet_session(session)

            request_kwargs = dict(kwargs)
            if use_rnet:
                request_kwargs.setdefault("read_timeout", rnet_read_timeout())
                request_kwargs.setdefault("max_retries", 0)
            else:
                request_kwargs.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
            req_headers = dict(request_kwargs.get("headers", {}) or {})
            if not any(str(k).lower() == "accept-encoding" for k in req_headers):
                req_headers["Accept-Encoding"] = "identity"
            chunk_end: Optional[int] = None
            if part_mode and part_offset is not None and part_end is not None:
                req_start = part_offset + written
                chunk_end = part_end
                if request_limit is not None and part_end - req_start + 1 > request_limit:
                    chunk_end = req_start + request_limit - 1
                req_headers["Range"] = f"bytes={req_start}-{chunk_end}"
            elif resume_offset > 0 and not item_range:
                if (
                    request_limit is not None
                    and known_total is not None
                    and known_total - resume_offset > request_limit
                ):
                    chunk_end = resume_offset + request_limit - 1
                    req_headers["Range"] = f"bytes={resume_offset}-{chunk_end}"
                else:
                    req_headers["Range"] = f"bytes={resume_offset}-"
            request_kwargs["headers"] = req_headers

            stream = session.get(url, stream=True, **request_kwargs)

            if (not part_mode) and (not item_range) and resume_offset > 0 and stream.status_code == 416:
                complete = parse_content_range_unsatisfied(
                    stream.headers.get("Content-Range") or stream.headers.get("content-range")
                )
                try:
                    stream.close()
                except Exception:
                    pass
                if complete == resume_offset and known_total in (None, resume_offset):
                    if not segmented and not progress_seeded:
                        progress_seeded = True
                        yield dict(total=resume_offset)
                        yield dict(advance=resume_offset)
                    os.replace(tmp_file, save_path)
                    yield dict(file_downloaded=save_path, written=resume_offset)
                    if segmented:
                        yield dict(advance=1)
                    break
                tmp_file.unlink(missing_ok=True)
                raise IOError(f"resume from byte {resume_offset} got a 416, complete length {complete}")

            stream.raise_for_status()

            resumed = (not part_mode) and (not item_range) and resume_offset > 0 and stream.status_code == 206
            if resumed:
                content_range = parse_content_range(
                    stream.headers.get("Content-Range") or stream.headers.get("content-range")
                )
                if content_range is None or content_range[0] != resume_offset:
                    start, end, total = content_range or (None, None, None)
                    if start == 0 and total is not None and end == total - 1:
                        resumed = False
                        resume_offset = 0
                    else:
                        tmp_file.unlink(missing_ok=True)
                        raise IOError(f"resume from byte {resume_offset} got Content-Range {content_range!r}")
                elif content_range[2] is not None:
                    known_total = content_range[2]
            if (not part_mode) and resume_offset > 0 and not resumed:
                resume_offset = 0
            if part_mode and part_offset is not None and part_end is not None:
                if stream.status_code != 206:
                    raise IOError(f"expected 206 for ranged part, got {stream.status_code}")
                content_range = parse_content_range(
                    stream.headers.get("Content-Range") or stream.headers.get("content-range")
                )
                if (
                    content_range is None
                    or content_range[0] != part_offset + written
                    or (chunk_end is not None and content_range[1] > chunk_end)
                    or (content_range[2] is not None and content_range[2] <= part_end)
                ):
                    raise IOError(f"part {part_offset}-{part_end} got Content-Range {content_range!r}")
            if use_rnet:
                content_encoded = is_content_encoded(
                    stream.headers.get("Content-Encoding") or stream.headers.get("content-encoding")
                )
                content_length = 0 if content_encoded else (stream.content_length or 0)
            else:
                content_encoded = is_content_encoded(stream.headers.get("Content-Encoding"))
                try:
                    content_length = int(stream.headers.get("Content-Length", "0"))
                    if content_encoded:
                        content_length = 0
                except ValueError:
                    content_length = 0

            if known_total is None and not part_mode and not item_range and stream.status_code == 200:
                known_total = content_length or None

            if known_total is None and not part_mode and not item_range and stream.status_code == 206:
                fresh_range = parse_content_range(
                    stream.headers.get("Content-Range") or stream.headers.get("content-range")
                )
                if fresh_range is not None:
                    if fresh_range[0] != resume_offset:
                        raise IOError(f"206 body starts at byte {fresh_range[0]}, expected {resume_offset}")
                    known_total = fresh_range[2]

            if item_range and not part_mode and stream.status_code != 206:
                if not (stream.status_code == 200 and range_covers_full_body(req_headers, content_length)):
                    raise IOError(f"expected 206 for byte-range segment, got {stream.status_code}")

            if item_range and not part_mode and stream.status_code == 206:
                slice_range = request_range(req_headers)
                if slice_range is not None:
                    slice_start, slice_end = slice_range
                    content_range = parse_content_range(
                        stream.headers.get("Content-Range") or stream.headers.get("content-range")
                    )
                    if (
                        content_range is None
                        or content_range[0] != slice_start
                        or (
                            slice_end is not None
                            and content_range[1] != slice_end
                            and not range_clamped_to_end(content_range, slice_end)
                        )
                    ):
                        raise IOError(
                            f"byte-range segment got Content-Range {content_range!r}, "
                            f"expected {slice_start}-{slice_end if slice_end is not None else ''}"
                        )
                    if not content_encoded:
                        content_length = content_range[1] - content_range[0] + 1

            if resumed and content_encoded:
                tmp_file.unlink(missing_ok=True)
                raise IOError(f"resume from byte {resume_offset} came back content-encoded")

            limiter = speed_limiter
            chunk_size = adaptive_chunk_size(content_length)
            if limiter:
                chunk_size = min(chunk_size, max(8192, int(limiter.rate / 4)))
            secured = written if part_mode else resume_offset
            total_size = (resume_offset + content_length) if resumed and content_length > 0 else content_length

            if not segmented and not part_mode and not progress_seeded:
                progress_seeded = True
                if total_size > 0:
                    yield dict(total=total_size)
                else:
                    yield dict(total=None)
                if resumed and resume_offset > 0:
                    yield dict(advance=resume_offset)

            if part_mode:
                file_mode = "r+b"
                file_buffering = 0
            else:
                file_mode = "ab" if resumed else "wb"
                file_buffering = 1_048_576
            with open(save_path if part_mode else tmp_file, file_mode, buffering=file_buffering) as f:
                if part_mode and part_offset is not None:
                    f.seek(part_offset + written)

                _write = f.write

                if use_rnet:
                    chunks = stream.stream()
                elif use_raw and not content_encoded:
                    _read1 = getattr(stream.raw, "read1", None) if claimed is not None else None
                    if _read1 is not None and racing is not None:
                        _raw_read = stream.raw.read
                        chunks = iter(lambda: _read1(chunk_size) if racing() else _raw_read(chunk_size), b"")
                    else:
                        chunks = iter(lambda: stream.raw.read(chunk_size), b"")
                else:
                    chunks = stream.iter_content(chunk_size=chunk_size)

                _data_accumulated = 0
                _bytes_since_yield = 0
                emit_progress = (not segmented) or part_mode
                if register is not None:
                    register(stream, True)
                try:
                    for chunk in chunks:
                        if DOWNLOAD_CANCELLED.is_set() or (abort is not None and abort.is_set()):
                            break
                        if claimed is not None and claimed():
                            return
                        if limiter is not None:
                            limiter.consume(len(chunk))
                        _write(chunk)
                        download_size = len(chunk)
                        written += download_size

                        if emit_progress:
                            _bytes_since_yield += download_size
                            _data_accumulated += download_size
                            now = _time()
                            time_since = now - last_speed_refresh
                            if time_since > PROGRESS_WINDOW:
                                yield dict(advance=_bytes_since_yield)
                                _bytes_since_yield = 0
                                if not part_mode:
                                    download_speed = math.ceil(_data_accumulated / (time_since or 1))
                                    yield dict(downloaded=f"{filesize.decimal(download_speed)}/s")
                                last_speed_refresh = now
                                _data_accumulated = 0

                    if emit_progress and _bytes_since_yield > 0:
                        yield dict(advance=_bytes_since_yield)
                finally:
                    if register is not None:
                        register(stream, False)
                    try:
                        stream.close()
                    except Exception:
                        pass

            aborted = abort is not None and abort.is_set()
            if (DOWNLOAD_CANCELLED.is_set() or aborted) and (not content_length or written < content_length):
                return

            if part_mode and part_offset is not None and part_end is not None:
                expected = part_end - part_offset + 1
                target = expected if chunk_end is None else chunk_end - part_offset + 1
                if written < target:
                    raise IOError(f"Failed to read part {part_offset}-{part_end}: got {written}/{expected}")
                if written < expected:
                    attempts = 1
                    outages = 0
                    continue
            elif content_length and written < content_length:
                raise IOError(f"Failed to read {content_length} bytes from the track URI.")

            if not part_mode:
                if known_total is not None:
                    incomplete = resume_offset + written < known_total
                else:
                    incomplete = written == 0 or (stream.status_code == 206 and not item_range)
                if incomplete:
                    if resume_offset + written <= resume_start:
                        raise IOError(f"range from byte {resume_start} returned no bytes past it")
                    resume_offset += written
                    if resume_offset > high_water:
                        high_water = resume_offset
                        attempts = 1
                        outages = 0
                    continue
                os.replace(tmp_file, save_path)
                yield dict(file_downloaded=save_path, written=resume_offset + written)
                if segmented:
                    yield dict(advance=1)
            break
        except Exception as exc:
            try:
                stream.close()
            except Exception:
                pass
            if claimed is not None and claimed():
                return
            failures += 1
            cancelled = DOWNLOAD_CANCELLED.is_set() or (abort is not None and abort.is_set())
            outage = stream is None and is_outage(exc)
            if outage:
                outages += 1
            disk_error = getattr(exc, "errno", None) in LOCAL_DISK_ERRNOS
            limit = OUTAGE_ATTEMPTS if outage else MAX_ATTEMPTS
            if cancelled or disk_error or (outages if outage else attempts) >= limit:
                if not cancelled:
                    exc.add_note(f"{GAVE_UP_NOTE} {failures} failed attempts over {_time() - started:.0f}s")
                    raise
                return
            if not part_mode:
                resume_offset = tmp_file.stat().st_size if tmp_file.exists() else 0
            progress = written if part_mode else (0 if item_range else resume_offset)
            if progress - secured >= MIN_RESUME_PROGRESS and resumes < MAX_RESUMES:
                resumes += 1
                attempts = 0
                if not item_range:
                    request_limit = (
                        REQUEST_CHUNK_SIZE if request_limit is None else max(MIN_REQUEST_CHUNK, request_limit // 2)
                    )
            elif on_retry is not None:
                try:
                    on_retry(exc)
                except Exception:
                    pass
            tries = outages if outage else max(1, attempts)
            delay = retry_sleep(exc, tries)
            from unshackle.core.session import failure_reason

            log_event(
                "segment_retry",
                message=f"{failure_reason(exc)}, retry {tries}/{limit - 1} in {delay:.1f}s",
                context={"file": save_path.name, "failures": failures, "resumes": resumes},
            )
            if abort is not None:
                if abort.wait(delay):
                    return
            else:
                if DOWNLOAD_CANCELLED.wait(delay):
                    return
            if not outage:
                attempts += 1


def rebuildable_adapter(adapter: Any) -> bool:
    """Whether a child can remount ``adapter`` with the same transport behaviour.

    A child mounts a plain HTTPAdapter. TimeoutHTTPAdapter counts as the same transport here:
    its one override supplies a default timeout when the caller passes none, and every request
    this module sends carries an explicit timeout. Any other subclass changes the transport
    itself, which a spec cannot carry: SSLCiphers swaps the SSL context, so a child would
    negotiate a different cipher list and the host would reject it.
    """
    from unshackle.core.service import TimeoutHTTPAdapter

    return type(adapter) in (HTTPAdapter, TimeoutHTTPAdapter)


def build_session_spec(session: Optional[Any]) -> Optional[dict[str, Any]]:
    """Picklable spec to rebuild ``session`` in a child process. Gives None when it is not cheaply rebuildable.

    Live sessions (sockets, TLS state, threads) cannot cross a process boundary, so each child
    reconstructs its own from cheap state. State a child cannot reproduce gives None, and the
    caller falls back to a single process: a slower correct download beats a broken one.
    RnetSession needs a resolvable impersonate preset. A requests session needs every mounted
    adapter to be one a child can remount, and a spec spawn can pickle.
    """
    if session is None:
        return {"kind": "none"}
    if is_requests_session(session):
        if not all(rebuildable_adapter(adapter) for adapter in session.adapters.values()):
            return None
        spec: dict[str, Any] = {
            "kind": "requests",
            "headers": dict(session.headers),
            "cookies": session.cookies,
            "proxies": dict(session.proxies),
            "trust_env": session.trust_env,
            "params": copy(session.params),
            "verify": session.verify,
            "cert": session.cert,
            "auth": session.auth,
        }
        try:
            pickle.dumps(spec)
        except Exception:
            return None
        return spec
    if is_rnet_session(session):
        name = session.impersonate_name
        if not name:
            return None
        return {
            "kind": "rnet",
            "impersonate": name,
            "headers": dict(session.headers),
            "cookies": session.cookies.snapshot(),
            "origin_cookies": session.export_origin_cookies(),
            "proxy": session.proxies.get("all") or session.proxies.get("https") or session.proxies.get("http"),
        }
    return None


def default_max_workers() -> int:
    """Return the per-track worker count to use when the caller gives none."""
    return min(16, (os.cpu_count() or 1) + 4)


def rebuild_session(spec: dict[str, Any], max_workers: int) -> Optional[Any]:
    """Reconstruct an HTTP session inside a child process from a spec built by ``build_session_spec``.

    ``max_workers`` sizes the connection pool to what this child runs. The rebuilt HTTP session
    belongs to one child alone, so mounting an adapter here is safe where remounting a
    caller-provided HTTP session in ``requests()`` is not.
    """
    kind = spec["kind"]
    if kind == "requests":
        rs = Session()
        rs.headers.update(spec["headers"])
        if spec.get("cookies"):
            rs.cookies.update(spec["cookies"])
        if spec.get("proxies"):
            rs.proxies.update(spec["proxies"])
        rs.trust_env = spec.get("trust_env", True)
        if spec.get("params"):
            rs.params = spec["params"]
        rs.verify = spec.get("verify", True)
        rs.cert = spec.get("cert")
        rs.auth = spec.get("auth")
        adapter = HTTPAdapter(pool_connections=max_workers, pool_maxsize=max_workers, pool_block=True)
        rs.mount("https://", adapter)
        rs.mount("http://", adapter)
        return rs
    if kind == "rnet":
        from unshackle.core.session import session as make_session

        ns = make_session(spec["impersonate"])
        if spec.get("headers"):
            ns.headers.update(spec["headers"])
        ns.cookies.update(spec.get("cookies") or [])
        if spec.get("proxy"):
            ns.proxies.update({"all": spec["proxy"]})
        ns.import_origin_cookies(spec.get("origin_cookies") or {})
        return ns
    return None


def mp_worker(queue: Any, kwargs: dict[str, Any]) -> None:
    """Child entry point (top-level so ``spawn`` can pickle it).

    Rebuilds the HTTP session, iterates ``requests()`` for its url slice, and relays every event
    over the shared queue. Errors travel as a ``__mp_error__`` sentinel. This child always sends a
    ``__mp_done__`` sentinel last, so the parent knows that this child is complete.
    """
    try:
        overrides = os.environ.get("UNSHACKLE_DL_TIMING_OVERRIDES")
        if overrides:
            for pair in overrides.split(","):
                name, _, value = pair.partition("=")
                if name in ("READ_TIMEOUT", "RETRY_WAIT", "HEDGE_MIN_WAIT"):
                    setattr(sys.modules[__name__], name, float(value))
                if name == "READ_TIMEOUT":
                    config.network = {**config.network, "read_timeout": float(value)}
        spec = kwargs.pop("_session_spec")
        kwargs["session"] = rebuild_session(spec, int(kwargs.get("max_workers") or 1))
        for event in requests(**kwargs):
            queue.put(event)
    except Exception as exc:
        queue.put(
            {
                "__mp_error__": " ".join(f"{type(exc).__name__}: {exc}".split()),
                "__mp_traceback__": traceback.format_exc(),
                "__mp_notes__": [str(note) for note in getattr(exc, "__notes__", ())],
            }
        )
    finally:
        queue.put({"__mp_done__": True})


def download_multiprocess(
    urls: list[Any],
    output_dir: Path,
    filename: str,
    headers: Optional[MutableMapping[str, Union[str, bytes]]],
    cookies: Optional[Union[MutableMapping[str, str], CookieJar]],
    proxy: Optional[str],
    max_workers: int,
    adaptive: bool,
    spec: dict[str, Any],
    processes: int,
    debug_logger: Any,
) -> Generator[dict[str, Any], None, None]:
    """Fan segments across spawned children, each running ``requests()`` on a strided slice.

    Child ``k`` takes segments ``k, k+processes, k+2*processes, ...``: slow segments tend to
    cluster by position (a cold edge, a throttled range region), and striding spreads such a
    cluster across all children instead of trapping it in one child whose few workers would
    bound the tail. Children keep their original global indices with ``index_offset`` and
    ``index_stride``, so the file names do not change. The parent owns aggregate progress: it emits
    ``total`` once and computes one speed string from written bytes, dropping the children's
    own ``total`` and speed events.
    """
    n = len(urls)
    processes = max(1, min(processes, n))
    per_child_workers = max(1, max_workers // processes)
    pass_through = spec["kind"] == "none"

    chunks: list[tuple[int, list[Any]]] = []
    for k in range(processes):
        chunks.append((k, urls[k::processes]))

    if debug_logger:
        debug_logger.log(
            level="DEBUG",
            operation="downloader_mp_start",
            message="Starting multiprocess segment download",
            context={
                "url_count": n,
                "processes": len(chunks),
                "workers_per_child": per_child_workers,
                "chunk_sizes": [len(c) for _, c in chunks],
                "session_kind": spec["kind"],
                "adaptive": adaptive,
            },
        )

    mp_ctx = multiprocessing.get_context("spawn")
    queue: Any = mp_ctx.Queue()
    procs: list[Any] = []
    for offset, chunk in chunks:
        child_kwargs = dict(
            urls=chunk,
            output_dir=output_dir,
            filename=filename,
            headers=headers if pass_through else None,
            cookies=cookies if pass_through else None,
            proxy=proxy if pass_through else None,
            max_workers=per_child_workers,
            adaptive=adaptive,
            processes=1,
            index_offset=offset,
            index_stride=processes,
            _session_spec=spec,
        )
        p = mp_ctx.Process(target=mp_worker, args=(queue, child_kwargs), daemon=True)
        p.start()
        procs.append(p)

    yield dict(total=n)

    total_bytes = 0
    start_time = time.time()
    last_speed_report = start_time
    speed_window = SpeedWindow(start_time)
    done_count = 0

    dead_ticks = 0
    try:
        while done_count < len(procs):
            if DOWNLOAD_CANCELLED.is_set():
                for p in procs:
                    p.terminate()
                yield dict(downloaded="[yellow]CANCELLED")
                return
            try:
                event = queue.get(timeout=0.1)
            except Empty:
                event = None
                if any(p.exitcode not in (None, 0) for p in procs):
                    dead_ticks += 1
                    if dead_ticks >= 3:
                        for p in procs:
                            p.terminate()
                        codes = [p.exitcode for p in procs]
                        raise RuntimeError(f"segment download child died without result (exitcodes: {codes})")
            if event is not None:
                dead_ticks = 0
                if "__mp_done__" in event:
                    done_count += 1
                    continue
                if "__mp_error__" in event:
                    for p in procs:
                        p.terminate()
                    log_event("segment_child_failed", level="ERROR", message=event.get("__mp_traceback__", ""))
                    child_error = RuntimeError(f"segment download child failed: {event['__mp_error__']}")
                    for note in event.get("__mp_notes__", ()):
                        child_error.add_note(note)
                    raise child_error
                if "total" in event:
                    continue
                if "downloaded" in event:
                    continue
                if "advance" in event:
                    continue
                written = event.get("written")
                if written:
                    total_bytes += written
                if "file_downloaded" in event:
                    yield dict(advance=1)
                yield event

            now = time.time()
            if now - last_speed_report > 0.5 and total_bytes > 0:
                rate = speed_window.rate(now, total_bytes)
                if rate:
                    yield dict(downloaded=f"{filesize.decimal(math.ceil(rate))}/s")
                last_speed_report = now
    except KeyboardInterrupt:
        DOWNLOAD_CANCELLED.set()
        for p in procs:
            p.terminate()
        yield dict(downloaded="[yellow]CANCELLED")
        raise
    finally:
        for p in procs:
            p.join(timeout=1)
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)


def requests(
    urls: Union[str, dict[str, Any], list[str], list[dict[str, Any]], list[Union[str, dict[str, Any]]]],
    output_dir: Path,
    filename: str,
    headers: Optional[MutableMapping[str, Union[str, bytes]]] = None,
    cookies: Optional[Union[MutableMapping[str, str], CookieJar]] = None,
    proxy: Optional[str] = None,
    max_workers: Optional[int] = None,
    session: Optional[Any] = None,
    adaptive: bool = False,
    processes: int = 1,
    index_offset: int = 0,
    index_stride: int = 1,
) -> Generator[dict[str, Any], None, None]:
    """
    Download files with optimized I/O and adaptive chunk sizing.

    Supports both requests.Session and RnetSession. When you give a RnetSession
    (for example, from a service's get_session()), every segment download keeps the
    TLS fingerprint.

    A file already at its computed save path counts as finished from an earlier run. This function
    reports it as downloaded at its on-disk size without any request, so a second call resumes a
    batch from the segments the earlier run completed.

    Yields these download status updates while the segments download:

    - {total: 123} (there are 123 segments to download)
    - {total: None} (there are an unknown number of segments to download)
    - {advance: 1} (the downloader finished one segment)
    - {downloaded: "10.1 MB/s"} (currently downloading at a rate of 10.1 MB/s)
    - {file_downloaded: Path(...), written: 1024} (download finished, has the save path and size)

    The data is in the same format accepted by rich's progress.update() function.
    However, The `downloaded`, `file_downloaded` and `written` keys are custom and not
    natively accepted by rich progress bars.

    Parameters:
        urls: Web URL(s) to file(s) to download. You can use a dictionary with the key
            "url" for the URI, and other keys for extra arguments to use per-URL.
        output_dir: The folder to save the file into. If the save path's directory does
            not exist then it will be made automatically.
        filename: The filename or filename template to use for each file. The variables
            you can use are `i` for the URL index and `ext` for the URL extension.
        headers: A mapping of HTTP Header Key/Values to use for all downloads.
        cookies: A mapping of Cookie Key/Values or a `CookieJar` to use for all downloads.
        proxy: An optional proxy URI to route connections through for all downloads.
        max_workers: The maximum amount of threads to use for downloads. Defaults to
            min(16, cpu_count + 4).
        session: An optional requests.Session or RnetSession to use. When you give one,
            this function uses it directly, which keeps the TLS fingerprint. With None,
            this function makes a new requests.Session with HTTPAdapter connection pooling.
        adaptive: Opt-in CDN-aware dynamic segment concurrency for segmented (multi-URL)
            downloads. When True the per-track download worker count starts moderate and
            ramps up or backs off based on measured throughput and CDN errors, capped at
            max_workers. When False (default) this function submits all segments upfront
            with a fixed download worker count, which matches the non-adaptive
            behaviour exactly.
        processes: Split a large segment batch across this many spawned child processes to
            beat the single-interpreter throughput cap (GIL in the ssl read path). Engaged
            only when > 1 and there are at least MP_MIN_SEGMENTS urls. If not, the behaviour
            is byte-identical to a single process. Each child runs its own download worker pool.
            Ignored while a download speed limit is set: spawned children cannot share the
            process-global rate budget, so the batch stays in a single process.
        index_offset: Added to each url's enumerate index (after ``index_stride`` scaling)
            when formatting ``filename`` so a child handling part of a larger batch keeps
            its urls' original global indices/names. Internal: set by the multiprocess
            path, leave at 0 otherwise.
        index_stride: Multiplies each url's enumerate index before ``index_offset`` is
            added, mapping a strided slice (``urls[k::stride]``) back to global indices.
            Internal: set by the multiprocess path, leave at 1 otherwise.
    """
    if not urls:
        raise ValueError("urls must be provided and not empty")
    elif not isinstance(urls, (str, dict, list)):
        raise TypeError(f"Expected urls to be {str} or {dict} or a list of one of them, not {type(urls)}")

    if not output_dir:
        raise ValueError("output_dir must be provided")
    elif not isinstance(output_dir, Path):
        raise TypeError(f"Expected output_dir to be {Path}, not {type(output_dir)}")

    if not filename:
        raise ValueError("filename must be provided")
    elif not isinstance(filename, str):
        raise TypeError(f"Expected filename to be {str}, not {type(filename)}")

    if not isinstance(headers, (MutableMapping, type(None))):
        raise TypeError(f"Expected headers to be {MutableMapping}, not {type(headers)}")

    if not isinstance(cookies, (MutableMapping, CookieJar, type(None))):
        raise TypeError(f"Expected cookies to be {MutableMapping} or {CookieJar}, not {type(cookies)}")

    if not isinstance(proxy, (str, type(None))):
        raise TypeError(f"Expected proxy to be {str}, not {type(proxy)}")

    if not isinstance(max_workers, (int, type(None))):
        raise TypeError(f"Expected max_workers to be {int}, not {type(max_workers)}")

    if not isinstance(processes, int):
        raise TypeError(f"Expected processes to be {int}, not {type(processes)}")

    debug_logger = get_debug_logger()

    if not isinstance(urls, list):
        urls = [urls]

    if not max_workers:
        max_workers = default_max_workers()

    limiter = speed_limiter
    if processes > 1 and len(urls) >= MP_MIN_SEGMENTS and limiter is not None:
        processes = 1
        if debug_logger:
            debug_logger.log(
                level="DEBUG",
                operation="downloader_mp_fallback",
                message="Speed limit is set; spawned children cannot share its budget, using a single process",
                context={"url_count": len(urls), "speed_limit": limiter.rate},
            )

    if processes > 1 and len(urls) >= MP_MIN_SEGMENTS:
        spec = build_session_spec(session)
        if spec is not None:
            yield from download_multiprocess(
                urls=cast("list[Any]", urls),
                output_dir=output_dir,
                filename=filename,
                headers=headers,
                cookies=cookies,
                proxy=proxy,
                max_workers=max_workers,
                adaptive=adaptive,
                spec=spec,
                processes=processes,
                debug_logger=debug_logger,
            )
            if DOWNLOAD_CANCELLED.is_set():
                raise DownloadCancelled()
            return
        if debug_logger:
            debug_logger.log(
                level="DEBUG",
                operation="downloader_mp_fallback",
                message="Session not rebuildable across processes; using a single process",
                context={"session_type": type(session).__name__, "url_count": len(urls)},
            )

    needs_ext = any(name == "ext" for _, name, _, _ in Formatter().parse(filename))
    urls = [
        dict(save_path=save_path, **url) if isinstance(url, dict) else dict(url=url, save_path=save_path)
        for i, url in enumerate(urls)
        for save_path in [
            output_dir
            / filename.format(
                i=i * index_stride + index_offset,
                ext=(get_extension(url["url"] if isinstance(url, dict) else url) or "") if needs_ext else "",
            )
        ]
    ]
    output_dir.mkdir(parents=True, exist_ok=True)

    if session is None:
        session = Session()
        if headers:
            headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
            session.headers.update(headers)
        if cookies:
            session.cookies.update(cookies)
        if proxy:
            session.proxies.update({"all": proxy})
            run_proxy.pin(session)
        adapter = HTTPAdapter(pool_connections=max_workers, pool_maxsize=max_workers, pool_block=True)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

    if debug_logger:
        first_url = urls[0].get("url", "") if urls else ""
        url_display = first_url[:200] + "..." if len(first_url) > 200 else first_url
        debug_logger.log(
            level="DEBUG",
            operation="downloader_start",
            message="Starting download",
            context={
                "url_count": len(urls),
                "first_url": url_display,
                "output_dir": str(output_dir),
                "filename": filename,
                "max_workers": max_workers,
                "has_proxy": bool(proxy),
                "session_type": type(session).__name__,
                "adaptive": adaptive,
            },
        )

    segmented_batch = len(urls) > 1

    if len(urls) == 1:
        url_item = urls[0]
        try:
            ranged_used = False
            if (
                max_workers > 1
                and speed_limiter is None
                and not has_range_header(url_item)
                and not url_item["save_path"].exists()
            ):
                probe_kwargs = {k: v for k, v in url_item.items() if k not in ("url", "save_path")}
                total_size, supports_ranges = probe_ranged(url_item["url"], session, **probe_kwargs)
                if supports_ranges and total_size >= RANGE_PARALLEL_MIN_SIZE:
                    try:
                        yield from dispatch_parts(
                            session=session,
                            total_size=total_size,
                            max_workers=max_workers,
                            **url_item,
                        )
                        ranged_used = True
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        save_path = url_item.get("save_path")
                        if save_path:
                            sp = Path(save_path)
                            for stray in (sp, sp.with_name(f"{sp.name}.!dev")):
                                try:
                                    stray.unlink(missing_ok=True)
                                except OSError:
                                    pass
                        if DOWNLOAD_CANCELLED.is_set():
                            return
                        status = permanent_status(exc)
                        fatal = status is not None
                        if debug_logger:
                            debug_logger.log(
                                level="DEBUG",
                                operation="ranged_parallel_failed",
                                message=f"ranged download failed: {exc.__class__.__name__}",
                                context={
                                    "error": exc.__class__.__name__,
                                    "status": status,
                                    "action": "raise" if fatal else "sequential_fallback",
                                },
                            )
                        if fatal:
                            raise
            if not ranged_used:
                yield from download(
                    session=session,
                    segmented=segmented_batch,
                    **url_item,
                )
        except KeyboardInterrupt:
            DOWNLOAD_CANCELLED.set()
            yield dict(downloaded="[yellow]CANCELLED")
            raise
    else:
        total_bytes = 0
        start_time = time.time()
        last_speed_report = start_time
        speed_window = SpeedWindow(start_time)
        last_hedge_check = 0.0
        hedge_median = 0.0
        hedge_median_len = 0

        pool = ThreadPoolExecutor(max_workers=max_workers)
        event_queue: Queue[dict[str, Any]] = Queue()

        seg_lock = threading.Lock()
        seg_start: dict[int, float] = {}
        seg_done: set[int] = set()
        seg_durations: list[float] = []
        hedged: set[int] = set()
        batch_abort = threading.Event()

        active_streams: set = set()
        active_lock = threading.Lock()

        def register_stream(stream: Any, active: bool) -> None:
            with active_lock:
                if active:
                    active_streams.add(stream)
                else:
                    active_streams.discard(stream)

        controller = AdaptiveWorkerController(cap=max_workers, start=max_workers) if adaptive else None
        on_retry_cb = (lambda _exc: controller.record_error(time.time())) if controller else None

        def download_worker(index: int, url_item: dict[str, Any], hedge: bool = False) -> None:
            item = dict(url_item)
            save_path = item.pop("save_path")
            if save_path.exists():
                with seg_lock:
                    if index in seg_done:
                        return
                    seg_done.add(index)
                event_queue.put(dict(file_downloaded=save_path, written=save_path.stat().st_size))
                event_queue.put(dict(advance=1))
                return
            target = save_path.with_name(f"{save_path.name}.{'h' if hedge else 'p'}.!dev")
            if not hedge:
                with seg_lock:
                    seg_start[index] = time.time()
            for event in download(
                session=session,
                segmented=segmented_batch,
                save_path=target,
                claimed=lambda: index in seg_done,
                racing=(lambda: True) if hedge else (lambda: index in hedged),
                abort=batch_abort,
                on_retry=on_retry_cb,
                register=register_stream,
                **item,
            ):
                if "file_downloaded" in event:
                    with seg_lock:
                        if index in seg_done:
                            target.unlink(missing_ok=True)
                            return
                        seg_done.add(index)
                        if not hedge:
                            seg_durations.append(time.time() - seg_start[index])
                    os.replace(target, save_path)
                    event = dict(event, file_downloaded=save_path)
                event_queue.put(event)

        remaining: deque[tuple[int, dict[str, Any]]] = deque(enumerate(urls))
        pending: set = set()

        tail_boosted: set[int] = set()
        tail_skipped: set[int] = set()
        part_aborts: list[threading.Event] = []

        def submit(count: int, only: Optional[set[int]] = None) -> None:
            if count <= 0 or not remaining:
                return
            picked: list[tuple[int, dict[str, Any]]] = []
            for item in remaining:
                if count <= 0:
                    break
                if only is not None and item[0] not in only:
                    continue
                pending.add(pool.submit(download_worker, item[0], item[1]))
                picked.append(item)
                count -= 1
            for item in picked:
                remaining.remove(item)

        def tail_part_worker(
            index: int,
            url: str,
            part_target: Path,
            save_path: Path,
            start: int,
            end: int,
            size: int,
            abort: threading.Event,
            parts_left: list[int],
            part_lock: threading.Lock,
            req_kwargs: dict[str, Any],
        ) -> None:
            failure: Optional[Exception] = None
            try:
                for _ev in download(
                    url=url,
                    save_path=part_target,
                    session=session,
                    part_offset=start,
                    part_end=end,
                    abort=abort,
                    on_retry=on_retry_cb,
                    **req_kwargs,
                ):
                    pass
            except BaseException as exc:
                abort.set()
                if not isinstance(exc, Exception):
                    raise
                failure = exc
            with part_lock:
                parts_left[0] -= 1
                last = parts_left[0] == 0
            if DOWNLOAD_CANCELLED.is_set():
                return
            if failure is not None or abort.is_set():
                if last:
                    part_target.unlink(missing_ok=True)
                if failure is not None:
                    raise TailPartFailed(index) from failure
                return
            if not last:
                return
            with seg_lock:
                if index in seg_done:
                    part_target.unlink(missing_ok=True)
                    return
                seg_done.add(index)
                seg_durations.append(time.time() - seg_start[index])
            os.replace(part_target, save_path)
            event_queue.put(dict(file_downloaded=save_path, written=size))
            event_queue.put(dict(advance=1))

        def maybe_tail_boost(target: int) -> None:
            if DOWNLOAD_CANCELLED.is_set() or not tail_boost_engages(len(remaining), len(pending), target):
                return
            if seg_done and (total_bytes / len(seg_done)) < TAIL_BOOST_MIN_SEGMENT_SIZE:
                tail_skipped.update(index for index, _ in remaining)
                return
            boosted = 0
            for index, url_item in list(remaining):
                if boosted >= TAIL_BOOST_MAX_PER_CYCLE:
                    break
                spare = target - len(pending)
                if spare < 2:
                    break
                if index in tail_boosted or index in tail_skipped:
                    continue
                item = dict(url_item)
                save_path = item.pop("save_path")
                if save_path.exists():
                    tail_skipped.add(index)
                    continue
                if has_range_header(item):
                    tail_skipped.add(index)
                    continue
                req_kwargs = {k: v for k, v in item.items() if k != "url"}
                size, supports_ranges = probe_ranged(item["url"], session, **req_kwargs)
                parts = plan_tail_parts(size, spare) if supports_ranges else []
                if not parts:
                    tail_skipped.add(index)
                    continue
                try:
                    remaining.remove((index, url_item))
                except ValueError:
                    tail_skipped.add(index)
                    continue
                part_target = save_path.with_name(f"{save_path.name}.tp.!dev")
                with open(part_target, "wb") as f:
                    f.truncate(size)
                seg_start[index] = time.time()
                tail_boosted.add(index)
                abort = threading.Event()
                part_aborts.append(abort)
                parts_left = [len(parts)]
                part_lock = threading.Lock()
                for start, end in parts:
                    pending.add(
                        pool.submit(
                            tail_part_worker,
                            index,
                            item["url"],
                            part_target,
                            save_path,
                            start,
                            end,
                            size,
                            abort,
                            parts_left,
                            part_lock,
                            req_kwargs,
                        )
                    )
                boosted += 1

        queue_depth = submission_window(max_workers)

        if controller:
            submit(controller.update(time.time(), len(remaining)))
        else:
            submit(queue_depth)

        pending_advance = 0

        try:
            while pending or remaining:
                while True:
                    try:
                        event = event_queue.get_nowait()
                    except Empty:
                        break
                    advance = event.get("advance")
                    if advance:
                        pending_advance += advance
                        continue
                    written = event.get("written")
                    if written:
                        total_bytes += written
                        if controller:
                            controller.record_bytes(written, time.time())
                    yield event

                if pending_advance > 0:
                    yield dict(advance=pending_advance)
                    pending_advance = 0

                if DOWNLOAD_CANCELLED.is_set():
                    remaining.clear()

                now = time.time()

                target = max_workers
                if controller:
                    target = controller.update(now, len(pending) + len(remaining))
                    maybe_tail_boost(target)
                    queue_target = submission_window(target)
                    if len(pending) < queue_target:
                        if remaining and len(remaining) <= target:
                            submit(target - len(pending), only=tail_skipped)
                        else:
                            submit(queue_target - len(pending))
                elif len(pending) < queue_depth:
                    submit(queue_depth - len(pending))

                if now - last_speed_report > 0.5:
                    if controller:
                        rolling = controller.speed(now)
                        if rolling > 0:
                            yield dict(downloaded=f"{filesize.decimal(math.ceil(rolling))}/s")
                            last_speed_report = now
                    elif total_bytes > 0:
                        rate = speed_window.rate(now, total_bytes)
                        if rate:
                            yield dict(downloaded=f"{filesize.decimal(math.ceil(rate))}/s")
                        last_speed_report = now

                if (
                    len(pending) < target
                    and seg_durations
                    and speed_limiter is None
                    and now - last_hedge_check > 0.5
                    and not DOWNLOAD_CANCELLED.is_set()
                ):
                    last_hedge_check = now
                    cur_len = len(seg_durations)
                    if cur_len != hedge_median_len:
                        hedge_median = statistics.median(seg_durations)
                        hedge_median_len = cur_len
                    threshold = max(HEDGE_FACTOR * hedge_median, HEDGE_MIN_WAIT)
                    with seg_lock:
                        stuck = [
                            i
                            for i, started in seg_start.items()
                            if i not in seg_done
                            and i not in hedged
                            and i not in tail_boosted
                            and now - started > threshold
                        ]
                    for i in stuck[: target - len(pending)]:
                        hedged.add(i)
                        pending.add(pool.submit(download_worker, i, urls[i], True))

                if len(seg_done) == len(urls):
                    break

                completed, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in completed:
                    err = future.exception()
                    if isinstance(err, KeyboardInterrupt):
                        raise KeyboardInterrupt()
                    elif isinstance(err, TailPartFailed) and permanent_status(err) is None:
                        if err.index not in tail_skipped:
                            tail_boosted.discard(err.index)
                            tail_skipped.add(err.index)
                            remaining.appendleft((err.index, urls[err.index]))
                        continue
                    elif err:
                        DOWNLOAD_CANCELLED.set()
                        yield dict(downloaded="[red]FAILING")
                        pool.shutdown(wait=False, cancel_futures=True)
                        yield dict(downloaded="[red]FAILED")
                        if debug_logger:
                            debug_logger.log(
                                level="ERROR",
                                operation="downloader_failed",
                                message=f"Download failed: {err}",
                                error=err,
                                context={
                                    "url_count": len(urls),
                                    "output_dir": str(output_dir),
                                },
                            )
                        raise err
        except KeyboardInterrupt:
            DOWNLOAD_CANCELLED.set()
            yield dict(downloaded="[yellow]CANCELLING")
            pool.shutdown(wait=False, cancel_futures=True)
            yield dict(downloaded="[yellow]CANCELLED")
            raise
        finally:
            batch_abort.set()
            for part_abort in part_aborts:
                part_abort.set()
            if not DOWNLOAD_CANCELLED.is_set():
                with active_lock:
                    losers = list(active_streams)
                for stream in losers:
                    force_unblock_stream(stream)
            pool.shutdown(wait=not DOWNLOAD_CANCELLED.is_set(), cancel_futures=True)

        while True:
            try:
                event = event_queue.get_nowait()
            except Empty:
                break
            advance = event.get("advance")
            if advance:
                pending_advance += advance
                continue
            written = event.get("written")
            if written:
                total_bytes += written
            yield event

        if pending_advance > 0:
            yield dict(advance=pending_advance)
        elapsed = time.time() - start_time
        if elapsed > 0 and total_bytes > 0:
            download_speed = math.ceil(total_bytes / elapsed)
            yield dict(downloaded=f"{filesize.decimal(download_speed)}/s")

    if DOWNLOAD_CANCELLED.is_set():
        raise DownloadCancelled()

    if debug_logger:
        debug_logger.log(
            level="DEBUG",
            operation="downloader_complete",
            message="Download completed successfully",
            context={
                "url_count": len(urls),
                "output_dir": str(output_dir),
                "filename": filename,
            },
        )


__all__ = ("requests", "format_speed", "parse_speed_limit", "set_speed_limit")
