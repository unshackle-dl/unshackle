import math
import os
import statistics
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, wait
from concurrent.futures.thread import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Generator, MutableMapping, Optional, Union

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

# Adaptive chunk sizing — benchmarked optimal range
MIN_CHUNK = 524_288  # 512KB
MAX_CHUNK = 4_194_304  # 4MB
DEFAULT_CHUNK = 524_288  # 512KB
SPEED_ROLLING_WINDOW = 10  # seconds of history to keep for speed calculation

RANGE_PARALLEL_MIN_SIZE = 64 * 1024 * 1024
RANGE_PARALLEL_PART_SIZE = 16 * 1024 * 1024

# Tail-end boost (opt-in, adaptive only): when only a few segments remain and workers
# would otherwise idle, split each remaining segment into intra-segment range parts so
# aggregate throughput holds to the end instead of collapsing to (few)x(per-connection).
TAIL_BOOST_MAX_PER_CYCLE = 4  # cap on segments probed/split per drain cycle so blocking probes can't stall the loop
TAIL_BOOST_MIN_SEGMENT_SIZE = 8 * 1024 * 1024  # don't probe+split segments smaller than this
TAIL_BOOST_PART_SIZE = 4 * 1024 * 1024  # target size per tail part (smaller than the whole-file part size)


