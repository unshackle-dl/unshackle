import asyncio
import hashlib
import importlib
import json
import logging
import os
import sys
import tempfile
import threading
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unshackle.core.api.events import bus, publish_service_event
from unshackle.core.api.sanitize import sanitize_log
from unshackle.core.credential import Credential
from unshackle.core.utils.redact import REDACTED, URL_USERINFO_RE, redact_all, redact_path, redact_text

if TYPE_CHECKING:
    from unshackle.core.title_source import TitleSource

log = logging.getLogger("download_manager")


# Job parameters may carry secrets (a raw "user:pass" credential, a proxy URL with embedded
# userinfo). These must never leave the process via the API or logs, so they are masked
# wherever parameters are serialized for a response.
SENSITIVE_PARAM_KEYS = ("credential", "credentials", "password", "token", "api_key")


def redact_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of job parameters with secrets masked, safe to serialise."""
    if not isinstance(parameters, dict):
        return parameters
    redacted = dict(parameters)
    for key in SENSITIVE_PARAM_KEYS:
        if redacted.get(key):
            redacted[key] = REDACTED
    for key in ("proxy", "proxy_download"):
        proxy = redacted.get(key)
        if isinstance(proxy, str) and "@" in proxy:
            redacted[key] = URL_USERINFO_RE.sub(f"{REDACTED}@", proxy)
    return redacted


def credential_secrets(value: Any) -> list[str]:
    """The secret parts of one credential in any form ``Credential.from_config`` loads."""
    try:
        credential = Credential.from_config(value)
    except (AttributeError, TypeError, ValueError):
        return []
    parts = [
        value if isinstance(value, str) else "",
        credential.dumps(),
        str(credential.password),
        str(credential.extra or ""),
    ]
    return [part for part in parts if len(part) >= 4]  # short parts would blanket-replace and garble the text


def secret_values(parameters: dict[str, Any], service: str | None = None) -> list[str]:
    """Raw secret strings carried in job parameters and the credentials configured for ``service``, longest first."""
    if not isinstance(parameters, dict):
        return []
    secrets: list[str] = []
    if service:
        from unshackle.core.config import config

        configured = (config.credentials or {}).get(service)
        for value in configured.values() if isinstance(configured, dict) else [configured]:
            secrets.extend(credential_secrets(value))
    for key in ("credential", "password", "token", "api_key"):
        value = parameters.get(key)
        if isinstance(value, str) and value:
            secrets.append(value)
            if key == "credential":
                secrets.extend(credential_secrets(value))
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


def owner_id(api_key: str | None) -> str | None:
    """A short digest of an API key for the on-disk history, so the key itself never lands in a file."""
    if api_key is None:
        return None
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _redact_text(text: str | None, parameters: dict[str, Any], service: str | None = None) -> str | None:
    """Mask proxy userinfo and any known parameter secrets that leaked into a free-text field
    (error message / details / traceback / job worker stderr) before the API returns the field."""
    return redact_text(text, secret_values(parameters, service))


class JobStatus(Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


WORKER_STDERR_LIMIT = 64 * 1024
# Statuses a job can never leave; such jobs are safe to remove or retry.
TERMINAL_STATUSES = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)


@dataclass
class DownloadJob:
    """Represents a download job with all its parameters and status."""

    job_id: str
    status: JobStatus
    created_time: datetime
    service: str
    title_id: str
    parameters: dict[str, Any]

    started_time: datetime | None = None
    completed_time: datetime | None = None
    progress: float = 0.0

    output_files: list[str] = field(default_factory=list)
    error_message: str | None = None
    error_details: str | None = None
    error_code: str | None = None
    error_traceback: str | None = None
    worker_stderr: str | None = None

    phase: str | None = None
    title: str | None = None
    current_title: str | None = None
    completed_tracks: int = 0
    total_tracks: int = 0
    active_tracks: list[str] = field(default_factory=list)
    track_progress: list[dict[str, Any]] = field(default_factory=list)
    segments_done: float = 0.0
    segments_total: float = 0.0
    speed: str | None = None

    # Prompt a service raised through prompt_user() and waits on; answered by submit_input().
    input_prompt: str | None = None
    # Message a service shows through show_notice() while it waits for an approval; needs no answer.
    notice: str | None = None
    # Text of the QR code that goes with input_prompt or notice.
    qr: str | None = None

    # Subtitles skipped under skip_subtitle_errors (non-fatal). Each entry is a dl.SkippedSubtitle
    # dict (id / language / title) so a client can report which weren't available.
    skipped_subtitles: list[dict[str, Any]] = field(default_factory=list)
    # Titles skipped because a requested track or language is missing; each a dl.SkippedTitle (title / reason).
    skipped_titles: list[dict[str, Any]] = field(default_factory=list)

    cancel_event: threading.Event = field(default_factory=threading.Event)

    # Guards against writing the same job to the persistent history file twice.
    history_recorded: bool = False

    redacted: tuple[tuple, dict[str, str | None]] | None = field(default=None, repr=False, compare=False)

    # X-Secret-Key that created the job; scopes per-caller access, never serialized into to_dict().
    owner_key: str | None = None

    def to_dict(self, include_full_details: bool = False) -> dict[str, Any]:
        """Convert job to dictionary for JSON response."""
        result: dict[str, Any] = {
            "job_id": self.job_id,
            "status": self.status.value,
            "created_time": self.created_time.isoformat(),
            "service": self.service,
            "title_id": self.title_id,
            "title": self.title,
            "progress": self.progress,
            "phase": self.phase,
            "current_title": self.current_title,
            "completed_tracks": self.completed_tracks,
            "total_tracks": self.total_tracks,
            "active_tracks": self.active_tracks,
            "track_progress": self.track_progress,
            "segments_done": self.segments_done,
            "segments_total": self.segments_total,
            "speed": self.speed,
            "skipped_subtitles": self.skipped_subtitles,
            "skipped_titles": self.skipped_titles,
            "input_prompt": self.input_prompt,
            "notice": self.notice,
            "qr": self.qr,
        }

        if include_full_details:
            # Error/stderr/traceback are free text a service may have echoed a credential or proxy
            # URL into, so scrub them with the same secrets that redact_parameters masks.
            raw = (self.error_message, self.error_details, self.error_traceback, self.worker_stderr)
            if not self.redacted or self.redacted[0] != raw:
                names = ("error_message", "error_details", "error_traceback", "worker_stderr")
                self.redacted = (
                    raw,
                    {
                        name: redact_path(_redact_text(text, self.parameters, self.service))
                        for name, text in zip(names, raw, strict=True)
                    },
                )
            result.update(
                {
                    "parameters": redact_parameters(self.parameters),
                    "started_time": self.started_time.isoformat() if self.started_time else None,
                    "completed_time": self.completed_time.isoformat() if self.completed_time else None,
                    "output_files": self.output_files,
                    "error_code": self.error_code,
                    **self.redacted[1],
                }
            )

        return result


def history_path() -> Path:
    """Path of the persistent job history file (under the cache dir, kept out of the config/data tree)."""
    from unshackle.core.config import config

    return config.directories.cache / "api_history.jsonl"


def history_limit() -> int:
    """Max history entries to retain (serve.history_limit, default 100). A value of 0 or less means no limit."""
    from unshackle.core.config import config

    try:
        return int((config.serve or {}).get("history_limit", 100))
    except (TypeError, ValueError):
        return 100


_history_appends_since_trim = 0  # module-level; record_job_history runs serialized on the event loop


def record_job_history(job: DownloadJob) -> None:
    """Append a terminal job's summary as one JSON line to the history file (best effort)."""
    global _history_appends_since_trim
    if job.history_recorded or job.status not in TERMINAL_STATUSES:
        return
    job.history_recorded = True
    entry = {
        "job_id": job.job_id,
        "owner": owner_id(job.owner_key),
        "service": job.service,
        "title_id": job.title_id,
        "title": job.title,
        "status": job.status.value,
        "created_time": job.created_time.isoformat(),
        "completed_time": job.completed_time.isoformat() if job.completed_time else None,
        "output_files": job.output_files,
        "skipped_titles": job.skipped_titles,
        "parameters": redact_parameters(job.parameters),
        "error_message": _redact_text(job.error_message, job.parameters, job.service),
    }
    try:
        path = history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry)
        limit = history_limit()
        # Serialized on the manager's event loop, so append + periodic trim doesn't race.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        # Appending is O(1); only pay the full read-rewrite once appends drift past a slack
        # margin, keeping the newest N.
        if limit > 0:
            _history_appends_since_trim += 1
            if _history_appends_since_trim >= max(16, limit // 4):
                _history_appends_since_trim = 0
                existing = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                if len(existing) > limit:
                    path.write_text("\n".join(existing[-limit:]) + "\n", encoding="utf-8")  # keep newest N
    except OSError as e:
        log.warning(f"Could not write job history: {e}")


def read_job_history(limit: int = 100, service: str | None = None, owner: str | None = None) -> list[dict[str, Any]]:
    """Read persisted job history, newest first.

    With ``owner`` (an :func:`owner_id`), only that owner's entries and ownerless legacy
    entries come back. This function ignores corrupt lines. A missing file gives no entries.
    """
    path = history_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        log.warning(f"Could not read job history: {e}")
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if service and str(entry.get("service") or "").upper() != service.upper():
            continue
        if owner is not None and entry.get("owner") not in (None, owner):
            continue
        entries.append(entry)
    entries.reverse()
    return entries[:limit] if limit and limit > 0 else entries


def delete_job_history(job_id: str, allowed: set | None = None, owner: str | None = None) -> bool:
    """Remove a history entry by job_id (rewriting the file). Returns True if it removed one.

    When you give `allowed`, this function treats an entry outside the caller's service
    allowlist as absent (returns False), so a restricted API key cannot delete the entry.
    """
    path = history_path()
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        log.warning(f"Could not read job history: {e}")
        return False
    kept: list[str] = []
    deleted = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            kept.append(stripped)  # preserve corrupt lines untouched
            continue
        match = isinstance(entry, dict) and entry.get("job_id") == job_id
        owned = owner is None or entry.get("owner") in (None, owner)
        if match and owned and (allowed is None or str(entry.get("service") or "").upper() in allowed):
            deleted = True
            continue
        kept.append(stripped)
    if not deleted:
        return False
    try:
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except OSError as e:
        log.warning(f"Could not write job history: {e}")
        return False
    return True


def stamped_keys(params: dict[str, Any]) -> set[str]:
    """The parameters the client sent, from the server's ``client_keys`` stamp; every parameter without one."""
    return set(params.get("client_keys", params))


def plan_keys(params: dict[str, Any]) -> set[str] | None:
    """The parameters a job's dl plan counts as typed: :func:`stamped_keys`, or None (every one) for a relayed job.

    The remote server's own defaults must never replace the values this server merged for a relayed job.
    """
    return None if params.get("remote") else stamped_keys(params)


def client_sent_keys(params: dict[str, Any]) -> set[str] | None:
    """Pop the ``client_keys`` stamp and give the :func:`plan_keys` it held."""
    keys = plan_keys(params)
    params.pop("client_keys", None)
    return keys


def perform_download(
    job_id: str,
    service: str,
    title_id: str,
    params: dict[str, Any],
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> list[str]:
    """Do the synchronous download work for a job.

    ``params`` holds the API's form of the ``dl`` options; ``api_plan`` checks and casts them into the dl context,
    which feeds dl() and dl.result() as on the CLI. The service is built through its click cli like every other
    server path.
    """

    from contextlib import redirect_stderr, redirect_stdout
    from io import StringIO

    # Import dl.py components lazily to avoid circular deps during module import
    import click

    from unshackle.commands.dl import dl
    from unshackle.core.api.errors import APIError
    from unshackle.core.config import config
    from unshackle.core.remote_protocol import APIErrorCode
    from unshackle.core.services import Services

    log.info(f"Starting sync download for job {job_id}")

    # A service caches tokens under cache/<Service>/, keyed by service name only, so two jobs on
    # one service with different credentials would share a cache. When a per-job credential is set,
    # namespace the cache dir by a hash of it so the sessions can't cross.
    job_credential = params.get("credential")
    if job_credential:
        import hashlib

        cred_hash = hashlib.pbkdf2_hmac("sha256", job_credential.encode("utf-8"), b"unshackle-job-ns", 100_000).hex()[
            :12
        ]
        config.directories.cache = config.directories.cache / "_jobs" / cred_hash

    for banned in ("postscript", "post_script", "post_scripts"):
        if params.pop(banned, None) is not None:
            log.warning(f"Ignoring '{banned}' in API download parameters; post-scripts are config-only")

    from unshackle.core.api.handlers import DL_TRANSPORT_KEYS, instantiate_service, relay_label
    from unshackle.core.launch import launch
    from unshackle.core.proxies.resolve import initialize_proxy_providers
    from unshackle.core.run_plan import RunStopped, api_plan

    ctx = api_plan(params, client_sent_keys(params)).context(dl.cli, service)
    launch(ctx, {"client_argv": f"{relay_label(params)} download job {job_id}"})
    providers = initialize_proxy_providers(quiet=True) if params.get("server_proxy") is True else []
    dl_instance = dl(ctx=ctx, proxy_providers=providers, **ctx.params)
    # Per-request credential ("user:pass"); feed it into the map get_credentials() reads so a
    # client can authenticate without anything being persisted to disk. Without a profile,
    # get_credentials() falls back to "default", so store it there too rather than dropping it
    # (which would silently authenticate as the server's own default account).
    if params.get("credential"):
        svc_creds = config.credentials.get(service)
        if not isinstance(svc_creds, dict):
            config.credentials[service] = svc_creds = {}
        svc_creds[params.get("profile") or "default"] = params["credential"]

    remote = ctx.params["server"]
    if remote:
        from unshackle.core.remote_service import make_remote_service

        service_ctx = click.Context(click.Command(service), parent=ctx, obj=ctx.obj)
        service_instance: TitleSource = make_remote_service(service_ctx, remote, service, title_id, params)
    else:
        service_instance = instantiate_service(ctx, Services.load(service), title_id, params, DL_TRANSPORT_KEYS)

    original_download_dir = config.directories.downloads

    stdout_capture = StringIO()
    stderr_capture = StringIO()

    # progress_sink (dl.build_job_progress_callables) owns percentage; terminal status
    # (success/failure) is reported by the worker's result file.
    try:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            outcome = dl_instance.result(
                service=service_instance,
                **{**ctx.params, "select_titles": False, "list_": False, "list_titles": False},
                progress_sink=progress_callback,
            )

    except RunStopped as exc:
        log.debug("Stdout: %s", stdout_capture.getvalue())
        raise APIError(APIErrorCode.DOWNLOAD_ERROR, f"Download failed: {exc.reason}") from exc

    except SystemExit as exc:
        if exc.code != 0:
            log.error(f"Download exited with code {exc.code}")
            log.debug("Stdout: %s", stdout_capture.getvalue())
            log.debug("Stderr: %s", stderr_capture.getvalue())
            raise APIError(APIErrorCode.DOWNLOAD_ERROR, f"Download failed with exit code {exc.code}") from exc
        outcome = dl_instance.outcome()

    except Exception as exc:  # noqa: BLE001 - propagate to caller
        log.error(f"Download execution failed: {exc}")
        log.debug("Stdout: %s", stdout_capture.getvalue())
        log.debug("Stderr: %s", stderr_capture.getvalue())
        raise

    # dl.result() reports a download-worker exception and returns normally, so the CLI exits cleanly.
    if outcome.error is not None:
        # segment URLs carry signed CDN tokens (hmac, session IDs) that redact_text does not match
        detail = redact_all(dl_instance.failure_lines(outcome.error)[1].strip())
        named = (getattr(e, "error_code", None) for e in (outcome.error, outcome.error.__cause__))
        code = next((c for c in named if isinstance(c, APIErrorCode)), APIErrorCode.WORKER_ERROR)
        raise APIError(code, f"download worker failed: {detail}")

    if progress_callback and outcome.skipped_subtitles:
        progress_callback({"skipped_subtitles": outcome.skipped_subtitles})

    output_files = [str(p) for p in outcome.files]
    log.info(f"Download completed for job {job_id}, {len(output_files)} file(s) in {original_download_dir}")

    return output_files


class DownloadQueueManager:
    """Manages download job queue with configurable concurrency limits."""

    def __init__(self, max_concurrent_downloads: int = 2, job_retention_hours: int = 24) -> None:
        self.max_concurrent_downloads = max_concurrent_downloads
        self.job_retention_hours = job_retention_hours

        self._jobs: dict[str, DownloadJob] = {}
        self._job_queue: asyncio.Queue = asyncio.Queue()
        # asyncio.Queue has no reorder, so prioritized jobs go in a deque workers drain first.
        self._priority_jobs: deque = deque()
        self._promoted_ids: set = set()  # their stale FIFO-queue copies get skipped via this set
        self._active_downloads: dict[str, asyncio.Task] = {}
        self._download_processes: dict[str, asyncio.subprocess.Process] = {}
        self._job_temp_files: dict[str, dict[str, str]] = {}
        self._workers_started = False
        self._shutdown_event = asyncio.Event()
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

        log.info(
            f"Initialized download queue manager: max_concurrent={max_concurrent_downloads}, retention_hours={job_retention_hours}"
        )

    def create_job(self, service: str, title_id: str, owner_key: str | None = None, **parameters: Any) -> DownloadJob:
        """Make a new download job and add it to the queue."""
        from unshackle.core.api.session_store import refuse_while_draining

        refuse_while_draining()
        job_id = str(uuid.uuid4())
        job = DownloadJob(
            job_id=job_id,
            status=JobStatus.QUEUED,
            created_time=datetime.now(),
            service=service,
            title_id=title_id,
            parameters=parameters,
            owner_key=owner_key,
        )

        self._jobs[job_id] = job
        self._job_queue.put_nowait(job)

        log.info(f"Created download job {job_id} for {sanitize_log(service)}:{sanitize_log(title_id)}")
        bus.publish("job", {"event": "queued", **job.to_dict(include_full_details=True)})
        return job

    def get_job(self, job_id: str) -> DownloadJob | None:
        """Get job by ID."""
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[DownloadJob]:
        """Get all jobs."""
        return list(self._jobs.values())

    def subscribe(self, job_id: str) -> asyncio.Queue:
        """Add a listener for a job's events, and return the queue it receives them on."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.setdefault(job_id, []).append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        """Drop a listener registered by subscribe()."""
        queues = self._subscribers.get(job_id)
        if not queues:
            return
        with suppress(ValueError):
            queues.remove(queue)
        if not queues:
            del self._subscribers[job_id]

    def publish(self, job: DownloadJob, event: str) -> None:
        """Fan an event carrying the job's full detail dict out to every listener."""
        data = job.to_dict(include_full_details=True)
        bus.publish("job", {"event": event, **data})
        queues = self._subscribers.get(job.job_id)
        if not queues:
            return
        payload = {"event": event, "data": data}
        for queue in queues:
            self.put(queue, payload)

    def publish_terminal(self, job: DownloadJob) -> None:
        """Publish a job's final state, close every listener's event stream, then apply staged service reloads."""
        self.publish(job, job.status.value)
        for queue in self._subscribers.pop(job.job_id, []):
            self.put(queue, None)
        self.schedule_pending_reload()

    def busy_services(self) -> set[str]:
        """Tags with a job that has not reached a terminal status; queued jobs count so none starts on half-swapped code."""
        return {job.service for job in self._jobs.values() if job.status not in TERMINAL_STATUSES}

    def schedule_pending_reload(self) -> None:
        """Swap in staged service updates whose last job has finished, off the event loop."""
        schedule_pending_reload()

    @staticmethod
    def put(queue: asyncio.Queue, item: dict[str, Any] | None) -> None:
        """Enqueue an item, discarding the oldest when a slow listener has filled the queue."""
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(item)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a job that has the queued or the downloading status."""
        job = self._jobs.get(job_id)
        if not job:
            return False

        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.CANCELLED
            job.cancel_event.set()  # Signal cancellation
            job.completed_time = datetime.now()
            record_job_history(job)  # queued jobs never reach a worker, so persist here
            self.publish_terminal(job)
            log.info(f"Cancelled queued job {sanitize_log(job_id)}")
            return True
        elif job.status == JobStatus.DOWNLOADING:
            job.cancel_event.set()
            job.status = JobStatus.CANCELLED
            log.info(f"Signaled cancellation for downloading job {sanitize_log(job_id)}")

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

    def submit_input(self, job: DownloadJob, response: str) -> bool:
        """Send *response* to the job's worker when it waits on a prompt; ``False`` when no prompt is pending."""
        process = self._download_processes.get(job.job_id)
        if not job.input_prompt or process is None or process.stdin is None:
            return False
        process.stdin.write((json.dumps(response) + "\n").encode("utf-8"))
        # Cleared here, not on the worker's next progress write, so a second submit cannot queue a stale answer.
        job.input_prompt = None
        self.publish(job, "progress")
        return True

    def remove_job(self, job_id: str) -> bool:
        """Remove a terminal (completed/failed/cancelled) job entirely."""
        job = self._jobs.get(job_id)
        if not job or job.status not in TERMINAL_STATUSES:
            return False
        del self._jobs[job_id]
        log.info(f"Removed job {sanitize_log(job_id)}")
        return True

    def clear_finished_jobs(self, owner_key: str | None = None) -> int:
        """Remove terminal (completed/failed/cancelled) jobs, returning the count removed.

        With ``owner_key`` only that key's jobs and ownerless legacy jobs go.
        """
        finished = [
            job_id
            for job_id, job in self._jobs.items()
            if job.status in TERMINAL_STATUSES and (owner_key is None or job.owner_key in (None, owner_key))
        ]
        for job_id in finished:
            del self._jobs[job_id]
        if finished:
            log.info(f"Cleared {len(finished)} finished jobs")
        return len(finished)

    def prioritize_job(self, job_id: str) -> bool:
        """Move a queued job to the front of the line."""
        job = self._jobs.get(job_id)
        if not job or job.status != JobStatus.QUEUED:
            return False
        if job in self._priority_jobs:
            return True
        self._priority_jobs.append(job)
        self._promoted_ids.add(job_id)
        log.info(f"Moved job {sanitize_log(job_id)} to front of queue")
        return True

    def cleanup_old_jobs(self) -> int:
        """Remove jobs older than retention period."""
        cutoff_time = datetime.now() - timedelta(hours=self.job_retention_hours)
        jobs_to_remove = []

        for job_id, job in self._jobs.items():
            if job.status in TERMINAL_STATUSES:
                if job.completed_time and job.completed_time < cutoff_time:
                    jobs_to_remove.append(job_id)
                elif not job.completed_time and job.created_time < cutoff_time:
                    jobs_to_remove.append(job_id)

        for job_id in jobs_to_remove:
            del self._jobs[job_id]

        if jobs_to_remove:
            log.info(f"Cleaned up {len(jobs_to_remove)} old jobs")

        return len(jobs_to_remove)

    async def start_workers(self) -> None:
        """Start job worker tasks to process the download queue."""
        if self._workers_started:
            return

        self._workers_started = True

        # Start worker tasks
        for i in range(self.max_concurrent_downloads):
            asyncio.create_task(self.download_worker(f"worker-{i}"))

        # Start cleanup task
        asyncio.create_task(self.cleanup_worker())

        log.info(f"Started {self.max_concurrent_downloads} download workers")

    async def shutdown(self) -> None:
        """Shutdown the queue manager and cancel all active downloads."""
        log.info("Shutting down download queue manager")
        self._shutdown_event.set()

        for queues in list(self._subscribers.values()):
            for queue in queues:
                self.put(queue, None)
        self._subscribers.clear()

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
            except TimeoutError:
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

    async def download_worker(self, worker_name: str) -> None:
        """Job worker task that processes jobs from the queue."""
        log.debug(f"Download worker {worker_name} started")

        while not self._shutdown_event.is_set():
            try:
                # Prioritized jobs run first; their stale copies still in the FIFO queue are skipped.
                if self._priority_jobs:
                    job = self._priority_jobs.popleft()
                else:
                    job = await asyncio.wait_for(self._job_queue.get(), timeout=1.0)
                    if job.job_id in self._promoted_ids:
                        self._promoted_ids.discard(job.job_id)
                        continue

                if job.status == JobStatus.CANCELLED:
                    continue

                # Start processing the job
                job.status = JobStatus.DOWNLOADING
                job.started_time = datetime.now()
                self.publish(job, "status")

                log.info(f"Worker {worker_name} starting job {job.job_id}")

                # Create download task
                download_task = asyncio.create_task(self.execute_download(job))
                self._active_downloads[job.job_id] = download_task

                try:
                    await download_task
                except asyncio.CancelledError:
                    job.status = JobStatus.CANCELLED
                    log.info(f"Job {job.job_id} was cancelled")
                except Exception as e:
                    job.status = JobStatus.FAILED
                    job.error_message = str(e)
                    log.error(
                        f"Job {job.job_id} failed: {_redact_text(job.error_message, job.parameters, job.service)}"
                    )
                finally:
                    job.completed_time = datetime.now()
                    record_job_history(job)
                    self.publish_terminal(job)
                    if job.job_id in self._active_downloads:
                        del self._active_downloads[job.job_id]

            except TimeoutError:
                continue
            except Exception as e:
                log.error(f"Worker {worker_name} error: {e}")

    async def execute_download(self, job: DownloadJob) -> None:
        """Do the actual download for a job."""
        log.info(f"Executing download for job {job.job_id}")

        try:
            output_files = await self.run_download_async(job)
            job.status = JobStatus.COMPLETED
            job.output_files = output_files
            job.progress = 100.0
            log.info(f"Download completed for job {job.job_id}: {len(output_files)} files")
        except Exception as e:
            import traceback

            from unshackle.core.api.errors import categorize_exception

            job.status = JobStatus.FAILED
            job.error_message = str(e)

            api_error = categorize_exception(
                e, context={"service": job.service, "title_id": job.title_id, "job_id": job.job_id}
            )
            job.error_details = job.error_details or str(e)
            job.error_code = job.error_code or api_error.error_code.value
            job.error_traceback = job.error_traceback or traceback.format_exc()

            log.error(f"Download failed for job {job.job_id}: {_redact_text(str(e), job.parameters, job.service)}")
            raise

    def worker_speed_limit(self) -> float | None:
        """Return one job worker's share of the locked global speed limit, or None when unset."""
        requests_downloader = importlib.import_module("unshackle.core.downloaders.requests")

        limiter = requests_downloader.speed_limiter
        if not (requests_downloader.speed_limit_locked and limiter):
            return None
        return limiter.rate / max(1, self.max_concurrent_downloads)

    async def run_download_async(self, job: DownloadJob) -> list[str]:
        """Invoke a job worker subprocess to do the download."""

        payload = {
            "job_id": job.job_id,
            "service": job.service,
            "title_id": job.title_id,
            "parameters": job.parameters,
            "speed_limit": self.worker_speed_limit(),
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
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._download_processes[job.job_id] = process
        self._job_temp_files[job.job_id] = {"payload": payload_path, "result": result_path, "progress": progress_path}

        async def drain() -> tuple[bytes, bytes]:
            """Collect the worker's output; unlike communicate(), leave stdin open for prompt answers."""
            assert process.stdout is not None and process.stderr is not None
            out, err, _ = await asyncio.gather(process.stdout.read(), process.stderr.read(), process.wait())
            return out, err

        communicate_task = asyncio.create_task(drain())

        stdout_bytes = b""
        stderr_bytes = b""
        last_progress_stat: tuple[int, int] | None = None  # (st_mtime_ns, st_size) of last parse
        last_published: dict[str, Any] | None = None

        try:
            while True:
                done, _ = await asyncio.wait({communicate_task}, timeout=0.5)

                # Read progress before the exit check so the worker's last write lands (skip an unchanged file)
                try:
                    stat = os.stat(progress_path)
                    stat_key = (stat.st_mtime_ns, stat.st_size)
                    if stat_key != last_progress_stat:
                        with open(progress_path, encoding="utf-8") as handle:
                            progress_data = json.load(handle)
                            prompt_before = job.input_prompt
                            if progress_data.get("phase") and progress_data["phase"] != job.phase:
                                job.phase = progress_data["phase"]
                            if progress_data.get("current_title"):
                                job.current_title = str(progress_data["current_title"])
                            if progress_data.get("title"):
                                job.title = str(progress_data["title"])
                            if progress_data.get("output_files"):
                                job.output_files = [str(f) for f in progress_data["output_files"]]
                            if progress_data.get("total_tracks"):
                                job.total_tracks = int(progress_data["total_tracks"])
                            if progress_data.get("completed_tracks") is not None:
                                job.completed_tracks = int(progress_data["completed_tracks"])
                            if "active_tracks" in progress_data:
                                job.active_tracks = list(progress_data["active_tracks"])
                            if "track_progress" in progress_data:
                                job.track_progress = list(progress_data["track_progress"])
                            for _sk in ("segments_done", "segments_total"):
                                if _sk in progress_data:
                                    try:
                                        setattr(job, _sk, float(progress_data[_sk]))
                                    except (TypeError, ValueError):
                                        pass
                            if progress_data.get("speed"):
                                job.speed = str(progress_data["speed"])
                            if "input_prompt" in progress_data:
                                job.input_prompt = progress_data["input_prompt"]
                            if "notice" in progress_data:
                                job.notice = progress_data["notice"]
                            if "qr" in progress_data:
                                job.qr = progress_data["qr"]
                            if progress_data.get("skipped_subtitles"):
                                job.skipped_subtitles = progress_data["skipped_subtitles"]
                            if progress_data.get("skipped_titles"):
                                job.skipped_titles = progress_data["skipped_titles"]
                            if "progress" in progress_data:
                                new_progress = float(progress_data["progress"])
                                if new_progress != job.progress:
                                    job.progress = new_progress
                                    log.debug(f"Job {job.job_id} progress updated: {job.progress}%")
                            # submit_input() clears the prompt out of band, so a re-prompt with the same text
                            # leaves the file equal to last_published; publish the prompt change anyway.
                            if progress_data != last_published or job.input_prompt != prompt_before:
                                last_published = progress_data
                                self.publish(job, "progress")
                        last_progress_stat = stat_key
                except (OSError, json.JSONDecodeError, ValueError) as e:
                    log.debug(f"Could not read progress for job {job.job_id}: {e}")

                if communicate_task in done:
                    stdout_bytes, stderr_bytes = communicate_task.result()
                    break

                if job.cancel_event.is_set() or job.status == JobStatus.CANCELLED:
                    log.info(f"Cancellation detected for job {job.job_id}, terminating worker process")
                    process.terminate()
                    try:
                        await asyncio.wait_for(communicate_task, timeout=5)
                    except TimeoutError:
                        log.warning(f"Worker process for job {job.job_id} did not terminate, killing")
                        process.kill()
                        await asyncio.wait_for(communicate_task, timeout=5)
                    raise asyncio.CancelledError("Job was cancelled")

            returncode = process.returncode
            stdout = stdout_bytes.decode("utf-8", errors="ignore")
            stderr = stderr_bytes.decode("utf-8", errors="ignore")

            # A service can echo a credential or a proxy URL into its output, so scrub it before
            # it reaches the log as well, not only the API response.
            safe_stdout = _redact_text(stdout.strip(), job.parameters, job.service)
            safe_stderr = _redact_text(stderr.strip(), job.parameters, job.service)
            if stdout.strip():
                log.debug(f"Worker stdout for job {job.job_id}: {safe_stdout}")
            if stderr.strip():
                job.worker_stderr = stderr.strip()[-WORKER_STDERR_LIMIT:]
                if returncode != 0:
                    log.warning(f"Worker stderr for job {job.job_id}: {safe_stderr}")
                else:
                    log.debug(f"Worker stderr for job {job.job_id}: {safe_stderr}")

            result_data: dict[str, Any] | None = None
            for attempt in range(5):
                try:
                    with open(result_path, encoding="utf-8") as handle:
                        result_data = json.load(handle)
                except FileNotFoundError:
                    log.error(f"Result file missing for job {job.job_id}")
                except PermissionError as exc:
                    log.debug(f"Result file for job {job.job_id} denied, retrying: {exc}")
                    await asyncio.sleep(0.02 * (attempt + 1))
                    continue
                except json.JSONDecodeError as exc:
                    log.error(f"Failed to parse worker result for job {job.job_id}: {exc}")
                break

            if result_data:
                job.error_traceback = result_data.get("traceback")

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
            job.input_prompt = None
            job.notice = None
            job.qr = None

            temp_paths = self._job_temp_files.pop(job.job_id, {})
            for path in temp_paths.values():
                try:
                    os.remove(path)
                except OSError:
                    pass

    async def cleanup_worker(self) -> None:
        """Job worker that periodically cleans up old jobs."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(3600)
                self.cleanup_old_jobs()
            except Exception as e:
                log.error(f"Cleanup worker error: {e}")


# Global instance
download_manager: DownloadQueueManager | None = None


def get_download_manager() -> DownloadQueueManager:
    """Get the global download manager instance."""
    global download_manager
    if download_manager is None:
        from unshackle.core.config import config

        max_concurrent = getattr(config, "max_concurrent_downloads", 2)
        retention_hours = getattr(config, "download_job_retention_hours", 24)

        download_manager = DownloadQueueManager(max_concurrent, retention_hours)

    return download_manager


def busy_services() -> set[str]:
    """Tags a hot reload must not swap: those with an unfinished job and those a live remote session uses."""
    from unshackle.core.api.session_store import get_session_store
    from unshackle.core.api.stats import stats

    busy = {entry.service_tag for entry in get_session_store().list()}
    if stats.mode != "remote_only":
        busy |= get_download_manager().busy_services()
    return busy


_reload_task: asyncio.Task[None] | None = None


def schedule_pending_reload(tag: str | None = None) -> None:
    """Swap in staged service updates whose last job or remote session has ended, off the event loop.

    Runs on every job completion and remote session removal, because a busy service on a popular
    tag can otherwise stay staged across every periodic refresh tick. With ``tag``, the reload only
    runs when ``PENDING`` holds that tag.
    """
    global _reload_task
    from unshackle.core import services

    if not services.PENDING or (tag is not None and tag not in services.PENDING):
        return
    # apply_pending computes its ready set before it takes RELOAD_LOCK, so concurrent
    # session ends would each re-import the same tag; one in-flight task covers them all.
    if _reload_task is not None and not _reload_task.done():
        return

    async def run() -> None:
        try:
            applied = await asyncio.to_thread(services.apply_pending, busy_services())
            if applied:
                log.info(f"Services reloaded after their jobs and sessions ended: {', '.join(applied)}")
                publish_service_event("applied", applied)
        except Exception:
            log.exception("Applying pending service reloads failed")

    _reload_task = asyncio.get_running_loop().create_task(run())

    def clear(_: asyncio.Task[None]) -> None:
        global _reload_task
        _reload_task = None

    _reload_task.add_done_callback(clear)
