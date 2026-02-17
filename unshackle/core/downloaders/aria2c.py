import logging
import os
import subprocess
import textwrap
import threading
import time
from functools import partial
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Callable, Generator, MutableMapping, Optional, Union
from urllib.parse import urlparse

import requests
from Crypto.Random import get_random_bytes
from requests import Session
from requests.cookies import cookiejar_from_dict, get_cookie_header
from rich import filesize
from rich.text import Text

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.utilities import get_debug_logger, get_extension, get_free_port


def rpc(caller: Callable, secret: str, method: str, params: Optional[list[Any]] = None) -> Any:
    """Make a call to Aria2's JSON-RPC API."""
    try:
        rpc_res = caller(
            json={
                "jsonrpc": "2.0",
                "id": get_random_bytes(16).hex(),
                "method": method,
                "params": [f"token:{secret}", *(params or [])],
            }
        ).json()
        if rpc_res.get("code"):
            # wrap to console width - padding - '[Aria2c]: '
            error_pretty = "\n          ".join(
                textwrap.wrap(
                    f"RPC Error: {rpc_res['message']} ({rpc_res['code']})".strip(),
                    width=console.width - 20,
                    initial_indent="",
                )
            )
            console.log(Text.from_ansi("\n[Aria2c]: " + error_pretty))
        return rpc_res["result"]
    except requests.exceptions.ConnectionError:
        # absorb, process likely ended as it was calling RPC
        return


