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
    """Send a structured ``tool_run`` debug-log entry for an external tool invocation.

    Central helper so every binary call (FFmpeg, mkvpropedit, dovi_tool, and the other
    tools) logs the same shape. No-op when debug logging is off.
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


FFMPEG_INCONCLUSIVE = (
    "not found for",
    "no decoder found",
    "sub-sample encryption info",
    "error reading header",
    "not a decoding option",
)

FFMPEG_MISSING_REFERENCE = (
    "mmco",
    "missing reference",
    "co located pocs unavailable",
    "reference picture missing",
    "could not find ref with poc",
)
FFMPEG_CHECK_TIMEOUT = 120


def ffmpeg_decodes(path: Path, start: Optional[float] = None, seconds: float = 3, video: Optional[bool] = None) -> bool:
    """Return True when FFmpeg decodes a window of the file without an error.

    A wrong content key leaves the container intact and the samples as noise, which the
    decoders reject. ``-xerror`` stops at the first error so the check stays short.

    With ``start``, the check decodes ``seconds`` from that time. An FFmpeg copy cuts the
    window first, so no sample after the window gets to the decoder. Without ``start``, the
    check decodes the first seconds and the last 4 seconds, because a title can open with
    a clear or separately keyed lead.

    Every frame is decoded, because an HEVC keyframe decrypted with a wrong key can decode
    without an error. When a window starts inside a GOP and the only error is a missing
    reference frame, the check decodes the keyframes of that window again and uses that
    verdict. ``video=False`` skips that second pass.

    A file this FFmpeg build cannot judge counts as a pass, never as a wrong key, and so
    does a check that does not finish in time.
    """
    if not binaries.FFMPEG:
        raise EnvironmentError('FFmpeg executable "ffmpeg" not found but is required.')
    ffmpeg = str(binaries.FFMPEG)

    def decode(window: list[str], keyframes: bool) -> bool:
        check = [ffmpeg, "-nostdin", "-v", "error", "-err_detect", "explode", "-xerror"]
        if keyframes:
            check += ["-skip_frame:v", "nokey"]
        cut = None
        try:
            if start is None:
                ff = subprocess.run(
                    [*check, *window, "-i", str(path), "-f", "null", "-"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=FFMPEG_CHECK_TIMEOUT,
                )
            else:
                # -ignore_editlist: the windows come from tfdt, and an elst would shift FFmpeg's clock off them
                cut = subprocess.Popen(
                    [ffmpeg, "-nostdin", "-v", "error", "-ignore_editlist", "1", *window, "-i", str(path)]
                    + ["-map", "0:v?", "-map", "0:a?", "-c", "copy", "-f", "nut", "pipe:1"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                ff = subprocess.run(
                    [*check, "-i", "pipe:", "-f", "null", "-"],
                    stdin=cut.stdout,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=FFMPEG_CHECK_TIMEOUT,
                )
        except subprocess.TimeoutExpired:
            return True
        finally:
            if cut:
                if cut.stdout:
                    cut.stdout.close()
                cut.kill()
                cut.wait()
        err = ff.stderr.lower()
        if cut and cut.returncode not in (0, -9) and "error opening input" in err:
            return True  # the cut failed, so no sample got to the decoder
        if ff.returncode == 0 or any(m in err for m in FFMPEG_INCONCLUSIVE):
            return True
        if not keyframes and video is not False and any(m in err for m in FFMPEG_MISSING_REFERENCE):
            return decode(window, keyframes=True)
        return False

    if start is not None:
        return decode(["-ss", f"{start:.6f}", "-t", f"{max(seconds - 0.5, seconds / 2):.6f}"], keyframes=False)
    return decode(["-t", str(seconds)], keyframes=False) and decode(["-sseof", "-4", "-t", "4"], keyframes=False)


def ffprobe(uri: Union[bytes, Path]) -> dict:
    """Use FFprobe on the provided data to get its track information (``-show_streams``)."""
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
    result = json.loads(ff.stdout.decode("utf-8", errors="replace"))
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
    """Operate a CLI step that writes to `output` (when provided). Returns stderr bytes.

    Raises RuntimeError with the stderr tail when the process exits non-zero, or when
    you give `output` and it does not exist or is empty after the step.
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
