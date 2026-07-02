"""
DV exposure for HLS composite HEVC streams.

Some services deliver DV Profile 8.1 in a stream whose primary CODECS is plain
hvc1, with DV advertised only via SUPPLEMENTAL-CODECS. The fMP4 carries valid DV RPU NALs
but the container does not signal DV, so muxing the MP4 directly produces an MKV that
mediainfo and DV-capable TVs see as plain HDR10/HDR10+.

The RPU is already valid — only the container's DV signaling is lost. Demuxing the
elementary HEVC stream (ffmpeg -c:v copy) exposes the in-stream RPU to mkvmerge, which then
signals DV in the muxed MKV. No dovi_tool extract/inject round-trip is needed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from rich.padding import Padding
from rich.rule import Rule

from unshackle.core.binaries import FFMPEG
from unshackle.core.console import console
from unshackle.core.utilities import log_event
from unshackle.core.utils.subprocess import run_step

if TYPE_CHECKING:
    from unshackle.core.tracks import Video


class DVFixup:
    """Demux a DV-composite HEVC track to its elementary stream so mkvmerge exposes DV."""

    def __init__(self, video: "Video") -> None:
        self.log = logging.getLogger("dv-fixup")
        self.video = video

        if not FFMPEG:
            raise EnvironmentError("ffmpeg is required for DV-composite fixup but was not found.")
        if not video.path or not Path(video.path).exists():
            raise ValueError(f"Video track {video.id} was not downloaded before DV fixup.")

    def run(self) -> Path:
        """Demux the elementary HEVC so mkvmerge exposes its DV RPU. Returns the
        elementary-stream path, or the original source on any failure so muxing can
        proceed with the as-downloaded file."""
        source = Path(self.video.path)
        height = self.video.height or 0
        console.print(Padding(Rule(f"[rule.text]DV Composite Fixup ({height}p)"), (1, 2)))

        fixed_hevc = source.with_name(f"{self.video.id}.dv.hevc")
        if fixed_hevc.exists() and fixed_hevc.stat().st_size > 0:
            self.log.info("✓ DV signaling already exposed (reusing existing demux)")
            return fixed_hevc

        try:
            run_step(
                [FFMPEG, "-nostdin", "-y", "-i", source, "-c:v", "copy", "-f", "hevc", fixed_hevc],
                status="Demuxing HEVC bitstream to expose DV...",
                output=fixed_hevc,
                label="ffmpeg demux",
            )
        except Exception as e:
            self.log.warning(f"DV fixup failed ({e}); muxing source as-is.")
            log_event(
                "dv_fixup",
                level="WARNING",
                message="DV fixup failed; falling back to source",
                context={"error": str(e), "source": str(source)},
            )
            fixed_hevc.unlink(missing_ok=True)
            return source

        self.log.info("✓ DV signaling exposed")
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
