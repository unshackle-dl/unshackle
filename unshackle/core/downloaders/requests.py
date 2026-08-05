import math
import os
import re
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

# racers read per network arrival (read1) so superseded hedge losers exit promptly
RACER_READ1 = True

# Adaptive chunk sizing — benchmarked optimal range
MIN_CHUNK = 524_288  # 512KB
MAX_CHUNK = 4_194_304  # 4MB
DEFAULT_CHUNK = 524_288  # 512KB
SPEED_ROLLING_WINDOW = 10  # seconds of history to keep for speed calculation

RANGE_PARALLEL_MIN_SIZE = 64 * 1024 * 1024
RANGE_PARALLEL_PART_SIZE = 16 * 1024 * 1024


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


_speed_limiter: Optional[TokenBucket] = None
_speed_limit_locked = False

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
    match the displayed speeds (5M = 5.0 MB/s); values are bytes, not bits.
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
    global _speed_limiter, _speed_limit_locked
    if _speed_limit_locked and not lock:
        return
    _speed_limit_locked = lock
    _speed_limiter = TokenBucket(bytes_per_sec) if bytes_per_sec else None


def _adaptive_chunk_size(content_length: int) -> int:
    """Pick chunk size based on content length. Benchmarked sweet spot: 512KB-4MB."""
    if content_length <= 0:
        return DEFAULT_CHUNK
    return min(MAX_CHUNK, max(MIN_CHUNK, content_length // 4))


def _is_requests_session(session: Any) -> bool:
    """Check if the session is a standard requests.Session (supports resp.raw)."""
    return isinstance(session, Session)


def _is_rnet_session(session: Any) -> bool:
    """Check if the session is an RnetSession (uses resp.stream())."""
    from unshackle.core.session import RnetSession

    return isinstance(session, RnetSession)


def _is_content_encoded(value: Optional[str]) -> bool:
    """
    Check a Content-Encoding value for any coding other than absent or `identity`.

    Reading .raw skips urllib3's decoding, so an encoded body has to go through
    iter_content or the compressed bytes land on disk as the payload. Codings
    urllib3 cannot decode still count: an allowlist fails open on whatever token
    a server sends next, and the cost of guessing wrong is a corrupt file.
    """
    return any(coding.strip() not in ("", "identity") for coding in (value or "").lower().split(","))


def _probe_ranged(url: str, session: Any, **kwargs: Any) -> tuple[int, bool]:
    headers = {**(kwargs.get("headers") or {}), "Range": "bytes=0-0"}
    rest = {k: v for k, v in kwargs.items() if k != "headers"}
    if _is_rnet_session(session):
        rest.setdefault("read_timeout", READ_TIMEOUT)
    else:
        rest.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
    try:
        resp = session.get(url, stream=True, headers=headers, **rest)
    except Exception:
        return 0, False
    try:
        if resp.status_code != 206:
            return 0, False
        if _is_content_encoded(resp.headers.get("Content-Encoding") or resp.headers.get("content-encoding")):
            return 0, False
        content_range = resp.headers.get("Content-Range") or resp.headers.get("content-range") or ""
        total = content_range.rsplit("/", 1)[-1].strip()
        return (int(total), True) if total.isdigit() else (0, False)
    finally:
        try:
            resp.close()
        except Exception:
            pass


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

    def _worker(start: int, end: int) -> None:
        for ev in download(
            url=url,
            save_path=save_path,
            session=session,
            part_offset=start,
            part_end=end,
            **kwargs,
        ):
            events.put(ev)

    pool = ThreadPoolExecutor(max_workers=len(parts))
    futures = [pool.submit(_worker, s, e) for s, e in parts]
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
                    DOWNLOAD_CANCELLED.set()
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
    **kwargs: Any,
) -> Generator[dict[str, Any], None, None]:
    """
    Download a file with optimized I/O.

    Supports both requests.Session and RnetSession for TLS fingerprinting.
    RnetSession streams natively; requests.Session reads the raw socket, except
    on a content-encoded body, which has to go through iter_content to be decoded.

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
            stream.raise_for_status()

            resumed = (not part_mode) and resume_offset > 0 and stream.status_code == 206
            if (not part_mode) and resume_offset > 0 and not resumed:
                resume_offset = 0
            if part_mode and stream.status_code != 206:
                raise IOError(f"expected 206 for ranged part, got {stream.status_code}")
            content_encoded = False
            if use_rnet:
                content_length = stream.content_length or 0
            else:
                content_encoded = _is_content_encoded(stream.headers.get("Content-Encoding"))
                try:
                    content_length = int(stream.headers.get("Content-Length", "0"))
                    if content_encoded:
                        content_length = 0
                except ValueError:
                    content_length = 0

            if resumed and content_encoded:
                try:
                    stream.close()
                except Exception:
                    pass
                tmp_file.unlink(missing_ok=True)
                resume_offset = 0
                continue

            limiter = _speed_limiter
            chunk_size = _adaptive_chunk_size(content_length)
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
            with open(save_path if part_mode else tmp_file, file_mode, buffering=file_buffering) as f:
                if part_mode:
                    f.seek(part_offset + written)
                elif not resumed and content_length > 0:
                    f.truncate(content_length)
                    f.seek(0)

                _write = f.write

                if use_rnet:
                    chunks = stream.stream()
                elif use_raw and not content_encoded:
                    _read1 = getattr(stream.raw, "read1", None) if claimed is not None and RACER_READ1 else None
                    if _read1 is not None:
                        chunks = iter(lambda: _read1(chunk_size), b"")
                    else:
                        chunks = iter(lambda: stream.raw.read(chunk_size), b"")
                else:
                    chunks = stream.iter_content(chunk_size=chunk_size)

                _data_accumulated = 0
                _bytes_since_yield = 0
                emit_progress = (not segmented) or part_mode
                for chunk in chunks:
                    if DOWNLOAD_CANCELLED.is_set():
                        break
                    if claimed is not None and claimed():
                        # close the handle or Windows can't delete the stray .!dev at merge (WinError 32)
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

                try:
                    stream.close()
                except Exception:
                    pass

                if not part_mode and not resumed and content_length > 0 and written != content_length:
                    f.truncate(written)

            if DOWNLOAD_CANCELLED.is_set() and (not content_length or written < content_length):
                # cancelled mid-stream: keep the partial .!dev for resume, never finalize
                return

            if part_mode:
                expected = part_end - part_offset + 1
                if written < expected:
                    raise IOError(f"Failed to read part {part_offset}-{part_end}: got {written}/{expected}")
            elif not segmented and content_length and written < content_length:
                raise IOError(f"Failed to read {content_length} bytes from the track URI.")

            if not part_mode:
                os.replace(tmp_file, save_path)
                yield dict(file_downloaded=save_path, written=resume_offset + written)
                if segmented:
                    yield dict(advance=1)
            break
        except Exception:
            try:
                stream.close()
            except Exception:
                pass
            if claimed is not None and claimed():
                # a superseded loser's error must not retry or kill the batch
                return
            if DOWNLOAD_CANCELLED.is_set() or attempts == MAX_ATTEMPTS:
                if part_mode and not DOWNLOAD_CANCELLED.is_set():
                    raise
                return
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
            },
        )

    segmented_batch = len(urls) > 1

    if len(urls) == 1:
        url_item = urls[0]
        try:
            ranged_used = False
            if max_workers > 1 and _speed_limiter is None:
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
                seg_start[index] = time.time()
            for event in download(
                session=session,
                segmented=segmented_batch,
                save_path=target,
                claimed=lambda: index in seg_done,
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

        futures = [pool.submit(_download_worker, i, url) for i, url in enumerate(urls)]
        pending = set(futures)

        pending_advance = 0

        try:
            while pending:
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
                    # Pass through other events (file_downloaded, total, etc.)
                    yield event

                # Yield batched advances every drain cycle for responsive progress bar
                if pending_advance > 0:
                    yield dict(advance=pending_advance)
                    pending_advance = 0

                # Yield speed every 0.5s (throttled to avoid spamming Rich)
                now = time.time()
                if now - last_speed_report > 0.5 and total_bytes > 0:
                    rate = speed_window.rate(now, total_bytes)
                    if rate:
                        yield dict(downloaded=f"{filesize.decimal(math.ceil(rate))}/s")
                    last_speed_report = now

                # hedge stuck segments once spare workers exist (pending < max_workers);
                # throttled to ~0.5s and median recomputed only when seg_durations grows
                if (
                    len(pending) < max_workers
                    and seg_durations
                    and _speed_limiter is None
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
                            if i not in seg_done and i not in hedged and now - started > threshold
                        ]
                    for i in stuck[: max_workers - len(pending)]:
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
            # losers must close their handles before merge sweeps *.!dev (WinError 32);
            # no wait on cancel/fail: merge never runs and workers may be blocked in reads
            pool.shutdown(wait=not DOWNLOAD_CANCELLED.is_set(), cancel_futures=True)

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


__all__ = ("requests", "format_speed", "parse_speed_limit", "set_speed_limit")