class _Aria2Manager:
    """Singleton manager to run one aria2c process and enqueue downloads via RPC."""

    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)
        self._proc: Optional[subprocess.Popen] = None
        self._rpc_port: Optional[int] = None
        self._rpc_secret: Optional[str] = None
        self._rpc_uri: Optional[str] = None
        self._session: Session = Session()
        self._max_workers: Optional[int] = None
        self._max_concurrent_downloads: int = 0
        self._max_connection_per_server: int = 1
        self._split_default: int = 5
        self._file_allocation: str = "prealloc"
        self._proxy: Optional[str] = None
        self._lock: threading.Lock = threading.Lock()

    def _wait_for_rpc_ready(self, timeout_s: float = 8.0, interval_s: float = 0.1) -> None:
        assert self._proc is not None
        assert self._rpc_uri is not None
        assert self._rpc_secret is not None

        deadline = time.monotonic() + timeout_s

        payload = {
            "jsonrpc": "2.0",
            "id": get_random_bytes(16).hex(),
            "method": "aria2.getVersion",
            "params": [f"token:{self._rpc_secret}"],
        }

        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"aria2c exited before RPC became ready (exit code {self._proc.returncode})"
                )
            try:
                res = self._session.post(self._rpc_uri, json=payload, timeout=0.25)
                data = res.json()
                if isinstance(data, dict) and data.get("result") is not None:
                    return
            except (requests.exceptions.RequestException, ValueError):
                # Not ready yet (connection refused / bad response / etc.)
                pass
            time.sleep(interval_s)

        # Timed out: ensure we don't leave a zombie/stray aria2c process behind.
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except Exception:
                pass
        raise TimeoutError(f"aria2c RPC did not become ready within {timeout_s:.1f}s")

    def _build_args(self) -> list[str]:
        args = [
            "--continue=true",
            f"--max-concurrent-downloads={self._max_concurrent_downloads}",
            f"--max-connection-per-server={self._max_connection_per_server}",
            f"--split={self._split_default}",
            "--max-file-not-found=5",
            "--max-tries=5",
            "--retry-wait=2",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--console-log-level=warn",
            "--download-result=default",
            f"--file-allocation={self._file_allocation}",
            "--summary-interval=0",
            "--enable-rpc=true",
            f"--rpc-listen-port={self._rpc_port}",
            f"--rpc-secret={self._rpc_secret}",
        ]
        if self._proxy:
            args.extend(["--all-proxy", self._proxy])
        return args

    def ensure_started(
        self,
        proxy: Optional[str],
        max_workers: Optional[int],
    ) -> None:
        with self._lock:
            if not binaries.Aria2:
                debug_logger = get_debug_logger()
                if debug_logger:
                    debug_logger.log(
                        level="ERROR",
                        operation="downloader_aria2c_binary_missing",
                        message="Aria2c executable not found in PATH or local binaries directory",
                        context={"searched_names": ["aria2c", "aria2"]},
                    )
                raise EnvironmentError("Aria2c executable not found...")

            effective_proxy = proxy or None

            if not max_workers:
                effective_max_workers = min(32, (os.cpu_count() or 1) + 4)
            elif not isinstance(max_workers, int):
                raise TypeError(f"Expected max_workers to be {int}, not {type(max_workers)}")
            else:
                effective_max_workers = max_workers

            if self._proc and self._proc.poll() is None:
                if effective_proxy != self._proxy or effective_max_workers != self._max_workers:
                    self._logger.warning(
                        "aria2c process is already running; requested proxy=%r, max_workers=%r, "
                        "but running process will continue with proxy=%r, max_workers=%r",
                        effective_proxy,
                        effective_max_workers,
                        self._proxy,
                        self._max_workers,
                    )
                return

            self._rpc_port = get_free_port()
            self._rpc_secret = get_random_bytes(16).hex()
            self._rpc_uri = f"http://127.0.0.1:{self._rpc_port}/jsonrpc"

            self._max_workers = effective_max_workers
            self._max_concurrent_downloads = int(
                config.aria2c.get("max_concurrent_downloads", effective_max_workers)
            )
            self._max_connection_per_server = int(config.aria2c.get("max_connection_per_server", 1))
            self._split_default = int(config.aria2c.get("split", 5))
            self._file_allocation = config.aria2c.get("file_allocation", "prealloc")
            self._proxy = effective_proxy

            args = self._build_args()
            self._proc = subprocess.Popen(
                [binaries.Aria2, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self._wait_for_rpc_ready()

    @property
    def rpc_uri(self) -> str:
        assert self._rpc_uri
        return self._rpc_uri

    @property
    def rpc_secret(self) -> str:
        assert self._rpc_secret
        return self._rpc_secret

    @property
    def session(self) -> Session:
        return self._session

    def add_uris(self, uris: list[str], options: dict[str, Any]) -> str:
        """Add a single download with multiple URIs via RPC."""
        gid = rpc(
            caller=partial(self._session.post, url=self.rpc_uri),
            secret=self.rpc_secret,
            method="aria2.addUri",
            params=[uris, options],
        )
        return gid or ""

    def get_global_stat(self) -> dict[str, Any]:
        return rpc(
            caller=partial(self.session.post, url=self.rpc_uri),
            secret=self.rpc_secret,
            method="aria2.getGlobalStat",
        ) or {}

    def tell_status(self, gid: str) -> Optional[dict[str, Any]]:
        return rpc(
            caller=partial(self.session.post, url=self.rpc_uri),
            secret=self.rpc_secret,
            method="aria2.tellStatus",
            params=[gid, ["status", "errorCode", "errorMessage", "files", "completedLength", "totalLength"]],
        )

    def remove(self, gid: str) -> None:
        rpc(
            caller=partial(self.session.post, url=self.rpc_uri),
            secret=self.rpc_secret,
            method="aria2.forceRemove",
            params=[gid],
        )


_manager = _Aria2Manager()


def download(
    urls: Union[str, list[str], dict[str, Any], list[dict[str, Any]]],
    output_dir: Path,
    filename: str,
    headers: Optional[MutableMapping[str, Union[str, bytes]]] = None,
    cookies: Optional[Union[MutableMapping[str, str], CookieJar]] = None,
    proxy: Optional[str] = None,
    max_workers: Optional[int] = None,
) -> Generator[dict[str, Any], None, None]:
    """Enqueue downloads to the singleton aria2c instance via stdin and track per-call progress via RPC."""
    debug_logger = get_debug_logger()

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

    if not max_workers:
        max_workers = min(32, (os.cpu_count() or 1) + 4)
    elif not isinstance(max_workers, int):
        raise TypeError(f"Expected max_workers to be {int}, not {type(max_workers)}")

    if not isinstance(urls, list):
        urls = [urls]

    if cookies and not isinstance(cookies, CookieJar):
        cookies = cookiejar_from_dict(cookies)

    _manager.ensure_started(proxy=proxy, max_workers=max_workers)

    if debug_logger:
        first_url = urls[0] if isinstance(urls[0], str) else urls[0].get("url", "")
        url_display = first_url[:200] + "..." if len(first_url) > 200 else first_url
        debug_logger.log(
            level="DEBUG",
            operation="downloader_aria2c_start",
            message="Starting Aria2c download",
            context={
                "binary_path": str(binaries.Aria2),
                "url_count": len(urls),
                "first_url": url_display,
                "output_dir": str(output_dir),
                "filename": filename,
                "has_proxy": bool(proxy),
            },
        )

    # Build options for each URI and add via RPC
    gids: list[str] = []

    for i, url in enumerate(urls):
        if isinstance(url, str):
            url_data = {"url": url}
        else:
            url_data: dict[str, Any] = url

        url_filename = filename.format(i=i, ext=get_extension(url_data["url"]))

        opts: dict[str, Any] = {
            "dir": str(output_dir),
            "out": url_filename,
            "split": str(1 if len(urls) > 1 else int(config.aria2c.get("split", 5))),
        }

        # Cookies as header
        if cookies:
            mock_request = requests.Request(url=url_data["url"])
            cookie_header = get_cookie_header(cookies, mock_request)
            if cookie_header:
                opts.setdefault("header", []).append(f"Cookie: {cookie_header}")

        # Global headers
        for header, value in (headers or {}).items():
            if header.lower() == "cookie":
                raise ValueError("You cannot set Cookies as a header manually, please use the `cookies` param.")
            if header.lower() == "accept-encoding":
                continue
            if header.lower() == "referer":
                opts["referer"] = str(value)
                continue
            if header.lower() == "user-agent":
                opts["user-agent"] = str(value)
                continue
            opts.setdefault("header", []).append(f"{header}: {value}")

        # Per-url extra args
        for key, value in url_data.items():
            if key == "url":
                continue
            if key == "headers":
                for header_name, header_value in value.items():
                    opts.setdefault("header", []).append(f"{header_name}: {header_value}")
            else:
                opts[key] = str(value)

        # Add via RPC
        gid = _manager.add_uris([url_data["url"]], opts)
        if gid:
            gids.append(gid)

    yield dict(total=len(gids))

    completed: set[str] = set()

    try:
        while len(completed) < len(gids):
            if DOWNLOAD_CANCELLED.is_set():
                # Remove tracked downloads on cancel
                for gid in gids:
                    if gid not in completed:
                        _manager.remove(gid)
                yield dict(downloaded="[yellow]CANCELLED")
                raise KeyboardInterrupt()

            stats = _manager.get_global_stat()
            dl_speed = int(stats.get("downloadSpeed", -1))

            # Aggregate progress across all GIDs for this call
            total_completed = 0
            total_size = 0

            # Check each tracked GID
            for gid in gids:
                if gid in completed:
                    continue

                status = _manager.tell_status(gid)
                if not status:
                    continue

                completed_length = int(status.get("completedLength", 0))
                total_length = int(status.get("totalLength", 0))
                total_completed += completed_length
                total_size += total_length

                state = status.get("status")
                if state in ("complete", "error"):
                    completed.add(gid)
                    yield dict(completed=len(completed))

                    if state == "error":
                        used_uri = None
                        try:
                            used_uri = next(
                                uri["uri"]
                                for file in status.get("files", [])
                                for uri in file.get("uris", [])
                                if uri.get("status") == "used"
                            )
                        except Exception:
                            used_uri = "unknown"
                        error = f"Download Error (#{gid}): {status.get('errorMessage')} ({status.get('errorCode')}), {used_uri}"
                        error_pretty = "\n          ".join(textwrap.wrap(error, width=console.width - 20, initial_indent=""))
                        console.log(Text.from_ansi("\n[Aria2c]: " + error_pretty))
                        if debug_logger:
                            debug_logger.log(
                                level="ERROR",
                                operation="downloader_aria2c_download_error",
                                message=f"Aria2c download failed: {status.get('errorMessage')}",
                                context={
                                    "gid": gid,
                                    "error_code": status.get("errorCode"),
                                    "error_message": status.get("errorMessage"),
                                    "used_uri": used_uri[:200] + "..." if used_uri and len(used_uri) > 200 else used_uri,
                                    "completed_length": status.get("completedLength"),
                                    "total_length": status.get("totalLength"),
                                },
                            )
                        raise ValueError(error)

            # Yield aggregate progress for this call's downloads
            if total_size > 0:
                # Yield both advance (bytes downloaded this iteration) and total for rich progress
                if dl_speed != -1:
                    yield dict(downloaded=f"{filesize.decimal(dl_speed)}/s", advance=0, completed=total_completed, total=total_size)
                else:
                    yield dict(advance=0, completed=total_completed, total=total_size)
            elif dl_speed != -1:
                yield dict(downloaded=f"{filesize.decimal(dl_speed)}/s")

            time.sleep(1)
    except KeyboardInterrupt:
        DOWNLOAD_CANCELLED.set()
        raise
    except Exception as e:
        DOWNLOAD_CANCELLED.set()
        yield dict(downloaded="[red]FAILED")
        if debug_logger and not isinstance(e, ValueError):
            debug_logger.log(
                level="ERROR",
                operation="downloader_aria2c_exception",
                message=f"Unexpected error during Aria2c download: {e}",
                error=e,
                context={
                    "url_count": len(urls),
                    "output_dir": str(output_dir),
                },
            )
        raise


def aria2c(
    urls: Union[str, list[str], dict[str, Any], list[dict[str, Any]]],
    output_dir: Path,
    filename: str,
    headers: Optional[MutableMapping[str, Union[str, bytes]]] = None,
    cookies: Optional[Union[MutableMapping[str, str], CookieJar]] = None,
    proxy: Optional[str] = None,
    max_workers: Optional[int] = None,
) -> Generator[dict[str, Any], None, None]:
    """
    Download files using Aria2(c).
    https://aria2.github.io

    Yields the following download status updates while chunks are downloading:

    - {total: 100} (100% download total)
    - {completed: 1} (1% download progress out of 100%)
    - {downloaded: "10.1 MB/s"} (currently downloading at a rate of 10.1 MB/s)

    The data is in the same format accepted by rich's progress.update() function.

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
            min(32,(cpu_count+4)). Use for the --max-concurrent-downloads option.
    """
    if proxy and not proxy.lower().startswith("http://"):
        # Only HTTP proxies are supported by aria2(c)
        proxy = urlparse(proxy)

        port = get_free_port()
        username, password = get_random_bytes(8).hex(), get_random_bytes(8).hex()
        local_proxy = f"http://{username}:{password}@localhost:{port}"

        scheme = {"https": "http+ssl", "socks5h": "socks"}.get(proxy.scheme, proxy.scheme)

        remote_server = f"{scheme}://{proxy.hostname}"
        if proxy.port:
            remote_server += f":{proxy.port}"
        if proxy.username or proxy.password:
            remote_server += "#"
        if proxy.username:
            remote_server += proxy.username
        if proxy.password:
            remote_server += f":{proxy.password}"

        p = subprocess.Popen(
            ["pproxy", "-l", f"http://:{port}#{username}:{password}", "-r", remote_server],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            yield from download(urls, output_dir, filename, headers, cookies, local_proxy, max_workers)
        finally:
            p.kill()
            p.wait()
        return
    yield from download(urls, output_dir, filename, headers, cookies, proxy, max_workers)


__all__ = ("aria2c",)
