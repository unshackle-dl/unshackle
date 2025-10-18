import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("download_manager")


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
        }

        if include_full_details:
            result.update(
                {
                    "parameters": self.parameters,
                    "started_time": self.started_time.isoformat() if self.started_time else None,
                    "completed_time": self.completed_time.isoformat() if self.completed_time else None,
                    "output_files": self.output_files,
                    "error_message": self.error_message,
                    "error_details": self.error_details,
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
    from unshackle.core.config import config
    from unshackle.core.services import Services
    from unshackle.core.utils.click_types import ContextData
    from unshackle.core.utils.collections import merge_dict

    log.info(f"Starting sync download for job {job_id}")

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
    ctx.obj = ContextData(config=service_config, cdm=None, proxy_providers=[], profile=params.get("profile"))
    ctx.params = {
        "proxy": params.get("proxy"),
        "no_proxy": params.get("no_proxy", False),
        "profile": params.get("profile"),
        "tag": params.get("tag"),
        "tmdb_id": params.get("tmdb_id"),
        "tmdb_name": params.get("tmdb_name", False),
        "tmdb_year": params.get("tmdb_year", False),
    }

    dl_instance = dl(
        ctx=ctx,
        no_proxy=params.get("no_proxy", False),
        profile=params.get("profile"),
        proxy=params.get("proxy"),
        tag=params.get("tag"),
        tmdb_id=params.get("tmdb_id"),
        tmdb_name=params.get("tmdb_name", False),
        tmdb_year=params.get("tmdb_year", False),
    )

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

    # Simple progress tracking if callback provided
    if progress_callback:
        # Report initial progress
        progress_callback({"progress": 0.0, "status": "starting"})

        # Simple approach: report progress at key points
        original_result = dl_instance.result

        def result_with_progress(*args, **kwargs):
            try:
                # Report that download started
                progress_callback({"progress": 5.0, "status": "downloading"})

                # Call original method
                result = original_result(*args, **kwargs)

                # Report completion
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
                vcodec=params.get("vcodec"),
                acodec=params.get("acodec"),
                vbitrate=params.get("vbitrate"),
                abitrate=params.get("abitrate"),
                range_=params.get("range", []),
                channels=params.get("channels"),
                no_atmos=params.get("no_atmos", False),
                wanted=params.get("wanted", []),
                lang=params.get("lang", ["orig"]),
                v_lang=params.get("v_lang", []),
                a_lang=params.get("a_lang", []),
                s_lang=params.get("s_lang", ["all"]),
                require_subs=params.get("require_subs", []),
                forced_subs=params.get("forced_subs", False),
                sub_format=params.get("sub_format"),
                video_only=params.get("video_only", False),
                audio_only=params.get("audio_only", False),
                subs_only=params.get("subs_only", False),
                chapters_only=params.get("chapters_only", False),
                no_subs=params.get("no_subs", False),
                no_audio=params.get("no_audio", False),
                no_chapters=params.get("no_chapters", False),
                slow=params.get("slow", False),
                list_=False,
                list_titles=False,
                skip_dl=params.get("skip_dl", False),
                export=params.get("export"),
                cdm_only=params.get("cdm_only"),
                no_proxy=params.get("no_proxy", False),
                no_folder=params.get("no_folder", False),
                no_source=params.get("no_source", False),
                workers=params.get("workers"),
                downloads=params.get("downloads", 1),
                best_available=params.get("best_available", False),
            )

    except SystemExit as exc:
        if exc.code != 0:
            stdout_str = stdout_capture.getvalue()
            stderr_str = stderr_capture.getvalue()
            log.error(f"Download exited with code {exc.code}")
            log.error(f"Stdout: {stdout_str}")
            log.error(f"Stderr: {stderr_str}")
            raise Exception(f"Download failed with exit code {exc.code}")

    except Exception as exc:  # noqa: BLE001 - propagate to caller
        stdout_str = stdout_capture.getvalue()
        stderr_str = stderr_capture.getvalue()
        log.error(f"Download execution failed: {exc}")
        log.error(f"Stdout: {stdout_str}")
        log.error(f"Stderr: {stderr_str}")
        raise

    log.info(f"Download completed for job {job_id}, files in {original_download_dir}")

    return []


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

        log.info(f"Created download job {job_id} for {service}:{title_id}")
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
            log.info(f"Cancelled queued job {job_id}")
            return True
        elif job.status == JobStatus.DOWNLOADING:
            # Set the cancellation event first - this will be checked by the download thread
            job.cancel_event.set()
            job.status = JobStatus.CANCELLED
            log.info(f"Signaled cancellation for downloading job {job_id}")

            # Cancel the active download task
            task = self._active_downloads.get(job_id)
            if task:
                task.cancel()
                log.info(f"Cancelled download task for job {job_id}")

            process = self._download_processes.get(job_id)
            if process:
                try:
                    process.terminate()
                    log.info(f"Terminated worker process for job {job_id}")
                except ProcessLookupError:
                    log.debug(f"Worker process for job {job_id} already exited")

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
            job.status = JobStatus.FAILED
            job.error_message = str(e)
            job.error_details = str(e)
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

            if stdout.strip():
                log.debug(f"Worker stdout for job {job.job_id}: {stdout.strip()}")
            if stderr.strip():
                log.warning(f"Worker stderr for job {job.job_id}: {stderr.strip()}")

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
                raise Exception(f"Worker exited with code {returncode}: {message}")

            if not result_data or result_data.get("status") != "success":
                message = result_data.get("message") if result_data else "worker did not report success"
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

    def _execute_download_sync(self, job: DownloadJob) -> List[str]:
        """Execute download synchronously using existing dl.py logic."""
        return _perform_download(job.job_id, job.service, job.title_id, job.parameters.copy(), job.cancel_event)

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
