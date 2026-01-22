import os
import re
import subprocess
import warnings
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Generator, MutableMapping

import requests
from requests.cookies import cookiejar_from_dict, get_cookie_header

from unshackle.core import binaries
from unshackle.core.binaries import FFMPEG, ShakaPackager, Mp4decrypt
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.utilities import get_debug_logger

PERCENT_RE = re.compile(r"(\d+\.\d+%)")
SPEED_RE = re.compile(r"(\d+\.\d+(?:MB|KB)ps)")
SIZE_RE = re.compile(r"(\d+\.\d+(?:MB|GB|KB)/\d+\.\d+(?:MB|GB|KB))")
WARN_RE = re.compile(r"(WARN : Response.*|WARN : One or more errors occurred.*)")
ERROR_RE = re.compile(r"(\bERROR\b.*|\bFAILED\b.*|\bException\b.*)")

DECRYPTION_ENGINE = {
    "shaka": "SHAKA_PACKAGER",
    "mp4decrypt": "MP4DECRYPT",
}

# Ignore FutureWarnings
warnings.simplefilter(action="ignore", category=FutureWarning)


def get_track_selection_args(track: Any) -> list[str]:
    """
    Generates track selection arguments for N_m3u8dl_RE.

    Args:
        track: A track object with attributes like descriptor, data, and class name.

    Returns:
        A list of strings for track selection.

    Raises:
        ValueError: If the manifest type is unsupported or track selection fails.
    """
    descriptor = track.descriptor.name
    track_type = track.__class__.__name__

    def _create_args(flag: str, parts: list[str], type_str: str, extra_args: list[str] | None = None) -> list[str]:
        if not parts:
            raise ValueError(f"[N_m3u8DL-RE]: Unable to select {type_str} track from {descriptor} manifest")

        final_args = [flag, ":".join(parts)]
        if extra_args:
            final_args.extend(extra_args)

        return final_args

    match descriptor:
        case "HLS":
            # HLS playlists are direct inputs; no selection arguments needed.
            return []

        case "DASH":
            representation = track.data.get("dash", {}).get("representation", {})
            adaptation_set = track.data.get("dash", {}).get("adaptation_set", {})
            parts = []

            if track_type == "Audio":
                track_id = representation.get("id") or adaptation_set.get("audioTrackId")
                lang = representation.get("lang") or adaptation_set.get("lang")

                if track_id:
                    parts.append(rf'"id=\b{track_id}\b"')
                    if lang:
                        parts.append(f"lang={lang}")
                else:
                    if codecs := representation.get("codecs"):
                        parts.append(f"codecs={codecs}")
                    if lang:
                        parts.append(f"lang={lang}")
                    if bw := representation.get("bandwidth"):
                        bitrate = int(bw) // 1000
                        parts.append(f"bwMin={bitrate}:bwMax={bitrate + 5}")
                    if roles := representation.findall("Role") + adaptation_set.findall("Role"):
                        if role := next((r.get("value") for r in roles if r.get("value", "").lower() == "main"), None):
                            parts.append(f"role={role}")
                return _create_args("-sa", parts, "audio")

            if track_type == "Video":
                if track_id := representation.get("id"):
                    parts.append(rf'"id=\b{track_id}\b"')
                else:
                    if width := representation.get("width"):
                        parts.append(f"res={width}*")
                    if codecs := representation.get("codecs"):
                        parts.append(f"codecs={codecs}")
                    if bw := representation.get("bandwidth"):
                        bitrate = int(bw) // 1000
                        parts.append(f"bwMin={bitrate}:bwMax={bitrate + 5}")
                return _create_args("-sv", parts, "video")

            if track_type == "Subtitle":
                if track_id := representation.get("id"):
                    parts.append(rf'"id=\b{track_id}\b"')
                else:
                    if lang := representation.get("lang"):
                        parts.append(f"lang={lang}")
                return _create_args("-ss", parts, "subtitle", extra_args=["--auto-subtitle-fix", "false"])

        case "ISM":
            quality_level = track.data.get("ism", {}).get("quality_level", {})
            stream_index = track.data.get("ism", {}).get("stream_index", {})
            parts = []

            if track_type == "Audio":
                if name := stream_index.get("Name") or quality_level.get("Index"):
                    parts.append(rf'"id=\b{name}\b"')
                else:
                    if codecs := quality_level.get("FourCC"):
                        parts.append(f"codecs={codecs}")
                    if lang := stream_index.get("Language"):
                        parts.append(f"lang={lang}")
                    if br := quality_level.get("Bitrate"):
                        bitrate = int(br) // 1000
                        parts.append(f"bwMin={bitrate}:bwMax={bitrate + 5}")
                return _create_args("-sa", parts, "audio")

            if track_type == "Video":
                if name := stream_index.get("Name") or quality_level.get("Index"):
                    parts.append(rf'"id=\b{name}\b"')
                else:
                    if width := quality_level.get("MaxWidth"):
                        parts.append(f"res={width}*")
                    if codecs := quality_level.get("FourCC"):
                        parts.append(f"codecs={codecs}")
                    if br := quality_level.get("Bitrate"):
                        bitrate = int(br) // 1000
                        parts.append(f"bwMin={bitrate}:bwMax={bitrate + 5}")
                return _create_args("-sv", parts, "video")

            # I've yet to encounter a subtitle track in ISM manifests, so this is mostly theoretical.
            if track_type == "Subtitle":
                if name := stream_index.get("Name") or quality_level.get("Index"):
                    parts.append(rf'"id=\b{name}\b"')
                else:
                    if lang := stream_index.get("Language"):
                        parts.append(f"lang={lang}")
                return _create_args("-ss", parts, "subtitle", extra_args=["--auto-subtitle-fix", "false"])

        case "URL":
            raise ValueError(
                f"[N_m3u8DL-RE]: Direct URL downloads are not supported for {track_type} tracks. "
                f"The track should use a different downloader (e.g., 'requests', 'aria2c')."
            )

    raise ValueError(f"[N_m3u8DL-RE]: Unsupported manifest type: {descriptor}")


