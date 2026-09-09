"""Standalone job worker process entry point for executing download jobs."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

from .download_manager import perform_download

log = logging.getLogger("download_worker")


def read_payload(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


_replace_lock = threading.Lock()


def write_result(path: Path, payload: Dict[str, Any]) -> None:
    """Write the payload with an atomic replace, because the parent polls this file during the write.

    Each call gets its own temp name, so two threads writing this destination cannot replace away
    the temp file the other is about to move.

    Windows denies the replace while another thread or the parent holds the destination open. The
    lock removes the in-process contention; the retry covers the parent process.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        with _replace_lock:
            for attempt in range(10):
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.02 * (attempt + 1))
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def main(argv: list[str]) -> int:
    if len(argv) not in [3, 4]:
        print(
            "Usage: python -m unshackle.core.api.download_worker <payload_path> <result_path> [progress_path]",
            file=sys.stderr,
        )
        return 2

    payload_path = Path(argv[1])
    result_path = Path(argv[2])
    progress_path = Path(argv[3]) if len(argv) > 3 else None

    result: Dict[str, Any] = {}
    exit_code = 0

    try:
        payload = read_payload(payload_path)
        job_id = payload["job_id"]
        service = payload["service"]
        title_id = payload["title_id"]
        params = payload.get("parameters", {})

        log.info(f"Worker starting job {job_id} ({service}:{title_id})")

        # Merged so sparse keys (current_title, output_files) survive later writes.
        progress_state: Dict[str, Any] = {}
        progress_lock = threading.Lock()

        def progress_callback(progress_data: Dict[str, Any]) -> None:
            """Write progress updates to file for main process to read."""
            if progress_path:
                try:
                    with progress_lock:
                        progress_state.update(progress_data)
                        log.info(f"Writing progress update: {progress_data}")
                        write_result(progress_path, progress_state)
                    log.info(f"Progress update written to {progress_path}")
                except Exception as e:
                    log.error(f"Failed to write progress update: {e}")

        output_files = perform_download(job_id, service, title_id, params, progress_callback=progress_callback)

        result = {"status": "success", "output_files": output_files}

    except Exception as exc:  # noqa: BLE001 - capture for parent process
        from unshackle.core.api.errors import categorize_exception

        exit_code = 1
        tb = traceback.format_exc()
        log.error(f"Worker failed with error: {exc}")

        api_error = categorize_exception(
            exc,
            context={
                "service": payload.get("service") if "payload" in locals() else None,
                "title_id": payload.get("title_id") if "payload" in locals() else None,
                "job_id": payload.get("job_id") if "payload" in locals() else None,
            },
        )

        result = {
            "status": "error",
            "message": str(exc),
            "error_details": api_error.message,
            "error_code": api_error.error_code.value,
            "traceback": tb,
        }

    finally:
        try:
            write_result(result_path, result)
        except Exception as exc:  # noqa: BLE001 - last resort logging
            log.error(f"Failed to write worker result file: {exc}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
