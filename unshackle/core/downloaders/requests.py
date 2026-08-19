import math
import multiprocessing
import os
import random
import re
import socket
import statistics
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import FIRST_COMPLETED, wait
from concurrent.futures.thread import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar
from pathlib import Path
from queue import Empty, Queue
from string import Formatter
from typing import Any, Callable, Generator, MutableMapping, Optional, Union, cast

from requests import Session
from requests.adapters import HTTPAdapter
from rich import filesize

from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.utilities import get_debug_logger, get_extension

MAX_ATTEMPTS = 5
RETRY_WAIT = 2
PROGRESS_WINDOW = 2

# read timeout bounds the gap between chunks so a quiet connection errors instead of hanging
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30

# re-request a tail segment stuck past max(HEDGE_FACTOR * median segment time, HEDGE_MIN_WAIT)
HEDGE_FACTOR = 3
HEDGE_MIN_WAIT = 5.0

MIN_CHUNK = 524_288
MAX_CHUNK = 4_194_304
DEFAULT_CHUNK = 524_288
SPEED_ROLLING_WINDOW = 10  # seconds of history to keep for speed calculation

RANGE_PARALLEL_MIN_SIZE = 64 * 1024 * 1024
RANGE_PARALLEL_PART_SIZE = 16 * 1024 * 1024

# One CPython interpreter caps sustained segment throughput (GIL in the ssl read path);
# fan a big segment batch across spawned children to reach line rate. Below this count the
# spawn/rebuild overhead outweighs the win, so keep the in-process path.
MP_MIN_SEGMENTS = 24

# Tail-end boost (opt-in, adaptive only): when only a few segments remain and workers
# would otherwise idle, split each remaining segment into intra-segment range parts so
# aggregate throughput holds to the end instead of collapsing to (few)x(per-connection).
TAIL_BOOST_MAX_PER_CYCLE = 4  # cap on segments probed/split per drain cycle so blocking probes can't stall the loop
TAIL_BOOST_MIN_SEGMENT_SIZE = 8 * 1024 * 1024
TAIL_BOOST_PART_SIZE = 4 * 1024 * 1024


class SpeedWindow:
    """Rolling-window download rate over the last SPEED_ROLLING_WINDOW seconds."""

    def __init__(self, start: float) -> None:
        # zero-byte seed keeps early readings from exceeding the true rate
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
        # tokens may go negative; waiting for n > capacity would deadlock
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
        raise ValueError(f"Invalid speed limit {value!r}: use e.g. 500k, 5M, 1.5G, plain bytes/sec, or 'off'.")
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
    ``ADAPTIVE_STEP``. Target is
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

    def speed(self, now: float) -> float:
        """Rolling bytes/sec over the window."""
        self.prune(now)
        if not self._samples:
            return 0.0
        span = now - self._samples[0][0]
        if span <= 0:
            return 0.0
        return sum(n for _, n in self._samples) / span

    def warmed_up(self, now: float) -> bool:
        # half a tick of samples gives enough of a baseline to probe against; waiting for
        # a full speed window would idle the ramp at the start of every download.
        # measured from the first-ever sample so pruning cannot starve the check
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
            # tail guard: too little work to saturate the target, so a low measured speed
            # here reflects starvation rather than a plateau, so hold and skip the probe/revert this tick
            pass
        elif self._last_action == "increase" and speed_now < ADAPTIVE_PLATEAU_GAIN * self._speed_before_increase:
            # last probe upward did not pay off: revert it and hold
            self.target = max(ADAPTIVE_MIN, self._target_before_increase)
            self._last_action = None
            reason = "plateau"
        elif self.target < self.cap:
            self._speed_before_increase = speed_now
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


def retry_sleep(exc: Exception, attempts: int) -> float:
    """Seconds to wait before retry number ``attempts``.

    download() owns segment retries (the HTTP session itself does not retry), so backoff
    lives here: honor the server's Retry-After when the failure carries a response,
    else exponential growth from RETRY_WAIT with jitter so concurrently rate-limited
    segments do not retry in lockstep. Capped at ``session.MAX_BACKOFF`` either way.
    """
    from unshackle.core.session import MAX_BACKOFF

    response = getattr(exc, "response", None)
    if response is None:
        # RnetSession wraps forcelist failures in MaxRetriesError with the HTTPError as __cause__
        response = getattr(getattr(exc, "__cause__", None), "response", None)
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