def build_download_args(
    track_url: str,
    filename: str,
    output_dir: Path,
    thread_count: int,
    retry_count: int,
    track_from_file: Path | None,
    custom_args: dict[str, Any] | None,
    headers: dict[str, Any] | None,
    cookies: CookieJar | None,
    proxy: str | None,
    content_keys: dict[str, str] | None,
    ad_keyword: str | None,
    skip_merge: bool | None = False,
) -> list[str]:
    """Constructs the CLI arguments for N_m3u8DL-RE."""

    # Default arguments
    args = {
        "--save-name": filename,
        "--save-dir": output_dir,
        "--tmp-dir": output_dir,
        "--thread-count": thread_count,
        "--download-retry-count": retry_count,
        "--write-meta-json": False,
    }
    if FFMPEG:
        args["--ffmpeg-binary-path"] = str(FFMPEG)
    if proxy:
        args["--custom-proxy"] = proxy
    if skip_merge:
        args["--skip-merge"] = skip_merge
    if ad_keyword:
        args["--ad-keyword"] = ad_keyword

    if content_keys:
        args["--key"] = next((f"{kid.hex}:{key.lower()}" for kid, key in content_keys.items()), None)

        decryption_config = config.decryption.lower()
        engine_name = DECRYPTION_ENGINE.get(decryption_config) or "SHAKA_PACKAGER"
        args["--decryption-engine"] = engine_name

        binary_path = None
        if engine_name == "SHAKA_PACKAGER":
            if ShakaPackager:
                binary_path = str(ShakaPackager)
        elif engine_name == "MP4DECRYPT":
            if Mp4decrypt:
                binary_path = str(Mp4decrypt)
        if binary_path:
            args["--decryption-binary-path"] = binary_path

    if custom_args:
        args.update(custom_args)

    command = [track_from_file or track_url]
    for flag, value in args.items():
        if value is True:
            command.append(flag)
        elif value is False:
            command.extend([flag, "false"])
        elif value is not False and value is not None:
            command.extend([flag, str(value)])

    if headers:
        for key, value in headers.items():
            if key.lower() not in ("accept-encoding", "cookie"):
                command.extend(["--header", f"{key}: {value}"])

    if cookies:
        req = requests.Request(method="GET", url=track_url)
        cookie_header = get_cookie_header(cookies, req)
        command.extend(["--header", f"Cookie: {cookie_header}"])

    return command