def _adaptive_chunk_size(content_length: int) -> int:
    """Pick chunk size based on content length. Benchmarked sweet spot: 512KB-4MB."""
    if content_length <= 0:
        return DEFAULT_CHUNK
    return min(MAX_CHUNK, max(MIN_CHUNK, content_length // 4))


# Adaptive worker controller (opt-in): AIMD hill-climb over segment concurrency
ADAPTIVE_TICK = 4.0  # seconds between target re-evaluations
ADAPTIVE_START = 6  # initial worker target, clamped to cap
ADAPTIVE_STEP = 2  # additive increase per tick
ADAPTIVE_MIN = 2  # never drop below this many workers
ADAPTIVE_ERROR_BURST = 3  # errors within a tick that trigger multiplicative decrease
ADAPTIVE_PLATEAU_GAIN = 1.10  # min speed ratio to justify keeping an increase


class AdaptiveWorkerController:
    """CDN-aware segment concurrency governor.

    Pure and synchronous (no threads or sockets), so the AIMD policy is unit-testable.
    ``update`` is evaluated once per ``tick`` and only after a full ``window`` of
    throughput samples exists: it ramps the worker target up while the CDN keeps
    rewarding extra concurrency, reverts the last increase on a plateau, and halves
    (with a one-tick cooldown) on an error burst. Target is always clamped to
    ``[ADAPTIVE_MIN, cap]``.
    """

    def __init__(
        self, cap: int, start: Optional[int] = None, tick: float = ADAPTIVE_TICK, window: float = SPEED_ROLLING_WINDOW
    ) -> None:
        self.cap = max(ADAPTIVE_MIN, cap)
        base = ADAPTIVE_START if start is None else start
        self.target = max(ADAPTIVE_MIN, min(base, self.cap))
        self.tick = tick
        self.window = window
        self._samples: deque[tuple[float, int]] = deque()
        self._first_sample: Optional[float] = None  # first-ever sample time (survives pruning)
        self._errors = 0  # errors observed since the last tick evaluation
        self._last_tick: Optional[float] = None
        self._last_action: Optional[str] = None  # "increase" once we have probed upward
        self._speed_before_increase = 0.0
        self._cooldown = False

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def record_bytes(self, n: int, now: float) -> None:
        if self._first_sample is None:
            self._first_sample = now
        self._samples.append((now, n))
        self._prune(now)

    def record_error(self, now: float) -> None:
        self._errors += 1

    def speed(self, now: float) -> float:
        """Rolling bytes/sec over the window."""
        self._prune(now)
        if not self._samples:
            return 0.0
        span = now - self._samples[0][0]
        if span <= 0:
            return 0.0
        return sum(n for _, n in self._samples) / span

    def _full_window(self, now: float) -> bool:
        # true once we have been observing for at least a full window; measured from the
        # first-ever sample so continuous pruning (which keeps span just under window) does
        # not perpetually starve the controller of a "full" window in real timing
        return self._first_sample is not None and (now - self._first_sample) >= self.window

    def update(self, now: float, inflight_plus_remaining: Optional[int] = None) -> int:
        """Evaluate the policy at most once per tick; return the current target.

        ``inflight_plus_remaining`` (when given) is the count of segments still in flight or
        not yet started. If it is below the target there is not enough work to saturate the
        target, so the low measured speed reflects starvation rather than a plateau, and the
        probe/revert step is skipped this tick. Error-burst decreases still apply. Default ``None`` keeps the
        original behaviour for existing callers.
        """
        if self._last_tick is None:
            self._last_tick = now
            return self.target
        if now - self._last_tick < self.tick:
            return self.target
        self._last_tick = now

        # hold until a full window of throughput history has accumulated
        if not self._full_window(now):
            self._errors = 0
            return self.target

        errors = self._errors
        self._errors = 0
        speed_now = self.speed(now)
        old = self.target

        if self._cooldown:
            # one tick of quiet after a decrease before probing again
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
            # last probe upward did not pay off, so revert it and hold
            self.target = max(ADAPTIVE_MIN, self.target - ADAPTIVE_STEP)
            self._last_action = None
            reason = "plateau"
        elif self.target < self.cap:
            self._speed_before_increase = speed_now
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


def _is_requests_session(session: Any) -> bool:
    """Check if the session is a standard requests.Session (supports resp.raw)."""
    return isinstance(session, Session)


def _is_rnet_session(session: Any) -> bool:
    """Check if the session is an RnetSession (uses resp.stream())."""
    from unshackle.core.session import RnetSession

    return isinstance(session, RnetSession)


def _probe_ranged(url: str, session: Any, **kwargs: Any) -> tuple[int, bool]:
    headers = {**(kwargs.get("headers") or {}), "Range": "bytes=0-0"}
    rest = {k: v for k, v in kwargs.items() if k != "headers"}
    if _is_rnet_session(session):
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
        ce = (resp.headers.get("Content-Encoding") or resp.headers.get("content-encoding") or "").lower()
        if ce in ("gzip", "deflate", "br"):
            return 0, False
        content_range = resp.headers.get("Content-Range") or resp.headers.get("content-range") or ""
        total = content_range.rsplit("/", 1)[-1].strip()
        return (int(total), True) if total.isdigit() else (0, False)
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _has_range_header(item: dict[str, Any]) -> bool:
    """True when a URL item's per-request headers already carry a Range.

    Such an item is itself a byte-range slice of a larger resource (DASH SegmentBase,
    HLS EXT-X-BYTERANGE); probing or part-mode would clobber that Range and fetch the
    wrong bytes, so ranged-parallel strategies must skip these items entirely.
    """
    return any(k.lower() == "range" for k in (item.get("headers") or {}))


def _plan_tail_parts(size: int, spare: int) -> list[tuple[int, int]]:
    """Plan intra-segment range parts for a tail segment.

    Returns inclusive ``[start, end]`` byte ranges covering ``[0, size)`` with no gaps or
    overlaps, split into at most ``spare`` parts of ~``TAIL_BOOST_PART_SIZE``. Returns an
    empty list when the segment is smaller than ``TAIL_BOOST_MIN_SEGMENT_SIZE`` (not worth a
    probe+split) or there are fewer than two spare workers. Pure, with no I/O, so testable.
    """
    if size < TAIL_BOOST_MIN_SEGMENT_SIZE or spare < 2:
        return []
    n_parts = max(1, min(spare, math.ceil(size / TAIL_BOOST_PART_SIZE)))
    part_size = math.ceil(size / n_parts)
    return [
        (s, e)
        for i in range(n_parts)
        for s, e in [(i * part_size, min(size - 1, (i + 1) * part_size - 1))]
        if s <= e
    ]


def _tail_boost_engages(remaining: int, pending: int, target: int) -> bool:
    """True when idle workers outnumber the remaining whole segments.

    That is the tail condition where running one worker per remaining segment would leave
    workers idle, so splitting those segments into range parts keeps throughput up. Gating on
    idle capacity (not a fixed segment count) is what lets the boost fire despite ``remaining``
    draining in strides of the worker target. Pure, so the gate is unit-testable.
    """
    spare = target - pending
    return remaining > 0 and spare >= 2 and remaining <= spare


def _dispatch_parts(
    url: str,
    save_path: Path,
    session: Any,
    total_size: int,
    max_workers: int,
    **kwargs: Any,
) -> Generator[dict[str, Any], None, None]:
    save_path.parent.mkdir(parents=True, exist_ok=True)
    control_file = save_path.with_name(f"{save_path.name}.!dev")

    n_parts = max(1, min(max_workers, math.ceil(total_size / RANGE_PARALLEL_PART_SIZE)))
    part_size = math.ceil(total_size / n_parts)
    parts = [
        (s, e)
        for i in range(n_parts)
        for s, e in [(i * part_size, min(total_size - 1, (i + 1) * part_size - 1))]
        if s <= e
    ]

    control_file.write_bytes(b"")
    with open(save_path, "wb") as f:
        f.truncate(total_size)

    events: Queue[dict[str, Any]] = Queue()
    # local to this ranged download: a failed part must not poison the process-global
    # DOWNLOAD_CANCELLED (which would kill the sequential fallback and sibling tracks)
    abort = threading.Event()

    def _worker(start: int, end: int) -> None:
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
    futures = [pool.submit(_worker, s, e) for s, e in parts]
    pending = set(futures)

    yield {"total": total_size}

    total_bytes = 0
    start_time = last_report = time.time()
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
                yield {"downloaded": f"{filesize.decimal(math.ceil(total_bytes / (now - start_time)))}/s"}
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

        yield {"file_downloaded": save_path, "written": total_size}
        completed = True
    except KeyboardInterrupt:
        DOWNLOAD_CANCELLED.set()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
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
    abort: Optional[threading.Event] = None,
    on_retry: Optional[Callable[[Exception], None]] = None,
    **kwargs: Any,
) -> Generator[dict[str, Any], None, None]:
    """
    Download a file with optimized I/O.

    Supports both requests.Session and RnetSession for TLS fingerprinting.
    Uses raw socket reads for requests.Session and native rnet streaming for RnetSession.

    Yields the following download status updates while chunks are downloading:

    - {total: 123} (there are 123 chunks to download)
    - {total: None} (there are an unknown number of chunks to download)
    - {advance: 1} (one chunk was downloaded)
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
            (with `part_end`), enables part mode for parallel ranged downloads —
            no truncate, no skip-if-exists, no control file; emits only `advance`
            events; retries resume mid-part via Range.
        part_end: Inclusive end byte of the part. Required when `part_offset` is set.
        claimed: Optional predicate checked before every attempt; when it returns True
            the download stops silently (another worker already delivered this file).
        abort: Optional local cancel signal. When set, the download stops like a cancel
            (keeps its partial, returns silently, does not raise). Used by ranged-parallel
            downloads to abort sibling parts without touching the process-global cancel.
        on_retry: Optional callback invoked with the raised exception before each retry
            wait (not on cancel or final exhaustion). Used to feed CDN error signals to
            the adaptive worker controller. Callback errors are swallowed.
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
    use_raw = _is_requests_session(session)

    attempts = 1
    written = 0
    while True:
        if claimed is not None and claimed():
            return
        if not part_mode:
            written = 0
        last_speed_refresh = _time()

        try:
            use_rnet = _is_rnet_session(session)

            request_kwargs = dict(kwargs)
            if use_rnet:
                request_kwargs.setdefault("read_timeout", READ_TIMEOUT)
                # own segment retries here, suppressing RnetSession's nested retry loop
                request_kwargs.setdefault("max_retries", 0)
            else:
                request_kwargs.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
            if part_mode:
                req_headers = dict(request_kwargs.get("headers", {}) or {})
                req_headers["Range"] = f"bytes={part_offset + written}-{part_end}"
                request_kwargs["headers"] = req_headers
            elif resume_offset > 0:
                req_headers = dict(request_kwargs.get("headers", {}) or {})
                req_headers["Range"] = f"bytes={resume_offset}-"
                request_kwargs["headers"] = req_headers

            stream = session.get(url, stream=True, **request_kwargs)

            if (not part_mode) and resume_offset > 0 and stream.status_code == 416:
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

            resumed = (not part_mode) and resume_offset > 0 and stream.status_code == 206
            if (not part_mode) and resume_offset > 0 and not resumed:
                resume_offset = 0
            if part_mode and stream.status_code != 206:
                raise IOError(f"expected 206 for ranged part, got {stream.status_code}")
            if use_rnet:
                content_length = stream.content_length or 0
            else:
                try:
                    content_length = int(stream.headers.get("Content-Length", "0"))
                    if stream.headers.get("Content-Encoding", "").lower() in ["gzip", "deflate", "br"]:
                        content_length = 0
                except ValueError:
                    content_length = 0

            chunk_size = _adaptive_chunk_size(content_length)
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
            # pre-truncates in _dispatch_parts because its workers seek to fixed offsets.
            with open(save_path if part_mode else tmp_file, file_mode, buffering=file_buffering) as f:
                if part_mode:
                    f.seek(part_offset + written)

                _write = f.write

                if use_rnet:
                    chunks = stream.stream()
                elif use_raw:
                    chunks = iter(lambda: stream.raw.read(chunk_size), b"")
                else:
                    chunks = stream.iter_content(chunk_size=chunk_size)

                _data_accumulated = 0
                _bytes_since_yield = 0
                emit_progress = (not segmented) or part_mode
                for chunk in chunks:
                    if DOWNLOAD_CANCELLED.is_set() or (abort is not None and abort.is_set()):
                        break
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
            cancelled = DOWNLOAD_CANCELLED.is_set() or (abort is not None and abort.is_set())
            if cancelled or attempts == MAX_ATTEMPTS:
                # a hedged loser whose file was already delivered must not kill the batch
                if claimed is not None and claimed():
                    return
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
            time.sleep(RETRY_WAIT)
            attempts += 1


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
) -> Generator[dict[str, Any], None, None]:
    """
    Download files with optimized I/O and adaptive chunk sizing.

    Supports both requests.Session and RnetSession. When a RnetSession is
    provided (e.g. from a service's get_session()), TLS fingerprinting is preserved
    on all segment downloads.

    Yields the following download status updates while chunks are downloading:

    - {total: 123} (there are 123 chunks to download)
    - {total: None} (there are an unknown number of chunks to download)
    - {advance: 1} (one chunk was downloaded)
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
        cookies: A mapping of Cookie Key/Values or a Cookie Jar to use for all downloads.
        proxy: An optional proxy URI to route connections through for all downloads.
        max_workers: The maximum amount of threads to use for downloads. Defaults to
            min(16, cpu_count + 4).
        session: An optional requests.Session or RnetSession to use. If provided,
            it will be used directly (preserving TLS fingerprinting). If None, a new
            requests.Session with HTTPAdapter connection pooling will be created.
        adaptive: Opt-in CDN-aware dynamic segment concurrency for segmented (multi-URL)
            downloads. When True the per-track worker count starts moderate and ramps up
            or backs off based on measured throughput and CDN errors, capped at
            max_workers. When False (default) all segments are submitted upfront using a
            fixed worker count, matching the non-adaptive behaviour exactly.
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

    debug_logger = get_debug_logger()

    if not isinstance(urls, list):
        urls = [urls]

    if not max_workers:
        max_workers = min(16, (os.cpu_count() or 1) + 4)

    urls = [
        dict(save_path=save_path, **url) if isinstance(url, dict) else dict(url=url, save_path=save_path)
        for i, url in enumerate(urls)
        for save_path in [
            output_dir / filename.format(i=i, ext=get_extension(url["url"] if isinstance(url, dict) else url))
        ]
    ]
    # once per batch; download() skips it for segments
    output_dir.mkdir(parents=True, exist_ok=True)

    # Use provided session or create a new optimized requests.Session
    # When a session is provided (e.g., service's RnetSession), don't mutate headers/cookies/proxy —
    # they're already set and the session may be shared across tracks.
    if session is None:
        session = Session()
        if headers:
            headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
            session.headers.update(headers)
        if cookies:
            session.cookies.update(cookies)
        if proxy:
            session.proxies.update({"all": proxy})

    # Mount HTTPAdapter with connection pooling sized to worker count.
    # Safe to do on any requests.Session — improves connection reuse for parallel downloads.
    if _is_requests_session(session):
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
            if max_workers > 1 and not _has_range_header(url_item):
                total_size, supports_ranges = _probe_ranged(url_item["url"], session)
                if supports_ranges and total_size >= RANGE_PARALLEL_MIN_SIZE:
                    try:
                        yield from _dispatch_parts(
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
                            for target in (sp, sp.with_name(f"{sp.name}.!dev")):
                                try:
                                    target.unlink(missing_ok=True)
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
        # Segmented download with thread pool
        # Speed is tracked here on the main thread, not in workers
        total_bytes = 0
        start_time = time.time()
        last_speed_report = start_time

        pool = ThreadPoolExecutor(max_workers=max_workers)
        event_queue: Queue[dict[str, Any]] = Queue()

        # hedging: first finisher claims the segment in seg_done under the lock,
        # the loser discards its own output and emits nothing
        seg_lock = threading.Lock()
        seg_start: dict[int, float] = {}
        seg_done: set[int] = set()
        seg_durations: list[float] = []
        hedged: set[int] = set()

        controller = AdaptiveWorkerController(cap=max_workers) if adaptive else None
        # feed CDN error signals (429/timeout/reset) from each retrying segment into the controller
        on_retry_cb = (lambda _exc: controller.record_error(time.time())) if controller else None

        def _download_worker(index: int, url_item: dict[str, Any], hedge: bool = False) -> None:
            item = dict(url_item)
            save_path = item.pop("save_path")
            if save_path.exists():  # finished in a previous run
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
                on_retry=on_retry_cb,
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

        def _submit(count: int, only: Optional[set[int]] = None) -> None:
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
                pending.add(pool.submit(_download_worker, item[0], item[1]))
                picked.append(item)
                count -= 1
            for item in picked:
                remaining.remove(item)

        def _tail_part_worker(
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

        def _maybe_tail_boost(target: int) -> None:
            # Tail boost: near the end, split the few not-yet-started segments across idle
            # workers with intra-segment range parts so aggregate throughput doesn't collapse
            # to (few segments)x(per-connection speed). Only not-yet-started segments in
            # `remaining` are boosted (never mid-flight ones) to avoid entangling the main
            # loop; gated behind adaptive, though it could be decoupled from the controller.
            # Engage on idle capacity so the boost fires despite `remaining` draining in strides
            # of the worker target; probe at most TAIL_BOOST_MAX_PER_CYCLE per cycle so a run of
            # blocking range probes can't stall the drain loop.
            if DOWNLOAD_CANCELLED.is_set() or not _tail_boost_engages(len(remaining), len(pending), target):
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
                if save_path.exists():  # finished in a previous run; let the normal fast-path handle it
                    tail_skipped.add(index)
                    continue
                if _has_range_header(item):  # byte-range slice; part-mode would clobber its Range
                    tail_skipped.add(index)
                    continue
                req_kwargs = {k: v for k, v in item.items() if k != "url"}
                size, supports_ranges = _probe_ranged(item["url"], session, **req_kwargs)
                parts = _plan_tail_parts(size, spare) if supports_ranges else []
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
                abort = threading.Event()
                parts_left = [len(parts)]
                part_lock = threading.Lock()
                for start, end in parts:
                    pending.add(
                        pool.submit(
                            _tail_part_worker,
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

        if controller:
            _submit(controller.update(time.time()))
        else:
            _submit(len(remaining))

        pending_advance = 0

        try:
            while pending or remaining:
                # Drain queued events — batch advances, track bytes for speed
                while True:
                    try:
                        event = event_queue.get_nowait()
                    except Empty:
                        break
                    # Accumulate advance events for batched yield
                    advance = event.get("advance")
                    if advance:
                        pending_advance += advance
                        continue
                    # Track bytes from completed segments for speed calculation
                    written = event.get("written")
                    if written:
                        total_bytes += written
                        if controller:
                            controller.record_bytes(written, time.time())
                    # Pass through other events (file_downloaded, total, etc.)
                    yield event

                # Yield batched advances every drain cycle for responsive progress bar
                if pending_advance > 0:
                    yield dict(advance=pending_advance)
                    pending_advance = 0

                now = time.time()

                # Adaptive: re-evaluate the worker target and top up in-flight futures
                target = max_workers
                if controller:
                    target = controller.update(now, len(pending) + len(remaining))
                    _maybe_tail_boost(target)  # spend spare workers on the tail before topping up
                    if len(pending) < target:
                        # In the final stride (unstarted segments <= one worker target), hold back
                        # boost candidates so idle workers accumulate for a range-split; only
                        # release segments the boost already declined (too small / no range) so
                        # nothing stalls. Otherwise top up in FIFO order as usual.
                        if remaining and len(remaining) <= target:
                            _submit(target - len(pending), only=tail_skipped)
                        else:
                            _submit(target - len(pending))

                # Yield speed every 0.5s (throttled to avoid spamming Rich)
                if now - last_speed_report > 0.5:
                    if controller:
                        rolling = controller.speed(now)
                        if rolling > 0:
                            yield dict(downloaded=f"{filesize.decimal(math.ceil(rolling))}/s")
                            last_speed_report = now
                    elif total_bytes > 0:
                        elapsed = now - start_time
                        if elapsed > 0:
                            download_speed = math.ceil(total_bytes / elapsed)
                            yield dict(downloaded=f"{filesize.decimal(download_speed)}/s")
                        last_speed_report = now

                # hedge stuck segments once spare workers exist within the active target
                if len(pending) < target and seg_durations and not DOWNLOAD_CANCELLED.is_set():
                    threshold = max(HEDGE_FACTOR * statistics.median(seg_durations), HEDGE_MIN_WAIT)
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
                        pending.add(pool.submit(_download_worker, i, urls[i], True))

                # all segments claimed; superseded losers exit via their claimed() check
                if len(seg_done) == len(urls):
                    break

                # Wait efficiently for next future completion (OS condition variable)
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
            pool.shutdown(wait=False, cancel_futures=True)

        # Drain remaining events
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

        # Flush remaining advances and final speed
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


__all__ = ("requests",)
