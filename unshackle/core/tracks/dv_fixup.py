"""
DV fixup for HLS composite HEVC streams.

Some services deliver DV Profile 8.1 in a stream whose primary CODECS is plain
hvc1, with DV advertised only via SUPPLEMENTAL-CODECS. The fMP4 carries DV RPU NALs but
the container does not signal DV, so muxing produces an MKV that mediainfo and DV-capable
TVs see as plain HDR10/HDR10+.

A dovi_tool extract-rpu / inject-rpu round-trip rewrites the bitstream so it is recognised
as DV after muxing. HDR10+ SEI NALs and HDR10 base layer signaling survive untouched.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from rich.padding import Padding
from rich.rule import Rule

from unshackle.core.binaries import FFMPEG, DoviTool
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.utilities import log_event
from unshackle.core.utils import dovi
from unshackle.core.utils.subprocess import run_step

if TYPE_CHECKING:
    from unshackle.core.tracks import Video


class DVFixup:
    """Round-trip a DV-composite HEVC track through dovi_tool to restore DV signaling."""

    def __init__(self, video: "Video") -> None:
        self.log = logging.getLogger("dv-fixup")
        self.video = video

        if not DoviTool:
            raise EnvironmentError("dovi_tool is required for DV-composite fixup but was not found.")
        if not FFMPEG:
            raise EnvironmentError("ffmpeg is required for DV-composite fixup but was not found.")
        if not video.path or not Path(video.path).exists():
            raise ValueError(f"Video track {video.id} was not downloaded before DV fixup.")

    def run(self) -> Path:
        """Execute the fixup. Returns the DV-signaled HEVC path, or the original
        source path on any failure so muxing can proceed with the as-downloaded file."""
        source = Path(self.video.path)
        height = self.video.height or 0
        console.print(Padding(Rule(f"[rule.text]DV Composite Fixup ({height}p)"), (1, 2)))

        fixed_hevc = source.with_name(f"{self.video.id}.dv.hevc")
        if fixed_hevc.exists() and fixed_hevc.stat().st_size > 0:
            self.log.info("✓ DV signaling already restored (reusing existing fixup)")
            return fixed_hevc

        tmp = config.directories.temp
        tmp.mkdir(parents=True, exist_ok=True)
        suffix = f"{self.video.id}_{height or 'na'}"
        raw_hevc = tmp / f"dvfix_{suffix}.hevc"
        rpu = tmp / f"dvfix_{suffix}_rpu.bin"

        try:
            run_step(
                [FFMPEG, "-nostdin", "-y", "-i", source, "-c:v", "copy", "-f", "hevc", raw_hevc],
                status="Demuxing HEVC bitstream...",
                output=raw_hevc,
                label="ffmpeg demux",
            )
            dovi.extract_rpu_with_fallback(raw_hevc, rpu)
            dovi.inject_rpu(raw_hevc, rpu, fixed_hevc, status="Re-injecting DV RPU with proper signaling...")
        except Exception as e:
            self.log.warning(f"DV fixup failed ({e}); muxing source as-is.")
            log_event(
                "dv_fixup",
                level="WARNING",
                message="DV fixup failed; falling back to source",
                context={"error": str(e), "source": str(source)},
            )
            for leftover in (raw_hevc, rpu, fixed_hevc):
                leftover.unlink(missing_ok=True)
            return source

        for leftover in (raw_hevc, rpu):
            leftover.unlink(missing_ok=True)

        self.log.info("✓ DV signaling restored")
        log_event(
            "dv_fixup",
            level="INFO",
            message="DV fixup complete",
            context={"source": str(source), "output": str(fixed_hevc)},
            success=True,
        )
        return fixed_hevc


def apply_dv_fixup(video: "Video") -> None:
    """Run DV fixup on `video` if flagged as DV-composite. Updates `video.path` in place
    and deletes the original source file so the standard mux cleanup handles the new path."""
    if not getattr(video, "dv_compatible_bitstream", False):
        return
    if not video.path or not Path(video.path).exists():
        return
    original = Path(video.path)
    fixed = DVFixup(video).run()
    if fixed != original:
        video.path = fixed
        original.unlink(missing_ok=True)


__all__ = ("DVFixup", "apply_dv_fixup")