def download(
    urls: str | dict[str, Any] | list[str | dict[str, Any]],
    track: Any,
    output_dir: Path,
    filename: str,
    headers: MutableMapping[str, str | bytes] | None,
    cookies: MutableMapping[str, str] | CookieJar | None,
    proxy: str | None,
    max_workers: int | None,
    content_keys: dict[str, Any] | None,
    skip_merge: bool | None = False,
) -> Generator[dict[str, Any], None, None]:
    debug_logger = get_debug_logger()

    if not urls:
        raise ValueError("urls must be provided and not empty")
    if not isinstance(urls, (str, dict, list)):
        raise TypeError(f"Expected urls to be str, dict, or list, not {type(urls)}")
    if not isinstance(output_dir, Path):
        raise TypeError(f"Expected output_dir to be Path, not {type(output_dir)}")
    if not isinstance(filename, str) or not filename:
        raise ValueError("filename must be a non-empty string")
    if not isinstance(headers, (MutableMapping, type(None))):
        raise TypeError(f"Expected headers to be a mapping or None, not {type(headers)}")
    if not isinstance(cookies, (MutableMapping, CookieJar, type(None))):
        raise TypeError(f"Expected cookies to be a mapping, CookieJar, or None, not {type(cookies)}")
    if not isinstance(proxy, (str, type(None))):
        raise TypeError(f"Expected proxy to be a str or None, not {type(proxy)}")
    if not isinstance(max_workers, (int, type(None))):
        raise TypeError(f"Expected max_workers to be an int or None, not {type(max_workers)}")
    if not isinstance(content_keys, (dict, type(None))):
        raise TypeError(f"Expected content_keys to be a dict or None, not {type(content_keys)}")
    if not isinstance(skip_merge, (bool, type(None))):
        raise TypeError(f"Expected skip_merge to be a bool or None, not {type(skip_merge)}")

    if cookies and not isinstance(cookies, CookieJar):
        cookies = cookiejar_from_dict(cookies)

    if not binaries.N_m3u8DL_RE:
        raise EnvironmentError("N_m3u8DL-RE executable not found...")

    effective_max_workers = max_workers or min(32, (os.cpu_count() or 1) + 4)

    if proxy and not config.n_m3u8dl_re.get("use_proxy", True):
        proxy = None

    thread_count = config.n_m3u8dl_re.get("thread_count", effective_max_workers)
    retry_count = config.n_m3u8dl_re.get("retry_count", 10)
    ad_keyword = config.n_m3u8dl_re.get("ad_keyword")

    arguments = build_download_args(
        track_url=track.url,
        track_from_file=track.from_file,
        filename=filename,
        output_dir=output_dir,
        thread_count=thread_count,
        retry_count=retry_count,
        custom_args=track.downloader_args,
        headers=headers,
        cookies=cookies,
        proxy=proxy,
        content_keys=content_keys,
        skip_merge=skip_merge,
        ad_keyword=ad_keyword,
    )
    selection_args = get_track_selection_args(track)
    arguments.extend(selection_args)

    log_file_path: Path | None = None
    if debug_logger:
        log_file_path = output_dir / f".n_m3u8dl_re_{filename}.log"
        arguments.extend(["--log-file-path", str(log_file_path)])

        track_url_display = track.url[:200] + "..." if len(track.url) > 200 else track.url
        debug_logger.log(
            level="DEBUG",
            operation="downloader_n_m3u8dl_re_start",
            message="Starting N_m3u8DL-RE download",
            context={
                "binary_path": str(binaries.N_m3u8DL_RE),
                "track_id": getattr(track, "id", None),
                "track_type": track.__class__.__name__,
                "track_url": track_url_display,
                "output_dir": str(output_dir),
                "filename": filename,
                "thread_count": thread_count,
                "retry_count": retry_count,
                "has_content_keys": bool(content_keys),
                "content_key_count": len(content_keys) if content_keys else 0,
                "has_proxy": bool(proxy),
                "skip_merge": skip_merge,
                "has_custom_args": bool(track.downloader_args),
                "selection_args": selection_args,
                "descriptor": track.descriptor.name if hasattr(track, "descriptor") else None,
            },
        )
    else:
        arguments.extend(["--no-log", "true"])

    yield {"total": 100}
    yield {"downloaded": "Parsing streams..."}

    try:
        with subprocess.Popen(
            [binaries.N_m3u8DL_RE, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        ) as process:
            last_line = ""
            track_type = track.__class__.__name__

            for line in process.stdout:
                output = line.strip()
                if not output:
                    continue
                last_line = output

                if warn_match := WARN_RE.search(output):
                    console.log(f"{track_type} {warn_match.group(1)}")
                    continue

                if speed_match := SPEED_RE.search(output):
                    size = size_match.group(1) if (size_match := SIZE_RE.search(output)) else ""
                    yield {"downloaded": f"{speed_match.group(1)} {size}"}

                if percent_match := PERCENT_RE.search(output):
                    progress = int(percent_match.group(1).split(".", 1)[0])
                    yield {"completed": progress} if progress < 100 else {"downloaded": "Merging"}

            process.wait()

        if process.returncode != 0:
            if debug_logger and log_file_path:
                log_contents = ""
                if log_file_path.exists():
                    try:
                        log_contents = log_file_path.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        log_contents = "<failed to read log file>"

                debug_logger.log(
                    level="ERROR",
                    operation="downloader_n_m3u8dl_re_failed",
                    message=f"N_m3u8DL-RE exited with code {process.returncode}",
                    context={
                        "returncode": process.returncode,
                        "track_id": getattr(track, "id", None),
                        "track_type": track.__class__.__name__,
                        "last_line": last_line,
                        "log_file_contents": log_contents,
                    },
                )
            if error_match := ERROR_RE.search(last_line):
                raise ValueError(f"[N_m3u8DL-RE]: {error_match.group(1)}")
            raise subprocess.CalledProcessError(process.returncode, arguments)

        if debug_logger:
            debug_logger.log(
                level="DEBUG",
                operation="downloader_n_m3u8dl_re_complete",
                message="N_m3u8DL-RE download completed successfully",
                context={
                    "track_id": getattr(track, "id", None),
                    "track_type": track.__class__.__name__,
                    "output_dir": str(output_dir),
                    "filename": filename,
                },
            )

    except ConnectionResetError:
        # interrupted while passing URI to download
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        DOWNLOAD_CANCELLED.set()  # skip pending track downloads
        yield {"downloaded": "[yellow]CANCELLED"}
        raise
    except Exception as e:
        DOWNLOAD_CANCELLED.set()  # skip pending track downloads
        yield {"downloaded": "[red]FAILED"}
        if debug_logger and log_file_path and not isinstance(e, (subprocess.CalledProcessError, ValueError)):
            log_contents = ""
            if log_file_path.exists():
                try:
                    log_contents = log_file_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    log_contents = "<failed to read log file>"

            debug_logger.log(
                level="ERROR",
                operation="downloader_n_m3u8dl_re_exception",
                message=f"Unexpected error during N_m3u8DL-RE download: {e}",
                error=e,
                context={
                    "track_id": getattr(track, "id", None),
                    "track_type": track.__class__.__name__,
                    "log_file_contents": log_contents,
                },
            )
        raise
    finally:
        if log_file_path and log_file_path.exists():
            try:
                log_file_path.unlink()
            except Exception:
                pass


def n_m3u8dl_re(
    urls: str | list[str] | dict[str, Any] | list[dict[str, Any]],
    track: Any,
    output_dir: Path,
    filename: str,
    headers: MutableMapping[str, str | bytes] | None = None,
    cookies: MutableMapping[str, str] | CookieJar | None = None,
    proxy: str | None = None,
    max_workers: int | None = None,
    content_keys: dict[str, Any] | None = None,
    skip_merge: bool | None = False,
) -> Generator[dict[str, Any], None, None]:
    """
    Download files using N_m3u8DL-RE.
    https://github.com/nilaoda/N_m3u8DL-RE

    Yields the following download status updates while chunks are downloading:

    - {total: 100} (100% download total)
    - {completed: 1} (1% download progress out of 100%)
    - {downloaded: "10.1 MB/s"} (currently downloading at a rate of 10.1 MB/s)

    The data is in the same format accepted by rich's progress.update() function.

    Parameters:
        urls: Web URL(s) to file(s) to download. NOTE: This parameter is ignored for now.
        track: The track to download. Used to get track attributes for the selection
            process. Note that Track.Descriptor.URL is not supported by N_m3u8DL-RE.
        output_dir: The folder to save the file into. If the save path's directory does
            not exist then it will be made automatically.
        filename: The filename or filename template to use for each file.
        headers: A mapping of HTTP Header Key/Values to use for all downloads.
        cookies: A mapping of Cookie Key/Values or a Cookie Jar to use for all downloads.
        proxy: A proxy to use for all downloads.
        max_workers: The maximum amount of threads to use for downloads. Defaults to
            min(32,(cpu_count+4)). Can be set in config with --thread-count option.
        content_keys: The content keys to use for decryption.
        skip_merge: Whether to skip merging the downloaded chunks.
    """

    yield from download(
        urls=urls,
        track=track,
        output_dir=output_dir,
        filename=filename,
        headers=headers,
        cookies=cookies,
        proxy=proxy,
        max_workers=max_workers,
        content_keys=content_keys,
        skip_merge=skip_merge,
    )


__all__ = ("n_m3u8dl_re",)