def is_requests_session(session: Any) -> bool:
    """Whether the HTTP session is a standard requests.Session (it has ``resp.raw``)."""
    return isinstance(session, Session)


def is_rnet_session(session: Any) -> bool:
    """Whether the HTTP session is an RnetSession (it uses ``resp.stream()``)."""
    from unshackle.core.session import RnetSession

    return isinstance(session, RnetSession)


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
    rest = {k: v for k, v in kwargs.items() if k != "headers"}
    if is_rnet_session(session):
        rest.setdefault("read_timeout", READ_TIMEOUT)
        # let download() own segment retries, with no nested session-level retry loop
        rest.setdefault("max_retries", 0)
    else:
        rest.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
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


def split_ranges(size: int, max_parts: int, part_target: int) -> list[tuple[int, int]]:
    """Split ``[0, size)`` into inclusive ``[start, end]`` byte ranges with no gaps or overlaps.

    At most ``max_parts`` parts of ~``part_target`` bytes each. Pure, with no I/O, so testable.
    """
    n_parts = max(1, min(max_parts, math.ceil(size / part_target)))
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

    parts = split_ranges(total_size, max_workers, RANGE_PARALLEL_PART_SIZE)

    control_file.write_bytes(b"")
    with open(save_path, "wb") as f:
        f.truncate(total_size)

    events: Queue[dict[str, Any]] = Queue()
    # local to this ranged download: a failed part must not poison the process-global
    # DOWNLOAD_CANCELLED (which would kill the sequential fallback and sibling tracks)
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

    pool = ThreadPoolExecutor(max_workers=len(parts))
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
            # a cross-track cancel makes part workers return silently with partials, so
            # every future completes without error; finalizing here would strip the .!dev
            # control file and pass off a hole-filled pre-truncated file as complete
            return
        yield {"file_downloaded": save_path, "written": total_size}
        completed = True
    except KeyboardInterrupt:
        DOWNLOAD_CANCELLED.set()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        # abort outlives the shutdown so a leaked part exits at its next check instead of
        # writing into save_path after the caller moved on; a consumer that abandons the
        # generator (GeneratorExit) reaches here with abort still unset
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

    Yields these download status updates while the chunks download:

    - {total: 123} (there are 123 chunks to download)
    - {total: None} (there are an unknown number of chunks to download)
    - {advance: 1} (the downloader finished one chunk)
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
    part_mode = part_offset is not None and part_end is not None

    # partial data lives at the .!dev name and is renamed into place on completion:
    # bare save_path = done, bare .!dev = resumable, no per-segment marker files
    tmp_file = save_path.with_name(f"{save_path.name}.!dev")
    if not segmented:
        save_path.parent.mkdir(parents=True, exist_ok=True)

    resume_offset = 0
    if not part_mode:
        if save_path.exists():
            if tmp_file.exists():
                # legacy layout: empty marker beside a partial save_path, restart clean
                tmp_file.unlink()
                save_path.unlink()
            else:
                yield dict(file_downloaded=save_path, written=save_path.stat().st_size)
                return
        elif tmp_file.exists():
            resume_offset = tmp_file.stat().st_size

    _time = time.time
    use_raw = is_requests_session(session)
    # item carries its own Range (DASH SegmentBase / HLS EXT-X-BYTERANGE slice): never
    # overwrite it with a resume Range (that would fetch the parent's tail, not the slice);
    # retries rewrite the whole slice in "wb" mode instead
    item_range = has_range_header(kwargs)

    attempts = 1
    written = 0
    while True:
        if claimed is not None and claimed():
            return
        if DOWNLOAD_CANCELLED.is_set() or (abort is not None and abort.is_set()):
            # a worker waking from a retry nap after cancel/batch-abort must exit here,
            # before opening a new request or the .!dev handle mid-teardown
            return
        if not part_mode:
            written = 0
        last_speed_refresh = _time()

        try:
            use_rnet = is_rnet_session(session)

            request_kwargs = dict(kwargs)
            if use_rnet:
                request_kwargs.setdefault("read_timeout", READ_TIMEOUT)
                # own segment retries here, suppressing RnetSession's nested retry loop
                request_kwargs.setdefault("max_retries", 0)
            else:
                request_kwargs.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
            req_headers = dict(request_kwargs.get("headers", {}) or {})
            # media bytes must arrive unrecoded: transparent CDN (de)compression breaks
            # Content-Length accounting and is meaningless on ranged requests
            if not any(str(k).lower() == "accept-encoding" for k in req_headers):
                req_headers["Accept-Encoding"] = "identity"
            if part_mode:
                req_headers["Range"] = f"bytes={part_offset + written}-{part_end}"
            elif resume_offset > 0 and not item_range:
                req_headers["Range"] = f"bytes={resume_offset}-"
            request_kwargs["headers"] = req_headers

            stream = session.get(url, stream=True, **request_kwargs)

            if (not part_mode) and (not item_range) and resume_offset > 0 and stream.status_code == 416:
                # our Range started past the end (a stale or oversized .!dev from a prior run).
                # discard it and restart clean rather than raise_for_status → burn retries.
                try:
                    stream.close()
                except Exception:
                    pass
                tmp_file.unlink(missing_ok=True)
                resume_offset = 0
                written = 0
                continue

            stream.raise_for_status()

            # item_range 206s reflect the slice's own Range, not a resume; those retries rewrite in "wb"
            resumed = (not part_mode) and (not item_range) and resume_offset > 0 and stream.status_code == 206
            if resumed:
                content_range = parse_content_range(
                    stream.headers.get("Content-Range") or stream.headers.get("content-range")
                )
                if content_range is None or content_range[0] != resume_offset:
                    # some servers answer `bytes=X-` with a 206 for the whole resource;
                    # appending that body would misplace every byte after the partial. A
                    # proven full-resource-from-zero body is byte-identical to a 200, so
                    # keep the response and rewrite from scratch; anything else (alien
                    # start, prefix slice, no provable placement) restarts clean like a 416
                    start, end, total = content_range or (None, None, None)
                    if start == 0 and total is not None and end == total - 1:
                        resumed = False
                        resume_offset = 0
                    else:
                        try:
                            stream.close()
                        except Exception:
                            pass
                        tmp_file.unlink(missing_ok=True)
                        resume_offset = 0
                        written = 0
                        continue
            if (not part_mode) and resume_offset > 0 and not resumed:
                resume_offset = 0
            if part_mode and part_offset is not None and part_end is not None:  # spelled so mypy narrows
                if stream.status_code != 206:
                    raise IOError(f"expected 206 for ranged part, got {stream.status_code}")
                content_range = parse_content_range(
                    stream.headers.get("Content-Range") or stream.headers.get("content-range")
                )
                if content_range is None or content_range[0] != part_offset + written or content_range[1] > part_end:
                    # a wrong start would land bytes at the wrong offset in the shared file,
                    # and an end past part_end would spill into a sibling part's region; fail
                    # the attempt before any byte is written
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

            if item_range and not part_mode and stream.status_code != 206:
                # server ignored the slice's Range and sent the whole parent (RFC 9110 allows
                # a 200 here); writing that as the segment would silently corrupt the merge, so
                # fail the attempt unless the slice spans the whole resource, in which case the
                # 200 body is byte-identical to the 206 and safe to keep
                if not (stream.status_code == 200 and range_covers_full_body(req_headers, content_length)):
                    raise IOError(f"expected 206 for byte-range segment, got {stream.status_code}")

            if item_range and not part_mode and stream.status_code == 206:
                # a 206 for a different range than the slice asked for (e.g. the whole parent,
                # or the short range a size-capping CDN chose to serve) would be written as the
                # whole segment and silently corrupt the merge
                slice_range = request_range(req_headers)
                if slice_range is not None:
                    slice_start, slice_end = slice_range
                    content_range = parse_content_range(
                        stream.headers.get("Content-Range") or stream.headers.get("content-range")
                    )
                    if (
                        content_range is None
                        or content_range[0] != slice_start
                        or (slice_end is not None and content_range[1] != slice_end)
                    ):
                        raise IOError(
                            f"byte-range segment got Content-Range {content_range!r}, "
                            f"expected {slice_start}-{slice_end if slice_end is not None else ''}"
                        )

            if resumed and content_encoded:
                try:
                    stream.close()
                except Exception:
                    pass
                tmp_file.unlink(missing_ok=True)
                resume_offset = 0
                continue

            limiter = speed_limiter
            chunk_size = adaptive_chunk_size(content_length)
            if limiter:
                chunk_size = min(chunk_size, max(8192, int(limiter.rate / 4)))
            total_size = (resume_offset + content_length) if resumed and content_length > 0 else content_length

            if not segmented and not part_mode:
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
            # non-part downloads write sequentially and must NOT pre-allocate: the resume
            # offset is derived from the tmp file's size, so preallocating to content_length
            # would make an interrupted write look fully downloaded and poison the resume
            # (unsatisfiable Range on retry, silent zero-padded corruption). part_mode still
            # pre-truncates in dispatch_parts because its workers seek to fixed offsets.
            with open(save_path if part_mode else tmp_file, file_mode, buffering=file_buffering) as f:
                if part_mode:
                    f.seek(part_offset + written)

                _write = f.write

                if use_rnet:
                    chunks = stream.stream()
                elif use_raw and not content_encoded:
                    _read1 = getattr(stream.raw, "read1", None) if claimed is not None else None
                    if _read1 is not None and racing is not None:
                        # only pay the per-arrival read1 tax while this segment is actually
                        # racing; a blocking full-chunk read otherwise (the common case)
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
                for chunk in chunks:
                    if DOWNLOAD_CANCELLED.is_set() or (abort is not None and abort.is_set()):
                        break
                    if claimed is not None and claimed():
                        # close the handle or Windows can't delete the stray .!dev at merge (WinError 32)
                        if register is not None:
                            register(stream, False)
                        try:
                            stream.close()
                        except Exception:
                            pass
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

                if register is not None:
                    register(stream, False)
                try:
                    stream.close()
                except Exception:
                    pass

            aborted = abort is not None and abort.is_set()
            if (DOWNLOAD_CANCELLED.is_set() or aborted) and (not content_length or written < content_length):
                # cancelled/aborted mid-stream: keep the partial for resume, never finalize,
                # and skip the size checks below (a short read here is expected, not an error)
                return

            if part_mode:
                expected = part_end - part_offset + 1
                if written < expected:
                    raise IOError(f"Failed to read part {part_offset}-{part_end}: got {written}/{expected}")
            elif content_length and written < content_length:
                # applies to segments too: a connection FIN'd mid-body is a clean EOF, so the
                # chunk loop ends without raising, so without this check a truncated segment would
                # finalize as complete and corrupt the merge. content_length==0 (chunked/gzip)
                # is unknown-length and correctly skips the check.
                raise IOError(f"Failed to read {content_length} bytes from the track URI.")

            if not part_mode:
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
            # a superseded loser's error must not retry or kill the batch
            if claimed is not None and claimed():
                return
            cancelled = DOWNLOAD_CANCELLED.is_set() or (abort is not None and abort.is_set())
            if cancelled or attempts == MAX_ATTEMPTS:
                # cancel/abort is not an error, but a genuine retry-exhaustion is, so surface it
                # so the caller fails fast instead of hitting a missing segment at merge time
                if not cancelled:
                    raise
                return
            if on_retry is not None:
                try:
                    on_retry(exc)
                except Exception:
                    pass
            if not part_mode:
                resume_offset = tmp_file.stat().st_size if tmp_file.exists() else 0
            delay = retry_sleep(exc, attempts)
            if abort is not None:
                # interruptible nap: backoff can reach MAX_BACKOFF, and teardown always
                # sets the batch abort, so a parked worker must wake and exit at once
                # instead of stalling shutdown (or the merge) for the full delay
                if abort.wait(delay):
                    return
            else:
                # no batch abort on this path, but a cross-track cancel must still wake a
                # parked worker rather than stall teardown for the full backoff
                if DOWNLOAD_CANCELLED.wait(delay):
                    return
            attempts += 1


