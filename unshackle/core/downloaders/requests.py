import math
import os
import time
from concurrent.futures import as_completed
from concurrent.futures.thread import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
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



def download(
    url: str,
    save_path: Path,
    session: Optional[Any] = None,
    segmented: bool = False,
    _speed_tracker: Optional[dict] = None,
    **kwargs: Any,
) -> Generator[dict[str, Any], None, None]:
    """
    Download a file with optimized I/O.

    Supports both requests.Session and curl_cffi CurlSession for TLS fingerprinting.
    Uses adaptive chunk sizing with buffered writes for maximum throughput.

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
        session: A requests.Session or curl_cffi CurlSession to make HTTP requests with.
            CurlSession preserves TLS fingerprinting for services that need it.
        segmented: If downloads are segments or parts of one bigger file.
        _speed_tracker: Shared speed tracking state for this download batch (per-call, not global).
        kwargs: Any extra keyword arguments to pass to the session.get() call. Use this
            for one-time request changes like a header, cookie, or proxy. For example,
            to request Byte-ranges use e.g., `headers={"Range": "bytes=0-128"}`.
    """
    session = session or Session()

    # Per-call speed tracking (shared across threads within one requests() call)
    if _speed_tracker is None:
        _speed_tracker = {"sizes": [], "last_refresh": time.time()}

    save_dir = save_path.parent
    control_file = save_path.with_name(f"{save_path.name}.!dev")

    save_dir.mkdir(parents=True, exist_ok=True)

    if control_file.exists():
        save_path.unlink(missing_ok=True)
        control_file.unlink()
    elif save_path.exists():
        yield dict(file_downloaded=save_path, written=save_path.stat().st_size)

    control_file.write_bytes(b"")

    attempts = 1
    try:
        while True:
            written = 0
            download_sizes: list[int] = []
            last_speed_refresh = time.time()

            try:
                stream = session.get(url, stream=True, **kwargs)
                stream.raise_for_status()

                # Determine content length and adaptive chunk size
                try:
                    content_length = int(stream.headers.get("Content-Length", "0"))
                    if stream.headers.get("Content-Encoding", "").lower() in ["gzip", "deflate", "br"]:
                        content_length = 0
                except ValueError:
                    content_length = 0

                chunk_size = _adaptive_chunk_size(content_length)

                if not segmented:
                    if content_length > 0:
                        yield dict(total=math.ceil(content_length / chunk_size))
                    else:
                        yield dict(total=None)

                # Buffered iter_content with adaptive chunk size
                # Works with both requests.Session and CurlSession
                with open(save_path, "wb", buffering=1_048_576) as f:
                    for chunk in stream.iter_content(chunk_size=chunk_size):
                        download_size = len(chunk)
                        f.write(chunk)
                        written += download_size

                        if not segmented:
                            yield dict(advance=1)
                            now = time.time()
                            time_since = now - last_speed_refresh
                            download_sizes.append(download_size)
                            if time_since > PROGRESS_WINDOW or download_size < chunk_size:
                                data_size = sum(download_sizes)
                                download_speed = math.ceil(data_size / (time_since or 1))
                                yield dict(downloaded=f"{filesize.decimal(download_speed)}/s")
                                last_speed_refresh = now
                                download_sizes.clear()

                if not segmented and content_length and written < content_length:
                    raise IOError(f"Failed to read {content_length} bytes from the track URI.")

                yield dict(file_downloaded=save_path, written=written)

                if segmented:
                    yield dict(advance=1)
                    now = time.time()
                    sizes = _speed_tracker["sizes"]
                    if written:
                        sizes.append((now, written))
                    # Prune entries older than the rolling window
                    cutoff = now - SPEED_ROLLING_WINDOW
                    while sizes and sizes[0][0] < cutoff:
                        sizes.pop(0)
                    time_since = now - _speed_tracker["last_refresh"]
                    if sizes and time_since > PROGRESS_WINDOW:
                        window_start = sizes[0][0]
                        window_duration = now - window_start
                        data_size = sum(size for _, size in sizes)
                        download_speed = math.ceil(data_size / (window_duration or 1))
                        yield dict(downloaded=f"{filesize.decimal(download_speed)}/s")
                        _speed_tracker["last_refresh"] = now
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

    Supports both requests.Session and curl_cffi CurlSession. When a CurlSession is
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
        session: An optional requests.Session or curl_cffi CurlSession to use. If provided,
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
    # When a session is provided (e.g., service's CurlSession), don't mutate it —
    # headers/cookies/proxy are already set on it and it may be shared across tracks.
    if session is None:
        session = Session()
        session.mount("https://", HTTPAdapter(pool_connections=max_workers, pool_maxsize=max_workers, pool_block=True))
        session.mount("http://", HTTPAdapter(pool_connections=max_workers, pool_maxsize=max_workers, pool_block=True))
        if headers:
            headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
            session.headers.update(headers)
        if cookies:
            session.cookies.update(cookies)
        if proxy:
            session.proxies.update({"all": proxy})

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
    if segmented_batch:
        yield dict(total=len(urls))

    # Per-call speed tracker — shared across threads within this call only
    speed_tracker: dict[str, Any] = {"sizes": [], "last_refresh": time.time()}

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for future in as_completed(
                pool.submit(download, session=session, segmented=segmented_batch, _speed_tracker=speed_tracker, **url)
                for url in urls
            ):
                try:
                    yield from future.result()
                except KeyboardInterrupt:
                    DOWNLOAD_CANCELLED.set()
                    yield dict(downloaded="[yellow]CANCELLING")
                    pool.shutdown(wait=True, cancel_futures=True)
                    yield dict(downloaded="[yellow]CANCELLED")
                    raise
                except Exception as e:
                    DOWNLOAD_CANCELLED.set()
                    yield dict(downloaded="[red]FAILING")
                    pool.shutdown(wait=True, cancel_futures=True)
                    yield dict(downloaded="[red]FAILED")
                    if debug_logger:
                        debug_logger.log(
                            level="ERROR",
                            operation="downloader_failed",
                            message=f"Download failed: {e}",
                            error=e,
                            context={
                                "url_count": len(urls),
                                "output_dir": str(output_dir),
                            },
                        )
                    raise

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
    finally:
        speed_tracker["sizes"].clear()


__all__ = ("requests",)
