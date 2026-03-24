import json
import logging
import os
import subprocess
import threading
import time
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Generator, MutableMapping, Optional, Union

import requests
from Crypto.Random import get_random_bytes
from requests import Session
from requests.cookies import cookiejar_from_dict, get_cookie_header
from rich import filesize

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.utilities import get_debug_logger, get_extension, get_free_port


def _stringify_header_value(value: Union[str, bytes]) -> str:
    if isinstance(value, bytes):
        return value.decode("utf8", errors="ignore")
    return str(value)


class _SurgeManager:
    """Singleton manager to run one Surge server process and enqueue downloads via HTTP API."""

    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)
        self._proc: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._token: Optional[str] = None
        self._base_url: Optional[str] = None
        self._session: Session = Session()
        self._session.trust_env = False
        self._profile_dir: Optional[Path] = None
        self._max_workers: Optional[int] = None
        self._max_connections_per_host: int = 0
        self._max_concurrent_downloads: int = 0
        self._proxy: Optional[str] = None
        self._lock: threading.Lock = threading.Lock()

    def _build_env(self, profile_dir: Path) -> tuple[dict[str, str], Path]:
        env = os.environ.copy()
        temp_dir = profile_dir / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)

        if os.name == "nt":
            appdata_dir = profile_dir / "appdata"
            localappdata_dir = profile_dir / "localappdata"
            appdata_dir.mkdir(parents=True, exist_ok=True)
            localappdata_dir.mkdir(parents=True, exist_ok=True)

            env["APPDATA"] = str(appdata_dir)
            env["LOCALAPPDATA"] = str(localappdata_dir)
            env["TEMP"] = str(temp_dir)
            env["TMP"] = str(temp_dir)
            settings_path = appdata_dir / "surge" / "settings.json"
        else:
            config_home = profile_dir / "config"
            state_home = profile_dir / "state"
            runtime_dir = profile_dir / "runtime"

            config_home.mkdir(parents=True, exist_ok=True)
            state_home.mkdir(parents=True, exist_ok=True)
            runtime_dir.mkdir(parents=True, exist_ok=True)

            env["XDG_CONFIG_HOME"] = str(config_home)
            env["XDG_STATE_HOME"] = str(state_home)
            env["XDG_RUNTIME_DIR"] = str(runtime_dir)
            env["TEMP"] = str(temp_dir)
            env["TMP"] = str(temp_dir)
            settings_path = config_home / "surge" / "settings.json"

        settings_path.parent.mkdir(parents=True, exist_ok=True)
        return env, settings_path

    def _write_settings(self, settings_path: Path, effective_proxy: Optional[str]) -> None:
        settings = {
            "general": {
                "default_download_dir": "",
                "warn_on_duplicate": False,
                "download_complete_notification": False,
                "allow_remote_open_actions": False,
                "extension_prompt": False,
                "auto_resume": False,
                "skip_update_check": True,
                "category_enabled": False,
                "categories": [],
                "clipboard_monitor": False,
                "theme": 0,
                "log_retention_count": 1,
            },
            "network": {
                "max_connections_per_host": self._max_connections_per_host,
                "max_concurrent_downloads": self._max_concurrent_downloads,
                "user_agent": str(config.surge.get("user_agent", "") or ""),
                "proxy_url": effective_proxy or "",
                "sequential_download": bool(config.surge.get("sequential_download", False)),
                "min_chunk_size": int(config.surge.get("min_chunk_size", 2 * 1024 * 1024)),
                "worker_buffer_size": int(config.surge.get("worker_buffer_size", 512 * 1024)),
            },
        }
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf8")

    def _wait_for_api_ready(self, timeout_s: float = 8.0, interval_s: float = 0.1) -> None:
        assert self._proc is not None
        assert self._base_url is not None

        deadline = time.monotonic() + timeout_s
        health_url = f"{self._base_url}/health"

        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"surge exited before API became ready (exit code {self._proc.returncode})"
                )
            try:
                res = self._session.get(health_url, timeout=0.25)
                data = res.json()
                if res.ok and isinstance(data, dict) and data.get("status") == "ok":
                    return
            except (requests.exceptions.RequestException, ValueError):
                pass
            time.sleep(interval_s)

        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except Exception:
                pass
        raise TimeoutError(f"surge API did not become ready within {timeout_s:.1f}s")

    def ensure_started(
        self,
        proxy: Optional[str],
        max_workers: Optional[int],
    ) -> None:
        with self._lock:
            if not binaries.Surge:
                debug_logger = get_debug_logger()
                if debug_logger:
                    debug_logger.log(
                        level="ERROR",
                        operation="downloader_surge_binary_missing",
                        message="Surge executable not found in PATH or local binaries directory",
                        context={"searched_names": ["surge"]},
                    )
                raise EnvironmentError("Surge executable not found...")

            configured_proxy = str(config.surge.get("proxy_url", "") or "").strip() or None
            effective_proxy = proxy or configured_proxy

            if not max_workers:
                effective_max_workers = min(32, (os.cpu_count() or 1) + 4)
            elif not isinstance(max_workers, int):
                raise TypeError(f"Expected max_workers to be {int}, not {type(max_workers)}")
            else:
                effective_max_workers = max_workers

            if self._proc and self._proc.poll() is None:
                if effective_proxy != self._proxy or effective_max_workers != self._max_workers:
                    self._logger.warning(
                        "surge process is already running; requested proxy=%r, max_workers=%r, "
                        "but running process will continue with proxy=%r, max_workers=%r",
                        effective_proxy,
                        effective_max_workers,
                        self._proxy,
                        self._max_workers,
                    )
                return

            profile_root = config.directories.temp / "surge"
            profile_root.mkdir(parents=True, exist_ok=True)

            self._profile_dir = profile_root / get_random_bytes(8).hex()
            env, settings_path = self._build_env(self._profile_dir)

            self._max_workers = effective_max_workers
            self._max_connections_per_host = int(
                config.surge.get("max_connections_per_host", effective_max_workers)
            )
            self._max_concurrent_downloads = int(
                config.surge.get("max_concurrent_downloads", effective_max_workers)
            )
            self._proxy = effective_proxy
            self._write_settings(settings_path, effective_proxy)

            self._port = get_free_port()
            self._token = get_random_bytes(16).hex()
            self._base_url = f"http://127.0.0.1:{self._port}"
            self._session.headers["Authorization"] = f"Bearer {self._token}"

            self._proc = subprocess.Popen(
                [
                    binaries.Surge,
                    "server",
                    "--port",
                    str(self._port),
                    "--output",
                    str(config.directories.temp),
                    "--token",
                    self._token,
                    "--no-resume",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            self._wait_for_api_ready()

    @property
    def base_url(self) -> str:
        assert self._base_url
        return self._base_url

    @property
    def session(self) -> Session:
        return self._session

    def add_download(
        self,
        url: str,
        output_dir: Path,
        filename: str,
        headers: Optional[dict[str, str]] = None,
        mirrors: Optional[list[str]] = None,
    ) -> str:
        payload = {
            "url": url,
            "path": str(output_dir),
            "filename": filename,
            "headers": headers or {},
            "mirrors": mirrors or [],
            "skip_approval": True,
        }
        response = self._session.post(f"{self.base_url}/download", json=payload, timeout=10)
        if response.status_code not in (200, 202):
            raise requests.HTTPError(
                f"Surge API request failed ({response.status_code}): {response.text.strip()}",
                response=response,
            )
        data = response.json()
        download_id = data.get("id")
        if not download_id:
            raise ValueError("Surge API did not return a download id")
        return str(download_id)

    def get_status(self, download_id: str) -> Optional[dict[str, Any]]:
        response = self._session.get(
            f"{self.base_url}/download",
            params={"id": download_id},
            timeout=10,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise requests.HTTPError(
                f"Surge API request failed ({response.status_code}): {response.text.strip()}",
                response=response,
            )
        return response.json()

    def delete(self, download_id: str) -> None:
        response = self._session.post(
            f"{self.base_url}/delete",
            params={"id": download_id},
            timeout=10,
        )
        if response.status_code not in (200, 404):
            raise requests.HTTPError(
                f"Surge API request failed ({response.status_code}): {response.text.strip()}",
                response=response,
            )


_manager = _SurgeManager()


def surge(
    urls: Union[str, list[str], dict[str, Any], list[dict[str, Any]]],
    output_dir: Path,
    filename: str,
    headers: Optional[MutableMapping[str, Union[str, bytes]]] = None,
    cookies: Optional[Union[MutableMapping[str, str], CookieJar]] = None,
    proxy: Optional[str] = None,
    max_workers: Optional[int] = None,
) -> Generator[dict[str, Any], None, None]:
    """
    Download files using Surge in headless server mode.
    https://github.com/surge-downloader/Surge

    Yields the following download status updates while chunks are downloading:

    - {total: 1} (download(s) have been queued)
    - {completed: 1024, total: 4096} (bytes downloaded for single-file downloads)
    - {completed: 3, total: 10} (completed file count for segmented batches)
    - {downloaded: "10.1 MB/s"} (current aggregate speed)
    - {file_downloaded: Path(...), written: 1024} (download finished)
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

    if cookies and not isinstance(cookies, CookieJar):
        cookies = cookiejar_from_dict(cookies)

    output_dir.mkdir(parents=True, exist_ok=True)
    _manager.ensure_started(proxy=proxy, max_workers=max_workers)

    if debug_logger:
        first_url = urls[0] if isinstance(urls[0], str) else urls[0].get("url", "")
        url_display = first_url[:200] + "..." if len(first_url) > 200 else first_url
        effective_proxy = proxy or (str(config.surge.get("proxy_url", "") or "").strip() or None)
        debug_logger.log(
            level="DEBUG",
            operation="downloader_surge_start",
            message="Starting Surge download",
            context={
                "binary_path": str(binaries.Surge),
                "url_count": len(urls),
                "first_url": url_display,
                "output_dir": str(output_dir),
                "filename": filename,
                "max_workers": max_workers,
                "has_proxy": bool(effective_proxy),
            },
        )

    tracked_downloads: list[dict[str, Any]] = []

    for i, url in enumerate(urls):
        if isinstance(url, str):
            url_data = {"url": url}
        else:
            url_data = url

        if "url" not in url_data:
            raise ValueError("Each Surge download item must include a 'url'")

        request_headers: dict[str, str] = {}

        for header_name, header_value in (headers or {}).items():
            if header_name.lower() == "accept-encoding":
                continue
            request_headers[header_name] = _stringify_header_value(header_value)

        if cookies:
            mock_request = requests.Request(url=url_data["url"])
            cookie_header = get_cookie_header(cookies, mock_request)
            if cookie_header:
                request_headers["Cookie"] = cookie_header

        for header_name, header_value in (url_data.get("headers") or {}).items():
            if header_name.lower() == "accept-encoding":
                continue
            request_headers[header_name] = _stringify_header_value(header_value)

        mirrors = url_data.get("mirrors") or []
        if mirrors and not isinstance(mirrors, list):
            raise TypeError(f"Expected mirrors to be {list}, not {type(mirrors)}")

        url_filename = str(
            url_data.get(
                "filename",
                filename.format(i=i, ext=get_extension(url_data["url"])),
            )
        )
        save_path = output_dir / url_filename

        download_id = _manager.add_download(
            url=url_data["url"],
            output_dir=output_dir,
            filename=url_filename,
            headers=request_headers,
            mirrors=mirrors,
        )
        tracked_downloads.append(
            {
                "id": download_id,
                "filename": url_filename,
                "save_path": save_path,
            }
        )

    yield dict(total=len(tracked_downloads))

    completed: set[str] = set()

    try:
        while len(completed) < len(tracked_downloads):
            if DOWNLOAD_CANCELLED.is_set():
                for download in tracked_downloads:
                    if download["id"] not in completed:
                        _manager.delete(download["id"])
                yield dict(downloaded="[yellow]CANCELLED")
                raise KeyboardInterrupt()

            total_downloaded = 0
            total_size = 0
            total_speed_mb = 0.0
            has_known_size = False

            for download in tracked_downloads:
                download_id = download["id"]
                if download_id in completed:
                    continue

                status = _manager.get_status(download_id)
                if not status:
                    continue

                downloaded_bytes = int(status.get("downloaded", 0) or 0)
                total_bytes = int(status.get("total_size", 0) or 0)
                total_downloaded += downloaded_bytes
                total_size += total_bytes
                total_speed_mb += float(status.get("speed", 0) or 0)
                has_known_size = has_known_size or total_bytes > 0

                state = (status.get("status") or "").lower()
                if state == "completed":
                    completed.add(download_id)
                    final_path = Path(status.get("dest_path") or download["save_path"])
                    written = downloaded_bytes
                    if not written and final_path.exists():
                        written = final_path.stat().st_size
                    yield dict(file_downloaded=final_path, written=written)
                    yield dict(completed=len(completed))
                elif state == "error":
                    error = status.get("error") or "Unknown error"
                    raise ValueError(f"[Surge]: Download Error (#{download_id}): {error}")

            progress_data: dict[str, Any] = {"advance": 0}

            if len(tracked_downloads) > 1:
                progress_data["completed"] = len(completed)
                progress_data["total"] = len(tracked_downloads)
            else:
                progress_data["completed"] = total_downloaded
                progress_data["total"] = total_size if has_known_size else None

            if total_speed_mb > 0:
                total_speed_bytes = int(total_speed_mb * (1024**2))
                progress_data["downloaded"] = f"{filesize.decimal(total_speed_bytes)}/s"

            yield progress_data
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
                operation="downloader_surge_exception",
                message=f"Unexpected error during Surge download: {e}",
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
            operation="downloader_surge_complete",
            message="Surge download completed successfully",
            context={
                "url_count": len(tracked_downloads),
                "output_dir": str(output_dir),
                "filename": filename,
            },
        )


__all__ = ("surge",)
