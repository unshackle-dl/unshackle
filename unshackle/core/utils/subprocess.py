import json
import subprocess
import time
from pathlib import Path
from typing import Optional, Sequence, Union

from unshackle.core import binaries
from unshackle.core.console import console
from unshackle.core.utilities import get_debug_logger


def log_tool_run(
    label: str,
    tool: Optional[str],
    returncode: Optional[int],
    *,
    duration_ms: Optional[float] = None,
    **context: object,
) -> None:
    """Emit a structured ``tool_run`` debug-log entry for an external tool invocation.

    Central helper so every binary call (ffmpeg, mkvpropedit, dovi_tool, etc.) logs the
    same shape. No-op when debug logging is disabled.
    """
    dl = get_debug_logger()
    if not dl:
        return
    failed = bool(returncode)
    dl.log(
        level="ERROR" if failed else "DEBUG",
        operation="tool_run",
        message=f"{label} {'failed' if failed else 'ok'}",
        context={
            "label": label,
            "tool": tool,
            "returncode": returncode,
            "duration_ms": duration_ms,
            **context,
        },
    )


def ffprobe(uri: Union[bytes, Path]) -> dict:
    """Use ffprobe on the provided data to get stream information."""
    if not binaries.FFProbe:
        raise EnvironmentError('FFProbe executable "ffprobe" not found but is required.')

    args = [binaries.FFProbe, "-v", "quiet", "-of", "json", "-show_streams"]
    if isinstance(uri, Path):
        args.extend(
            ["-f", "lavfi", "-i", "movie={}[out+subcc]".format(str(uri).replace("\\", "/").replace(":", "\\\\:"))]
        )
    elif isinstance(uri, bytes):
        args.append("pipe:")

    dl = get_debug_logger()
    start = time.monotonic()
    try:
        ff = subprocess.run(args, input=uri if isinstance(uri, bytes) else None, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        if dl:
            dl.log(
                level="DEBUG",
                operation="tool_run",
                message="ffprobe failed",
                context={"tool": "ffprobe", "duration_ms": round((time.monotonic() - start) * 1000, 1)},
            )
        return {}
    result = json.loads(ff.stdout.decode("utf8"))
    if dl:
        dl.log(
            level="DEBUG",
            operation="tool_run",
            message=f"ffprobe found {len(result.get('streams', []))} stream(s)",
            context={
                "tool": "ffprobe",
                "streams": len(result.get("streams", [])),
                "duration_ms": round((time.monotonic() - start) * 1000, 1),
            },
        )
    return result


def run_step(
    args: Sequence[Union[str, Path]],
    *,
    status: Optional[str] = None,
    output: Optional[Path] = None,
    label: str = "subprocess step",
) -> bytes:
    """Run a CLI step that writes to `output` (when provided). Returns stderr bytes.

    Raises RuntimeError with the stderr tail when the process exits non-zero, or when
    `output` is given and does not exist / is empty after the run.
    """
    if output is not None:
        output.unlink(missing_ok=True)

    str_args = [str(a) for a in args]
    start = time.monotonic()
    if status:
        with console.status(status, spinner="dots"):
            p = subprocess.run(str_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        p = subprocess.run(str_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    stderr = p.stderr or b""
    bad_output = output is not None and (not output.exists() or output.stat().st_size == 0)
    failed = bool(p.returncode or bad_output)

    if dl := get_debug_logger():
        dl.log(
            level="ERROR" if failed else "DEBUG",
            operation="tool_run",
            message=f"{label} {'failed' if failed else 'ok'}",
            context={
                "label": label,
                "tool": Path(str_args[0]).name if str_args else None,
                "arg_count": len(str_args),
                "returncode": p.returncode,
                "duration_ms": round((time.monotonic() - start) * 1000, 1),
                "output": str(output) if output else None,
                "output_size": output.stat().st_size if output and output.exists() else 0,
                "bad_output": bad_output,
            },
        )

    if failed:
        if output is not None:
            output.unlink(missing_ok=True)
        raise RuntimeError(f"{label} failed: {stderr.decode(errors='replace')[-400:]}")
    return stderr
