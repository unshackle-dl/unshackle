import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from unshackle.core.api.sanitize import sanitize_log
from unshackle.core.utils.redact import REDACTED, URL_USERINFO_RE, redact_text

log = logging.getLogger("download_manager")


# Job parameters may carry secrets (a raw "user:pass" credential, a proxy URL with embedded
# userinfo). These must never leave the process via the API or logs, so they are masked
# wherever parameters are serialized for a response.
_SENSITIVE_PARAM_KEYS = ("credential", "credentials", "password", "token", "api_key")


def _redact_parameters(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of job parameters with secrets masked, safe to serialize."""
    if not isinstance(parameters, dict):
        return parameters
    redacted = dict(parameters)
    for key in _SENSITIVE_PARAM_KEYS:
        if redacted.get(key):
            redacted[key] = REDACTED
    proxy = redacted.get("proxy")
    if isinstance(proxy, str) and "@" in proxy:
        redacted["proxy"] = URL_USERINFO_RE.sub(f"{REDACTED}@", proxy)
    return redacted


def _secret_values(parameters: Dict[str, Any]) -> List[str]:
    """Raw secret strings carried in job parameters, longest first, for scrubbing free text."""
    if not isinstance(parameters, dict):
        return []
    secrets: List[str] = []
    for key in ("credential", "password", "token", "api_key"):
        value = parameters.get(key)
        if isinstance(value, str) and value:
            secrets.append(value)
            if key == "credential" and ":" in value:
                password = value.split(":", 1)[1]
                if len(password) >= 4:  # short passwords would blanket-replace and garble the text
                    secrets.append(password)
    creds = parameters.get("credentials")
    if isinstance(creds, dict):
        secrets.extend(v for v in creds.values() if isinstance(v, str) and v)
    elif isinstance(creds, str) and creds:
        secrets.append(creds)
    return sorted(set(secrets), key=len, reverse=True)  # longest first so substrings don't survive


def _redact_text(text: Optional[str], parameters: Dict[str, Any]) -> Optional[str]:
    """Mask proxy userinfo and any known parameter secrets that leaked into a free-text field
    (error message / details / traceback / worker stderr) before it is returned via the API."""
    return redact_text(text, _secret_values(parameters))


class JobStatus(Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DownloadJob:
    """Represents a download job with all its parameters and status."""

    job_id: str
    status: JobStatus
    created_time: datetime
    service: str
    title_id: str
    parameters: Dict[str, Any]

    # Progress tracking
    started_time: Optional[datetime] = None
    completed_time: Optional[datetime] = None
    progress: float = 0.0

    # Results and error info
    output_files: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
    error_details: Optional[str] = None
    error_code: Optional[str] = None
    error_traceback: Optional[str] = None
    worker_stderr: Optional[str] = None

    # Current phase, track counts, and labels of the tracks downloading now.
    phase: Optional[str] = None
    completed_tracks: int = 0
    total_tracks: int = 0
    active_tracks: List[str] = field(default_factory=list)

    # Subtitles skipped under skip_subtitle_errors (non-fatal). Each entry is a dl.SkippedSubtitle
    # dict (id / language / title) so a client can report which weren't available.
    skipped_subtitles: List[Dict[str, Any]] = field(default_factory=list)

    # Cancellation support
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self, include_full_details: bool = False) -> Dict[str, Any]:
        """Convert job to dictionary for JSON response."""
        result = {
            "job_id": self.job_id,
            "status": self.status.value,
            "created_time": self.created_time.isoformat(),
            "service": self.service,
            "title_id": self.title_id,
            "progress": self.progress,
            "phase": self.phase,
            "completed_tracks": self.completed_tracks,
            "total_tracks": self.total_tracks,
            "active_tracks": self.active_tracks,
            "skipped_subtitles": self.skipped_subtitles,
        }

        if include_full_details:
            # Error/stderr/traceback are free text a service may have echoed a credential or proxy
            # URL into, so scrub them with the same secrets that _redact_parameters masks.
            result.update(
                {
                    "parameters": _redact_parameters(self.parameters),
                    "started_time": self.started_time.isoformat() if self.started_time else None,
                    "completed_time": self.completed_time.isoformat() if self.completed_time else None,
                    "output_files": self.output_files,
                    "error_message": _redact_text(self.error_message, self.parameters),
                    "error_details": _redact_text(self.error_details, self.parameters),
                    "error_code": self.error_code,
                    "error_traceback": _redact_text(self.error_traceback, self.parameters),
                    "worker_stderr": _redact_text(self.worker_stderr, self.parameters),
                }
            )

        return result


def _perform_download(
    job_id: str,
    service: str,
    title_id: str,
    params: Dict[str, Any],
    cancel_event: Optional[threading.Event] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[str]:
    """Execute the synchronous download logic for a job."""

    def _check_cancel(stage: str):
        if cancel_event and cancel_event.is_set():
            raise Exception(f"Job was cancelled {stage}")

    from contextlib import redirect_stderr, redirect_stdout
    from io import StringIO

    _check_cancel("before execution started")

    # Import dl.py components lazily to avoid circular deps during module import
    import click
    import yaml

    from unshackle.commands.dl import dl
    from unshackle.core.api.errors import APIError, APIErrorCode
    from unshackle.core.config import config
    from unshackle.core.services import Services
    from unshackle.core.tracks import Subtitle, Video
    from unshackle.core.utils.click_types import ContextData
    from unshackle.core.utils.collections import merge_dict

    log.info(f"Starting sync download for job {job_id}")

    # A service caches tokens under cache/<Service>/, keyed by service name only, so two jobs on
    # one service with different credentials would share a cache. When a per-job credential is set,
    # namespace the cache dir by a hash of it so the sessions can't cross.
    job_credential = params.get("credential")
    if job_credential:
        import hashlib

        cred_hash = hashlib.sha256(job_credential.encode("utf-8")).hexdigest()[:12]
        config.directories.cache = config.directories.cache / "_jobs" / cred_hash

    # Convert string parameters to enums (API receives strings, dl.result() expects enums)
    vcodec_raw = params.get("vcodec")
    if vcodec_raw:
        if isinstance(vcodec_raw, str):
            vcodec_raw = [vcodec_raw]
        if isinstance(vcodec_raw, list) and vcodec_raw and not isinstance(vcodec_raw[0], Video.Codec):
            codec_map = {c.name.upper(): c for c in Video.Codec}
            codec_map.update({c.value.upper(): c for c in Video.Codec})
            params["vcodec"] = [codec_map[v.upper()] for v in vcodec_raw if v.upper() in codec_map]
    else:
        params["vcodec"] = []

    range_raw = params.get("range")
    if range_raw:
        if isinstance(range_raw, str):
            range_raw = [range_raw]
        if isinstance(range_raw, list) and range_raw and not isinstance(range_raw[0], Video.Range):
            range_map = {r.name.upper(): r for r in Video.Range}
            range_map.update({r.value.upper(): r for r in Video.Range})
            params["range"] = [range_map[r.upper()] for r in range_raw if r.upper() in range_map]
    else:
        params["range"] = [Video.Range.SDR]

    sub_format_raw = params.get("sub_format")
    if sub_format_raw and isinstance(sub_format_raw, str):
        sub_map = {c.name.upper(): c for c in Subtitle.Codec}
        sub_map.update({c.value.upper(): c for c in Subtitle.Codec})
        params["sub_format"] = sub_map.get(sub_format_raw.upper())

    if params.get("export"):
        params["export"] = bool(params["export"])

    # Normalize slow: accept string "MIN-MAX", list/tuple, or True (default 60-120)
    slow_raw = params.get("slow")
    if slow_raw is not None and not isinstance(slow_raw, tuple):
        if isinstance(slow_raw, bool):
            params["slow"] = (60, 120) if slow_raw else None
        elif isinstance(slow_raw, list) and len(slow_raw) == 2:
            params["slow"] = (int(slow_raw[0]), int(slow_raw[1]))
        elif isinstance(slow_raw, str):
            from unshackle.core.utils.click_types import SLOW_DELAY_RANGE

            try:
                params["slow"] = SLOW_DELAY_RANGE.convert(slow_raw, None, None)
            except click.BadParameter as exc:
                raise Exception(f"Invalid slow parameter: {exc}")

    # Convert wanted episode strings to internal "SxE" format
    # Accepts: "S01E01", "S01-S03", "s1e1", "1x1", or already-parsed format
    wanted_raw = params.get("wanted")
    if wanted_raw:
        from unshackle.core.utils.click_types import SeasonRange

        if isinstance(wanted_raw, str):
            wanted_raw = [wanted_raw]
        # Only convert if not already in internal "SxE" format
        needs_conversion = any(not re.match(r"^\d+x\d+$", w) for w in wanted_raw)
        if needs_conversion:
            season_range = SeasonRange()
            params["wanted"] = season_range.parse_tokens(*wanted_raw)

    # Load service configuration
    service_config_path = Services.get_path(service) / config.filenames.config
    if service_config_path.exists():
        service_config = yaml.safe_load(service_config_path.read_text(encoding="utf8"))
    else:
        service_config = {}
    merge_dict(config.services.get(service), service_config)

    from unshackle.commands.dl import dl as dl_command

    ctx = click.Context(dl_command.cli)
    ctx.invoked_subcommand = service
    from unshackle.core.api.handlers import load_full_cdm

    cdm = load_full_cdm(service, params.get("profile"), params.get("cdm_type"))
    ctx.obj = ContextData(config=service_config, cdm=cdm, proxy_providers=[], profile=params.get("profile"))
    ctx.params = {
        "proxy": params.get("proxy"),
        "no_proxy": params.get("no_proxy", False),
        "no_proxy_download": params.get("no_proxy_download", False),
        "profile": params.get("profile"),
        "repack": params.get("repack", False),
        "tag": params.get("tag"),
        "tmdb_id": params.get("tmdb_id"),
        "imdb_id": params.get("imdb_id"),
        "animeapi_id": params.get("animeapi_id"),
        "enrich": params.get("enrich", False),
        "output_dir": Path(params["output_dir"]) if params.get("output_dir") else None,
        "no_cache": params.get("no_cache", False),
        "reset_cache": params.get("reset_cache", False),
    }
    # Hand-built context: record parameter sources so service dl overrides
    # apply to defaults but never clobber client-sent values.
    from click.core import ParameterSource

    for param_name in ctx.params:
        ctx.set_parameter_source(
            param_name, ParameterSource.COMMANDLINE if param_name in params else ParameterSource.DEFAULT
        )

    dl_instance = dl(
        ctx=ctx,
        no_proxy=params.get("no_proxy", False),
        profile=params.get("profile"),
        proxy=params.get("proxy"),
        repack=params.get("repack", False),
        tag=params.get("tag"),
        tmdb_id=params.get("tmdb_id"),
        imdb_id=params.get("imdb_id"),
        animeapi_id=params.get("animeapi_id"),
        enrich=params.get("enrich", False),
        output_dir=Path(params["output_dir"]) if params.get("output_dir") else None,
    )
    # Per-request CDM override (a device name in the WVDs dir); get_cdm() takes it first.
    if params.get("cdm"):
        dl_instance.cdm_override = params["cdm"]

    # Per-request credential ("user:pass"); feed it into the map get_credentials() reads so a
    # client can authenticate without anything being persisted to disk. Without a profile,
    # get_credentials() falls back to "default", so store it there too rather than dropping it
    # (which would silently authenticate as the server's own default account).
    if params.get("credential"):
        svc_creds = config.credentials.get(service)
        if not isinstance(svc_creds, dict):
            config.credentials[service] = svc_creds = {}
        svc_creds[params.get("profile") or "default"] = params["credential"]

    service_module = Services.load(service)

    _check_cancel("before service instantiation")

    try:
        import inspect

        service_init_params = inspect.signature(service_module.__init__).parameters

        service_ctx = click.Context(click.Command(service))
        service_ctx.parent = ctx
        service_ctx.obj = ctx.obj

        service_kwargs = {}

        if "title" in service_init_params:
            service_kwargs["title"] = title_id

        for key, value in params.items():
            if key in service_init_params and key not in ["service", "title_id"]:
                service_kwargs[key] = value

        for param_name, param_info in service_init_params.items():
            if param_name not in service_kwargs and param_name not in ["self", "ctx"]:
                if param_info.default is inspect.Parameter.empty:
                    if param_name == "movie":
                        service_kwargs[param_name] = "/movies/" in title_id
                    elif param_name == "meta_lang":
                        service_kwargs[param_name] = None
                    else:
                        log.warning(f"Unknown required parameter '{param_name}' for service {service}, using None")
                        service_kwargs[param_name] = None

        service_instance = service_module(service_ctx, **service_kwargs)

    except Exception as exc:  # noqa: BLE001 - propagate meaningful failure
        log.error(f"Failed to create service instance: {exc}")
        raise

    original_download_dir = config.directories.downloads

    _check_cancel("before download execution")

    stdout_capture = StringIO()
    stderr_capture = StringIO()

    # The progress_sink (dl.build_job_progress_callables) owns the percentage; status changes
    # are emitted here.
    if progress_callback:
        progress_callback({"progress": 0.0, "status": "starting"})
        original_result = dl_instance.result

        def result_with_progress(*args, **kwargs):
            try:
                progress_callback({"status": "downloading"})
                result = original_result(*args, **kwargs)
                progress_callback({"progress": 100.0, "status": "completed"})
                return result
            except Exception as e:
                progress_callback({"progress": 0.0, "status": "failed", "error": str(e)})
                raise

        dl_instance.result = result_with_progress

    try:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            dl_instance.result(
                service=service_instance,
                quality=params.get("quality", []),
                vcodec=params.get("vcodec", []),
                acodec=params.get("acodec"),
                vbitrate=params.get("vbitrate"),
                abitrate=params.get("abitrate"),
                vbitrate_range=params.get("vbitrate_range"),
                abitrate_range=params.get("abitrate_range"),
                range_=params.get("range", ["SDR"]),
                channels=params.get("channels"),
                no_atmos=params.get("no_atmos", False),
                select_titles=False,
                wanted=params.get("wanted", []),
                latest_episode=params.get("latest_episode", False),
                lang=params.get("lang", ["orig"]),
                v_lang=params.get("v_lang", []),
                a_lang=params.get("a_lang", []),
                s_lang=params.get("s_lang", ["all"]),
                require_subs=params.get("require_subs", []),
                forced_subs=params.get("forced_subs", False),
                exact_lang=params.get("exact_lang", False),
                sub_format=params.get("sub_format"),
                video_only=params.get("video_only", False),
                audio_only=params.get("audio_only", False),
                subs_only=params.get("subs_only", False),
                chapters_only=params.get("chapters_only", False),
                no_subs=params.get("no_subs", False),
                skip_subtitle_errors=params.get("skip_subtitle_errors", False),
                no_audio=params.get("no_audio", False),
                no_chapters=params.get("no_chapters", False),
                no_video=params.get("no_video", False),
                audio_description=params.get("audio_description", False),
                slow=params.get("slow", None),
                list_=False,
                list_titles=False,
                skip_dl=params.get("skip_dl", False),
                export=params.get("export"),
                cdm_only=params.get("cdm_only"),
                no_proxy=params.get("no_proxy", False),
                no_proxy_download=params.get("no_proxy_download", False),
                no_folder=params.get("no_folder", False),
                no_source=params.get("no_source", False),
                no_mux=params.get("no_mux", False),
                workers=params.get("workers"),
                downloads=params.get("downloads", 1),
                worst=params.get("worst", False),
                best_available=params.get("best_available", False),
                split_audio=params.get("split_audio"),
                progress_sink=progress_callback,
            )

    except SystemExit as exc:
        if exc.code != 0:
            stdout_str = stdout_capture.getvalue()
            stderr_str = stderr_capture.getvalue()
            log.error(f"Download exited with code {exc.code}")
            log.error(f"Stdout: {stdout_str}")
            log.error(f"Stderr: {stderr_str}")
            raise APIError(APIErrorCode.DOWNLOAD_ERROR, f"Download failed with exit code {exc.code}")

    except Exception as exc:  # noqa: BLE001 - propagate to caller
        stdout_str = stdout_capture.getvalue()
        stderr_str = stderr_capture.getvalue()
        log.error(f"Download execution failed: {exc}")
        log.error(f"Stdout: {stdout_str}")
        log.error(f"Stderr: {stderr_str}")
        raise

    # dl.result() catches a download-worker exception, reports it, but returns normally (exit 0).
    # It sets download_failed in that case, so the job isn't reported as completed with no output.
    if getattr(dl_instance, "download_failed", False):
        detail = (stdout_capture.getvalue() + stderr_capture.getvalue())[-200:].strip()
        raise APIError(APIErrorCode.WORKER_ERROR, "download worker failed: " + (detail or "see logs"))

    # Surface any subtitles that were skipped (non-fatal failures) so the client can report them.
    if progress_callback:
        skipped_subs = getattr(dl_instance, "skipped_subtitles", None)
        if skipped_subs:
            progress_callback({"skipped_subtitles": list(skipped_subs)})

    output_files = [str(p) for p in dl_instance.completed_files]
    log.info(f"Download completed for job {job_id}, {len(output_files)} file(s) in {original_download_dir}")

    return output_files


class DownloadQueueManager:
    """Manages download job queue with configurable concurrency limits."""

    def __init__(self, max_concurrent_downloads: int = 2, job_retention_hours: int = 24):
        self.max_concurrent_downloads = max_concurrent_downloads
        self.job_retention_hours = job_retention_hours

        self._jobs: Dict[str, DownloadJob] = {}
        self._job_queue: asyncio.Queue = asyncio.Queue()
        self._active_downloads: Dict[str, asyncio.Task] = {}
        self._download_processes: Dict[str, asyncio.subprocess.Process] = {}
        self._job_temp_files: Dict[str, Dict[str, str]] = {}
        self._workers_started = False
        self._shutdown_event = asyncio.Event()

        log.info(
            f"Initialized download queue manager: max_concurrent={max_concurrent_downloads}, retention_hours={job_retention_hours}"
        )

    def create_job(self, service: str, title_id: str, **parameters) -> DownloadJob:
        """Create a new download job and add it to the queue."""
        job_id = str(uuid.uuid4())
        job = DownloadJob(
            job_id=job_id,
            status=JobStatus.QUEUED,
            created_time=datetime.now(),
            service=service,
            title_id=title_id,
            parameters=parameters,
        )

        self._jobs[job_id] = job
        self._job_queue.put_nowait(job)

        log.info(f"Created download job {job_id} for {sanitize_log(service)}:{sanitize_log(title_id)}")
        return job

    def get_job(self, job_id: str) -> Optional[DownloadJob]:
        """Get job by ID."""
        return self._jobs.get(job_id)

    def list_jobs(self) -> List[DownloadJob]:
        """List all jobs."""
        return list(self._jobs.values())

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a job if it's queued or downloading."""
        job = self._jobs.get(job_id)
        if not job:
            return False

        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.CANCELLED
            job.cancel_event.set()  # Signal cancellation
            log.info(f"Cancelled queued job {sanitize_log(job_id)}")
            return True
        elif job.status == JobStatus.DOWNLOADING:
            # Set the cancellation event first - this will be checked by the download thread
            job.cancel_event.set()
            job.status = JobStatus.CANCELLED
            log.info(f"Signaled cancellation for downloading job {sanitize_log(job_id)}")

            # Cancel the active download task
            task = self._active_downloads.get(job_id)
            if task:
                task.cancel()
                log.info(f"Cancelled download task for job {sanitize_log(job_id)}")

            process = self._download_processes.get(job_id)
            if process:
                try:
                    process.terminate()
                    log.info(f"Terminated worker process for job {sanitize_log(job_id)}")
                except ProcessLookupError:
                    log.debug(f"Worker process for job {sanitize_log(job_id)} already exited")

            return True

        return False

    def cleanup_old_jobs(self) -> int:
        """Remove jobs older than retention period."""
        cutoff_time = datetime.now() - timedelta(hours=self.job_retention_hours)
        jobs_to_remove = []

        for job_id, job in self._jobs.items():
            if job.status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
                if job.completed_time and job.completed_time < cutoff_time:
                    jobs_to_remove.append(job_id)
                elif not job.completed_time and job.created_time < cutoff_time:
                    jobs_to_remove.append(job_id)

        for job_id in jobs_to_remove:
            del self._jobs[job_id]

        if jobs_to_remove:
            log.info(f"Cleaned up {len(jobs_to_remove)} old jobs")

        return len(jobs_to_remove)

    async def start_workers(self):
        """Start worker tasks to process the download queue."""
        if self._workers_started:
            return

        self._workers_started = True

        # Start worker tasks
        for i in range(self.max_concurrent_downloads):
            asyncio.create_task(self._download_worker(f"worker-{i}"))

        # Start cleanup task
        asyncio.create_task(self._cleanup_worker())

        log.info(f"Started {self.max_concurrent_downloads} download workers")

    async def shutdown(self):
        """Shutdown the queue manager and cancel all active downloads."""
        log.info("Shutting down download queue manager")
        self._shutdown_event.set()

        # Cancel all active downloads
        for task in self._active_downloads.values():
            task.cancel()

        # Terminate worker processes
        for job_id, process in list(self._download_processes.items()):
            try:
                process.terminate()
            except ProcessLookupError:
                log.debug(f"Worker process for job {job_id} already exited during shutdown")

        for job_id, process in list(self._download_processes.items()):
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                log.warning(f"Worker process for job {job_id} did not exit, killing")
                process.kill()
                await process.wait()
            finally:
                self._download_processes.pop(job_id, None)

        # Clean up any remaining temp files
        for paths in self._job_temp_files.values():
            for path in paths.values():
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._job_temp_files.clear()

        # Wait for workers to finish
        if self._active_downloads:
            await asyncio.gather(*self._active_downloads.values(), return_exceptions=True)

    async def _download_worker(self, worker_name: str):
        """Worker task that processes jobs from the queue."""
        log.debug(f"Download worker {worker_name} started")

        while not self._shutdown_event.is_set():
            try:
                # Wait for a job or shutdown signal
                job = await asyncio.wait_for(self._job_queue.get(), timeout=1.0)

                if job.status == JobStatus.CANCELLED:
                    continue

                # Start processing the job
                job.status = JobStatus.DOWNLOADING
                job.started_time = datetime.now()

                log.info(f"Worker {worker_name} starting job {job.job_id}")

                # Create download task
                download_task = asyncio.create_task(self._execute_download(job))
                self._active_downloads[job.job_id] = download_task

                try:
                    await download_task
                except asyncio.CancelledError:
                    job.status = JobStatus.CANCELLED
                    log.info(f"Job {job.job_id} was cancelled")
                except Exception as e:
                    job.status = JobStatus.FAILED
                    job.error_message = str(e)
                    log.error(f"Job {job.job_id} failed: {e}")
                finally:
                    job.completed_time = datetime.now()
                    if job.job_id in self._active_downloads:
                        del self._active_downloads[job.job_id]

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.error(f"Worker {worker_name} error: {e}")

    async def _execute_download(self, job: DownloadJob):
        """Execute the actual download for a job."""
        log.info(f"Executing download for job {job.job_id}")

        try:
            output_files = await self._run_download_async(job)
            job.status = JobStatus.COMPLETED
            job.output_files = output_files
            job.progress = 100.0
            log.info(f"Download completed for job {job.job_id}: {len(output_files)} files")
        except Exception as e:
            import traceback

            from unshackle.core.api.errors import categorize_exception

            job.status = JobStatus.FAILED
            job.error_message = str(e)
            job.error_details = str(e)

            api_error = categorize_exception(
                e, context={"service": job.service, "title_id": job.title_id, "job_id": job.job_id}
            )
            job.error_code = api_error.error_code.value

            job.error_traceback = traceback.format_exc()

            log.error(f"Download failed for job {job.job_id}: {e}")
            raise

    async def _run_download_async(self, job: DownloadJob) -> List[str]:
        """Invoke a worker subprocess to execute the download."""

        payload = {
            "job_id": job.job_id,
            "service": job.service,
            "title_id": job.title_id,
            "parameters": job.parameters,
        }

        payload_fd, payload_path = tempfile.mkstemp(prefix=f"unshackle_job_{job.job_id}_", suffix="_payload.json")
        os.close(payload_fd)
        result_fd, result_path = tempfile.mkstemp(prefix=f"unshackle_job_{job.job_id}_", suffix="_result.json")
        os.close(result_fd)
        progress_fd, progress_path = tempfile.mkstemp(prefix=f"unshackle_job_{job.job_id}_", suffix="_progress.json")
        os.close(progress_fd)

        with open(payload_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "unshackle.core.api.download_worker",
            payload_path,
            result_path,
            progress_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._download_processes[job.job_id] = process
        self._job_temp_files[job.job_id] = {"payload": payload_path, "result": result_path, "progress": progress_path}

        communicate_task = asyncio.create_task(process.communicate())

        stdout_bytes = b""
        stderr_bytes = b""

        try:
            while True:
                done, _ = await asyncio.wait({communicate_task}, timeout=0.5)
                if communicate_task in done:
                    stdout_bytes, stderr_bytes = communicate_task.result()
                    break

                # Check for progress updates
                try:
                    if os.path.exists(progress_path):
                        with open(progress_path, "r", encoding="utf-8") as handle:
                            progress_data = json.load(handle)
                            if progress_data.get("phase") and progress_data["phase"] != job.phase:
                                job.phase = progress_data["phase"]
                            if progress_data.get("total_tracks"):
                                job.total_tracks = int(progress_data["total_tracks"])
                            if progress_data.get("completed_tracks") is not None:
                                job.completed_tracks = int(progress_data["completed_tracks"])
                            if "active_tracks" in progress_data:
                                job.active_tracks = list(progress_data["active_tracks"])
                            if progress_data.get("skipped_subtitles"):
                                job.skipped_subtitles = progress_data["skipped_subtitles"]
                            if "progress" in progress_data:
                                new_progress = float(progress_data["progress"])
                                if new_progress != job.progress:
                                    job.progress = new_progress
                                    log.info(f"Job {job.job_id} progress updated: {job.progress}%")
                except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
                    log.debug(f"Could not read progress for job {job.job_id}: {e}")

                if job.cancel_event.is_set() or job.status == JobStatus.CANCELLED:
                    log.info(f"Cancellation detected for job {job.job_id}, terminating worker process")
                    process.terminate()
                    try:
                        await asyncio.wait_for(communicate_task, timeout=5)
                    except asyncio.TimeoutError:
                        log.warning(f"Worker process for job {job.job_id} did not terminate, killing")
                        process.kill()
                        await asyncio.wait_for(communicate_task, timeout=5)
                    raise asyncio.CancelledError("Job was cancelled")

            returncode = process.returncode
            stdout = stdout_bytes.decode("utf-8", errors="ignore")
            stderr = stderr_bytes.decode("utf-8", errors="ignore")

            # A service can echo a credential or a proxy URL into its output, so scrub it before
            # it reaches the log as well, not only the API response.
            safe_stdout = _redact_text(stdout.strip(), job.parameters)
            safe_stderr = _redact_text(stderr.strip(), job.parameters)
            if stdout.strip():
                log.debug(f"Worker stdout for job {job.job_id}: {safe_stdout}")
            if stderr.strip():
                job.worker_stderr = stderr.strip()
                if returncode != 0:
                    log.warning(f"Worker stderr for job {job.job_id}: {safe_stderr}")
                else:
                    log.debug(f"Worker stderr for job {job.job_id}: {safe_stderr}")

            result_data: Optional[Dict[str, Any]] = None
            try:
                with open(result_path, "r", encoding="utf-8") as handle:
                    result_data = json.load(handle)
            except FileNotFoundError:
                log.error(f"Result file missing for job {job.job_id}")
            except json.JSONDecodeError as exc:
                log.error(f"Failed to parse worker result for job {job.job_id}: {exc}")

            if returncode != 0:
                message = result_data.get("message") if result_data else "unknown error"
                if result_data:
                    job.error_details = result_data.get("error_details", message)
                    job.error_code = result_data.get("error_code")
                raise Exception(f"Worker exited with code {returncode}: {message}")

            if not result_data or result_data.get("status") != "success":
                message = result_data.get("message") if result_data else "worker did not report success"
                if result_data:
                    job.error_details = result_data.get("error_details", message)
                    job.error_code = result_data.get("error_code")
                raise Exception(f"Worker failure: {message}")

            return result_data.get("output_files", [])

        finally:
            if not communicate_task.done():
                communicate_task.cancel()
                with suppress(asyncio.CancelledError):
                    await communicate_task

            self._download_processes.pop(job.job_id, None)

            temp_paths = self._job_temp_files.pop(job.job_id, {})
            for path in temp_paths.values():
                try:
                    os.remove(path)
                except OSError:
                    pass

    async def _cleanup_worker(self):
        """Worker that periodically cleans up old jobs."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(3600)  # Run every hour
                self.cleanup_old_jobs()
            except Exception as e:
                log.error(f"Cleanup worker error: {e}")


# Global instance
download_manager: Optional[DownloadQueueManager] = None


def get_download_manager() -> DownloadQueueManager:
    """Get the global download manager instance."""
    global download_manager
    if download_manager is None:
        # Load configuration from unshackle config
        from unshackle.core.config import config

        max_concurrent = getattr(config, "max_concurrent_downloads", 2)
        retention_hours = getattr(config, "download_job_retention_hours", 24)

        download_manager = DownloadQueueManager(max_concurrent, retention_hours)

    return download_manager