def build_session_spec(session: Optional[Any]) -> Optional[dict[str, Any]]:
    """Picklable spec to rebuild ``session`` in a child process. Gives None when it is not cheaply rebuildable.

    Live sessions (sockets, TLS state, threads) cannot cross a process boundary, so each child
    reconstructs its own from cheap state. RnetSession needs a resolvable impersonate preset.
    Without one it is not cheaply rebuildable, and the caller falls back to a single process.
    """
    if session is None:
        return {"kind": "none"}
    if is_requests_session(session):
        return {
            "kind": "requests",
            "headers": dict(session.headers),
            "cookies": session.cookies,  # RequestsCookieJar pickles cleanly
            "proxies": dict(session.proxies),
        }
    if is_rnet_session(session):
        name = session.impersonate_name
        if not name:
            return None
        return {
            "kind": "rnet",
            "impersonate": name,
            "headers": dict(session.headers),
            "cookies": session.cookies.get_dict_by_domain(),
            "proxy": session.proxies.get("all") or session.proxies.get("https") or session.proxies.get("http"),
        }
    return None


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
        adapter = HTTPAdapter(pool_connections=max_workers, pool_maxsize=max_workers, pool_block=True)
        rs.mount("https://", adapter)
        rs.mount("http://", adapter)
        return rs
    if kind == "rnet":
        from unshackle.core.session import session as make_session

        ns = make_session(spec["impersonate"])
        if spec.get("headers"):
            ns.headers.update(spec["headers"])
        for domain, jar in spec.get("cookies", {}).items():
            if domain:
                for name, value in jar.items():
                    ns.cookies.set(name, value, domain=domain)
            else:
                # no domain to scope to; update() reproduces the parent's localhost-scoped state
                ns.cookies.update(jar)
        if spec.get("proxy"):
            ns.proxies.update({"all": spec["proxy"]})
        return ns
    return None  # "none": child builds its own Session from the passed headers/cookies/proxy


