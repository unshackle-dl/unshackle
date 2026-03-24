import math
import os
import time
from concurrent.futures import FIRST_COMPLETED, wait
from concurrent.futures.thread import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Generator, MutableMapping, Optional, Union

from requests import Session
from requests.adapters import HTTPAdapter
from rich import filesize

from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.utilities import get_debug_logger, get_extension

MAX_ATTEMPTS = 5
RETRY_WAIT = 2
PROGRESS_WINDOW = 2

# Adaptive chunk sizing — benchmarked optimal range
MIN_CHUNK = 524_288      # 512KB
MAX_CHUNK = 4_194_304    # 4MB
DEFAULT_CHUNK = 524_288  # 512KB
SPEED_ROLLING_WINDOW = 10  # seconds of history to keep for speed calculation


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


def download(
    url: str,
    save_path: Path,
    session: Optional[Any] = None,
    segmented: bool = False,
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
        kwargs: Any extra keyword arguments to pass to the session.get() call. Use this
            for one-time request changes like a header, cookie, or proxy. For example,
            to request Byte-ranges use e.g., `headers={"Range": "bytes=0-128"}`.
    """
    session = session or Session()

    save_dir = save_path.parent
    control_file = save_path.with_name(f"{save_path.name}.!dev")

    save_dir.mkdir(parents=True, exist_ok=True)

    if control_file.exists():
        save_path.unlink(missing_ok=True)
        control_file.unlink()
    elif save_path.exists():
        yield dict(file_downloaded=save_path, written=save_path.stat().st_size)

    control_file.write_bytes(b"")
    _time = time.time
    use_raw = _is_requests_session(session)

    attempts = 1
    try:
        while True:
            written = 0
            last_speed_refresh = _time()

            try:
                use_rnet = _is_rnet_session(session)
                stream = session.get(url, stream=True, **kwargs)
                stream.raise_for_status()

                # Determine content length and adaptive chunk size
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

                if not segmented:
                    if content_length > 0:
                        yield dict(total=content_length)
                    else:
                        yield dict(total=None)

                # Pre-allocate file when size is known (helps filesystem allocate contiguous blocks)
                with open(save_path, "wb", buffering=1_048_576) as f:
                    if content_length > 0:
                        f.truncate(content_length)
                        f.seek(0)

                    # Cache f.write for hot loop
                    _write = f.write

                    # Build chunk iterator based on session type
                    if use_rnet:
                        # rnet: native Rust streaming — 3.5x faster than curl_cffi (benchmarked)
                        chunks = stream.stream()
                    elif use_raw:
                        # requests.Session: raw socket read — 30-35% faster than iter_content
                        stream.raw.decode_content = False
                        _read = stream.raw.read

                        def _chunks() -> Generator[bytes, None, None]:
                            while True:
                                chunk = _read(chunk_size)
                                if not chunk:
                                    break
                                yield chunk
                            stream.close()

                        chunks = _chunks()
                    else:
                        # Fallback: iter_content
                        def _chunks_iter() -> Generator[bytes, None, None]:
                            yield from stream.iter_content(chunk_size=chunk_size)
                            stream.close()

                        chunks = _chunks_iter()

                    # Unified write + progress loop
                    _data_accumulated = 0
                    _bytes_since_yield = 0
                    for chunk in chunks:
                        if DOWNLOAD_CANCELLED.is_set():
                            break
                        _write(chunk)
                        download_size = len(chunk)
                        written += download_size

                        if not segmented:
                            _bytes_since_yield += download_size
                            _data_accumulated += download_size
                            now = _time()
                            time_since = now - last_speed_refresh
                            if time_since > PROGRESS_WINDOW:
                                yield dict(advance=_bytes_since_yield)
                                _bytes_since_yield = 0
                                download_speed = math.ceil(_data_accumulated / (time_since or 1))
                                yield dict(downloaded=f"{filesize.decimal(download_speed)}/s")
                                last_speed_refresh = now
                                _data_accumulated = 0

                    # Flush any remaining bytes
                    if not segmented and _bytes_since_yield > 0:
                        yield dict(advance=_bytes_since_yield)

                    # Truncate to actual written size in case pre-allocation overshot
                    if content_length > 0 and written != content_length:
                        f.truncate(written)

                if not segmented and content_length and written < content_length:
                    raise IOError(f"Failed to read {content_length} bytes from the track URI.")

                yield dict(file_downloaded=save_path, written=written)

                if segmented:
                    yield dict(advance=1)
                break
            except Exception as e:
                save_path.unlink(missing_ok=True)
                if DOWNLOAD_CANCELLED.is_set() or attempts == MAX_ATTEMPTS:
                    raise e
                time.sleep(RETRY_WAIT)
                attempts += 1
    finally:
        control_file.unlink()


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
            min(12,(cpu_count+4)).
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
            },
        )

    segmented_batch = len(urls) > 1

    # Fast path: single URL — no thread pool overhead
    if len(urls) == 1:
        try:
            yield from download(
                session=session,
                segmented=segmented_batch,
                **urls[0],
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

        def _download_worker(url_item: dict[str, Any]) -> None:
            for event in download(
                session=session,
                segmented=segmented_batch,
                **url_item,
            ):
                event_queue.put(event)

        futures = [pool.submit(_download_worker, url) for url in urls]
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
                    elapsed = now - start_time
                    if elapsed > 0:
                        download_speed = math.ceil(total_bytes / elapsed)
                        yield dict(downloaded=f"{filesize.decimal(download_speed)}/s")
                    last_speed_report = now

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
