"""Thin wrappers around dovi_tool subcommands used by DVFixup and Hybrid.

Centralises argv construction, status spinners, and error handling so callers do not
re-implement subprocess plumbing per call site. Each wrapper:

- Resolves `binaries.DoviTool` and raises EnvironmentError if missing.
- Delegates to `core.utils.subprocess.run_step` for execution, output validation, and
  stderr-tail RuntimeError on failure.
- Returns captured stderr so callers can inspect specific failure modes (e.g. the
  MAX_PQ_LUMINANCE retry path in extract_rpu).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional

from unshackle.core import binaries
from unshackle.core.utils.subprocess import log_tool_run, run_step


def _require_dovi_tool() -> str:
    if not binaries.DoviTool:
        raise EnvironmentError("dovi_tool executable was not found but is required.")
    return str(binaries.DoviTool)


def extract_rpu(
    source: Path,
    output: Path,
    *,
    mode: Optional[int] = 3,
    status: Optional[str] = "Extracting DV RPU...",
    label: str = "dovi_tool extract-rpu",
) -> bytes:
    """Extract DV RPU NALs from a raw HEVC stream. `mode=None` skips the -m flag (untouched)."""
    tool = _require_dovi_tool()
    args: list = [tool]
    if mode is not None:
        args += ["-m", str(mode)]
    args += ["extract-rpu", source, "-o", output]
    return run_step(args, status=status, output=output, label=label)


def inject_rpu(
    source: Path,
    rpu: Path,
    output: Path,
    *,
    status: Optional[str] = "Re-injecting DV RPU...",
    label: str = "dovi_tool inject-rpu",
) -> bytes:
    """Inject a DV RPU back into a raw HEVC stream, producing DV-signaled output."""
    tool = _require_dovi_tool()
    return run_step(
        [tool, "inject-rpu", "-i", source, "--rpu-in", rpu, "-o", output],
        status=status,
        output=output,
        label=label,
    )


def editor(
    source: Path,
    json_spec: Path,
    output: Path,
    *,
    status: Optional[str] = "Editing DV RPU...",
    label: str = "dovi_tool editor",
) -> bytes:
    """Apply a JSON edit spec to an RPU file."""
    tool = _require_dovi_tool()
    return run_step(
        [tool, "editor", "-i", source, "-j", json_spec, "-o", output],
        status=status,
        output=output,
        label=label,
    )


def info_summary(rpu: Path) -> str:
    """Return the textual summary (`dovi_tool info -i ... -s`) for an RPU file."""
    tool = _require_dovi_tool()
    info_start = time.monotonic()
    p = subprocess.run([tool, "info", "-i", str(rpu), "-s"], capture_output=True, text=True)
    log_tool_run(
        "dovi_tool info",
        "dovi_tool",
        p.returncode,
        duration_ms=round((time.monotonic() - info_start) * 1000, 1),
    )
    if p.returncode != 0:
        raise RuntimeError(f"dovi_tool info failed: {(p.stderr or '')[-400:]}")
    return p.stdout


def generate_from_hdr10plus(
    extra_json: Path,
    hdr10plus_json: Path,
    output: Path,
    *,
    status: Optional[str] = "Generating DV RPU from HDR10+ metadata...",
    label: str = "dovi_tool generate",
) -> bytes:
    """Build a DV RPU from extracted HDR10+ metadata + an extra JSON descriptor."""
    tool = _require_dovi_tool()
    return run_step(
        [tool, "generate", "-j", extra_json, "--hdr10plus-json", hdr10plus_json, "-o", output],
        status=status,
        output=output,
        label=label,
    )


def extract_rpu_with_fallback(source: Path, output: Path, *, label: str = "dovi_tool extract-rpu") -> bytes:
    """Try `-m 3` first; on MAX_PQ_LUMINANCE error, retry untouched (no -m). Returns stderr.

    Used when the caller wants automatic normalization but cannot abort if the source
    rejects mode-3 conversion.
    """
    try:
        return extract_rpu(source, output, mode=3, label=label)
    except RuntimeError as e:
        if "MAX_PQ_LUMINANCE" not in str(e):
            raise
        return extract_rpu(source, output, mode=None, status="Extracting DV RPU (untouched)...", label=label)


__all__ = (
    "extract_rpu",
    "extract_rpu_with_fallback",
    "inject_rpu",
    "editor",
    "info_summary",
    "generate_from_hdr10plus",
)