def mp_worker(queue: Any, kwargs: dict[str, Any]) -> None:
    """Child entry point (top-level so ``spawn`` can pickle it).

    Rebuilds the HTTP session, iterates ``requests()`` for its url slice, and relays every event
    over the shared queue. Errors travel as a ``__mp_error__`` sentinel. This child always sends a
    ``__mp_done__`` sentinel last, so the parent knows that this child is complete.
    """
    try:
        # spawn re-imports this module, dropping any timing constants the bench patched in
        # the parent (--fast-timeouts); the bench relays them via env as "NAME=SECONDS,..."
        overrides = os.environ.get("UNSHACKLE_DL_TIMING_OVERRIDES")
        if overrides:
            for pair in overrides.split(","):
                name, _, value = pair.partition("=")
                # only the timing constants the bench relays; a stale/foreign env var
                # must not be able to rewrite arbitrary module globals in every child
                if name in ("READ_TIMEOUT", "RETRY_WAIT", "HEDGE_MIN_WAIT"):
                    setattr(sys.modules[__name__], name, float(value))
        spec = kwargs.pop("_session_spec")
        kwargs["session"] = rebuild_session(spec, int(kwargs.get("max_workers") or 1))
        for event in requests(**kwargs):
            queue.put(event)
    except Exception:
        queue.put({"__mp_error__": traceback.format_exc()})
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
    # only the "none" spec relies on headers/cookies/proxy pass-through; a rebuilt session
    # already carries them and requests() ignores these args when a session is provided
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
                # cross-track cancel (e.g. a sibling track failed): spawned children carry
                # their own fresh flag and can't see it, so terminate them like KeyboardInterrupt
                for p in procs:
                    p.terminate()
                yield dict(downloaded="[yellow]CANCELLED")
                return
            try:
                event = queue.get(timeout=0.1)
            except Empty:
                event = None
                # a child that dies without its __mp_done__ sentinel (hard crash, OOM/AV kill)
                # would leave this loop waiting forever. nonzero exitcode means its finally never
                # ran; a few empty ticks of grace let any feeder-flushed messages arrive first
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
                    raise RuntimeError(f"segment download child failed:\n{event['__mp_error__']}")
                if "total" in event:
                    continue
                if "downloaded" in event:
                    continue
                if "advance" in event:
                    # children report mixed granularity (segment counts, or raw bytes when a
                    # length-1 strided chunk routes through the single-file path); the parent
                    # bar counts segments, so advance=1 is emitted per file_downloaded instead
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
                # terminate is async: a dying child can still hold segment handles for a
                # beat, so wait for the kill to land before the caller cleans up (WinError 32)
                p.join(timeout=5)


