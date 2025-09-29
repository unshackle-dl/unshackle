"""Standalone worker process entry point for executing download jobs."""

from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Dict

from .download_manager import _perform_download

log = logging.getLogger("download_worker")


def _read_payload(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_result(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle)


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
        payload = _read_payload(payload_path)
        job_id = payload["job_id"]
        service = payload["service"]
        title_id = payload["title_id"]
        params = payload.get("parameters", {})

        log.info(f"Worker starting job {job_id} ({service}:{title_id})")

        def progress_callback(progress_data: Dict[str, Any]) -> None:
            """Write progress updates to file for main process to read."""
            if progress_path:
                try:
                    log.info(f"Writing progress update: {progress_data}")
                    _write_result(progress_path, progress_data)
                    log.info(f"Progress update written to {progress_path}")
                except Exception as e:
                    log.error(f"Failed to write progress update: {e}")

        output_files = _perform_download(
            job_id, service, title_id, params, cancel_event=None, progress_callback=progress_callback
        )

        result = {"status": "success", "output_files": output_files}

    except Exception as exc:  # noqa: BLE001 - capture for parent process
        exit_code = 1
        tb = traceback.format_exc()
        log.error(f"Worker failed with error: {exc}")
        result = {"status": "error", "message": str(exc), "traceback": tb}

    finally:
        try:
            _write_result(result_path, result)
        except Exception as exc:  # noqa: BLE001 - last resort logging
            log.error(f"Failed to write worker result file: {exc}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