def requests(
    urls: Union[str, list[str], dict[str, Any], list[dict[str, Any]]],
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

    Yields these download status updates while the chunks download:

    - {total: 123} (there are 123 chunks to download)
    - {total: None} (there are an unknown number of chunks to download)
    - {advance: 1} (the downloader finished one chunk)
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
        max_workers = min(16, (os.cpu_count() or 1) + 4)

    # A spawned child re-imports this module and would start with no TokenBucket, so the
    # configured cap would silently stop applying; the cap wins over the fan-out, matching
    # the speed_limiter gates on the ranged-parallel paths.
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

    # Process fan-out: split a large segment batch across spawned children. Single-URL and small
    # batches keep the in-process path (a lone file is already parallelized by ranged parts).
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
            return
        if debug_logger:
            debug_logger.log(
                level="DEBUG",
                operation="downloader_mp_fallback",
                message="Session not rebuildable across processes; using a single process",
                context={"session_type": type(session).__name__, "url_count": len(urls)},
            )

    # only resolve each URL's extension when the template actually references {ext};
    # DASH/ISM use "{i}.mp4" (no ext), so this skips a urlparse per segment for them
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
    # once per batch; download() skips it for segments
    output_dir.mkdir(parents=True, exist_ok=True)

    # Provided sessions may be shared across tracks: don't mutate or remount them. Remounting
    # drops the service's 429/5xx Retry config and races get_adapter() in other track threads.
    if session is None:
        session = Session()
        if headers:
            headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
            session.headers.update(headers)
        if cookies:
            session.cookies.update(cookies)
        if proxy:
            session.proxies.update({"all": proxy})
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
            if max_workers > 1 and speed_limiter is None and not has_range_header(url_item):
                total_size, supports_ranges = probe_ranged(url_item["url"], session)
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
                    except Exception:
                        save_path = url_item.get("save_path")
                        if save_path:
                            sp = Path(save_path)
                            for stray in (sp, sp.with_name(f"{sp.name}.!dev")):
                                try:
                                    stray.unlink(missing_ok=True)
                                except OSError:
                                    pass
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
        hedge_median = 0.0  # cached; recomputed only when seg_durations grows
        hedge_median_len = 0

        pool = ThreadPoolExecutor(max_workers=max_workers)
        event_queue: Queue[dict[str, Any]] = Queue()

        # hedging: first finisher claims the segment in seg_done under the lock,
        # the loser discards its own output and emits nothing
        seg_lock = threading.Lock()
        seg_start: dict[int, float] = {}
        seg_done: set[int] = set()
        seg_durations: list[float] = []
        hedged: set[int] = set()
        # batch-local cancel, set at teardown: a worker leaked past a no-wait shutdown must
        # never retry once DOWNLOAD_CANCELLED is cleared for the next track (it would reopen
        # its .!dev mid-cleanup -> WinError 32)
        batch_abort = threading.Event()

        # streams currently being read, so teardown can unblock a superseded loser parked in a
        # slow read instead of the wait-gated shutdown draining it (see force_unblock_stream)
        active_streams: set = set()
        active_lock = threading.Lock()

        def register_stream(stream: Any, active: bool) -> None:
            with active_lock:
                if active:
                    active_streams.add(stream)
                else:
                    active_streams.discard(stream)

        # start at the cap and adapt downward under CDN pressure: ramping up from a low target
        # can at best converge to the throughput the cap already gives, so there is nothing to
        # gain by starting low. error-burst backoff and AIMD recovery still apply from there.
        controller = AdaptiveWorkerController(cap=max_workers, start=max_workers) if adaptive else None
        # feed CDN error signals (429/timeout/reset) from each retrying segment into the controller
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
            # each racer writes to its own target so a loser never touches the final file;
            # the .!dev suffix keeps strays visible to the manifest parsers' cleanup
            target = save_path.with_name(f"{save_path.name}.{'h' if hedge else 'p'}.!dev")
            if not hedge:
                # under seg_lock: the hedge scan iterates seg_start.items() on the main
                # thread, and a first-time key insert during that iteration raises
                with seg_lock:
                    seg_start[index] = time.time()
            for event in download(
                session=session,
                segmented=segmented_batch,
                save_path=target,
                claimed=lambda: index in seg_done,
                # read1 only while racing: a hedge racer always races; a primary races once
                # it has been hedged. `hedged` is read here without seg_lock; CPython set
                # membership is atomic and a one-iteration stale read is harmless.
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

        # not-yet-submitted segments; adaptive mode meters submission against the controller
        # target, while fixed mode (adaptive=False) submits every segment upfront
        remaining: deque[tuple[int, dict[str, Any]]] = deque(enumerate(urls))
        pending: set = set()

        # tail boost bookkeeping (adaptive only): indices whose parts are in flight vs
        # indices probed and left for the normal single-worker path, each decided once
        tail_boosted: set[int] = set()
        tail_skipped: set[int] = set()

        def submit(count: int, only: Optional[set[int]] = None) -> None:
            # only=<set> submits just those indices (used in the tail window to release
            # segments the boost declined while holding back boost candidates); with no
            # `only`, submits the leading `count` segments in FIFO order.
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
            try:
                # part_mode download writes [start, end] into the pre-truncated target and
                # emits only byte-`advance`; swallow those, since tail segments report at segment
                # granularity (advance=1) like every other segment, once on finalize below
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
            except BaseException:
                abort.set()  # stop sibling parts of this segment; a real failure fails the batch
                raise
            with part_lock:
                parts_left[0] -= 1
                finalize = parts_left[0] == 0
            if not finalize:
                return
            if DOWNLOAD_CANCELLED.is_set() or abort.is_set():
                return  # cancelled/aborted: keep the partial target for the stray sweep, don't finalize
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
            # Tail boost: near the end, split the few not-yet-started segments across idle
            # workers with intra-segment range parts so aggregate throughput doesn't collapse
            # to (few segments)x(per-connection speed). Only not-yet-started segments in
            # `remaining` are boosted (never mid-flight ones) to avoid entangling the main
            # loop; gated behind adaptive, though it could be decoupled from the controller.
            # Engage on idle capacity so the boost fires despite `remaining` draining in strides
            # of the worker target; probe at most TAIL_BOOST_MAX_PER_CYCLE per cycle so a run of
            # blocking range probes can't stall the drain loop.
            if DOWNLOAD_CANCELLED.is_set() or not tail_boost_engages(len(remaining), len(pending), target):
                return
            # completed segments predict the tail's sizes: below the min every probe would
            # decline anyway, and at real-CDN RTT those sequential probes cost seconds on the
            # main loop, so skip probing and release the tail through the normal path
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
                if has_range_header(item):  # byte-range slice; part-mode would clobber its Range
                    tail_skipped.add(index)
                    continue
                req_kwargs = {k: v for k, v in item.items() if k != "url"}
                size, supports_ranges = probe_ranged(item["url"], session, **req_kwargs)
                parts = plan_tail_parts(size, spare) if supports_ranges else []
                if not parts:
                    tail_skipped.add(index)  # no ranges / too small / no capacity -> normal path
                    continue
                try:
                    remaining.remove((index, url_item))
                except ValueError:
                    tail_skipped.add(index)
                    continue
                # distinct .tp.!dev target so an interrupted boost never collides with the
                # normal worker's .p.!dev; the .!dev suffix keeps the stray visible to cleanup
                part_target = save_path.with_name(f"{save_path.name}.tp.!dev")
                with open(part_target, "wb") as f:
                    f.truncate(size)
                seg_start[index] = time.time()
                tail_boosted.add(index)
                # a failed part fails the whole batch anyway, so parts share batch_abort
                abort = batch_abort
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

        # submitting every segment up front convoys on the pool's global lock and starves the
        # event drain, so fixed mode meters submission through a window the loop tops up
        queue_depth = max_workers * 2

        if controller:
            submit(controller.update(time.time()))
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

                now = time.time()

                target = max_workers
                if controller:
                    target = controller.update(now, len(pending) + len(remaining))
                    maybe_tail_boost(target)
                    if len(pending) < target:
                        # In the final stride (unstarted segments <= one worker target), hold back
                        # boost candidates so idle workers accumulate for a range-split; only
                        # release segments the boost already declined (too small / no range) so
                        # nothing stalls. Otherwise top up in FIFO order as usual.
                        if remaining and len(remaining) <= target:
                            submit(target - len(pending), only=tail_skipped)
                        else:
                            submit(target - len(pending))
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

                # hedge stuck segments once spare workers exist within the active target;
                # throttled to ~0.5s and median recomputed only when seg_durations grows
                if (
                    len(pending) < target
                    and seg_durations
                    and speed_limiter is None
                    and now - last_hedge_check > 0.5
                    and not DOWNLOAD_CANCELLED.is_set()
                ):
                    last_hedge_check = now
                    cur_len = len(seg_durations)  # atomic read; appended under seg_lock by workers
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
                            and i not in tail_boosted  # boosted segments are already range-split
                            and now - started > threshold
                        ]
                    for i in stuck[: target - len(pending)]:
                        hedged.add(i)
                        pending.add(pool.submit(download_worker, i, urls[i], True))

                # all segments claimed; superseded losers exit via their claimed() check
                if len(seg_done) == len(urls):
                    break

                completed, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in completed:
                    exc = future.exception()
                    if isinstance(exc, KeyboardInterrupt):
                        raise KeyboardInterrupt()
                    elif exc:
                        DOWNLOAD_CANCELLED.set()
                        yield dict(downloaded="[red]FAILING")
                        pool.shutdown(wait=False, cancel_futures=True)
                        yield dict(downloaded="[red]FAILED")
                        if debug_logger:
                            debug_logger.log(
                                level="ERROR",
                                operation="downloader_failed",
                                message=f"Download failed: {exc}",
                                error=exc,
                                context={
                                    "url_count": len(urls),
                                    "output_dir": str(output_dir),
                                },
                            )
                        raise exc
        except KeyboardInterrupt:
            DOWNLOAD_CANCELLED.set()
            yield dict(downloaded="[yellow]CANCELLING")
            pool.shutdown(wait=False, cancel_futures=True)
            yield dict(downloaded="[yellow]CANCELLED")
            raise
        finally:
            # losers must close their handles before merge sweeps *.!dev (WinError 32);
            # no wait on cancel/fail: the merge below never runs and workers may be blocked
            # in reads. The caller's own cleanup does still sweep on failure, so a worker
            # parked in a read can briefly hold a handle there (Windows).
            # batch_abort outlives the shutdown so a leaked worker exits at its next
            # arrival/timeout instead of retrying after the global flag is cleared
            batch_abort.set()
            # batch_abort is set, so a loser whose read we unblock here sees it and exits without
            # retrying; unblock only on the success path (the wait below still guarantees handles
            # are released before merge sweeps). rnet streams have no reachable socket -> no-op.
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
