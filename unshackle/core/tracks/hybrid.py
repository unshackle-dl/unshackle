import json
import logging
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from rich.padding import Padding
from rich.rule import Rule

from unshackle.core.binaries import FFMPEG, FFProbe, HDR10PlusTool
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.utilities import log_event
from unshackle.core.utils import dovi
from unshackle.core.utils.subprocess import run_step


class Hybrid:
    def __init__(self, videos, source) -> None:
        self.log = logging.getLogger("hybrid")

        """
            Takes the Dolby Vision and HDR10(+) streams out of the VideoTracks.
            It will then attempt to inject the Dolby Vision metadata layer to the HDR10(+) stream.
            If no DV track is available but HDR10+ is present, it will convert HDR10+ to DV.
            """
        global directories
        from unshackle.core.tracks import Video

        self.videos = videos
        self.source = source
        self.rpu_file = "RPU.bin"
        self.hdr_type = "HDR10"
        self.hevc_file = f"{self.hdr_type}-DV.hevc"
        self.hdr10plus_to_dv = False
        self.hdr10plus_file = "HDR10Plus.json"

        # Get resolution info from HDR10 track for display
        hdr10_track = next((v for v in videos if v.range == Video.Range.HDR10), None)
        hdr10p_track = next((v for v in videos if v.range == Video.Range.HDR10P), None)
        track_for_res = hdr10_track or hdr10p_track
        self.resolution = f"{track_for_res.height}p" if track_for_res and track_for_res.height else "Unknown"

        console.print(Padding(Rule(f"[rule.text]HDR10+DV Hybrid ({self.resolution})"), (1, 2)))

        log_event(
            "hybrid_init",
            level="DEBUG",
            message="Starting HDR10+DV hybrid processing",
            context={
                "source": source,
                "resolution": self.resolution,
                "video_count": len(videos),
                "video_ranges": [str(v.range) for v in videos],
            },
        )

        for video in self.videos:
            if not video.path or not os.path.exists(video.path):
                raise ValueError(f"Video track {video.id} was not downloaded before injection.")

        # Check if we have DV track available
        has_dv = any(video.range == Video.Range.DV for video in self.videos)
        has_hdr10 = any(video.range == Video.Range.HDR10 for video in self.videos)
        has_hdr10p = any(video.range == Video.Range.HDR10P for video in self.videos)

        if not has_hdr10 and not has_hdr10p:
            raise ValueError("No HDR10 or HDR10+ track available for hybrid processing.")

        # If we have HDR10+ but no DV, we can convert HDR10+ to DV
        if not has_dv and has_hdr10p:
            console.status("No DV track found, but HDR10+ is available. Will convert HDR10+ to DV.")
            self.hdr10plus_to_dv = True
        elif not has_dv:
            raise ValueError("No DV track available and no HDR10+ to convert.")

        if os.path.isfile(config.directories.temp / self.hevc_file):
            console.status("Already Injected")
            return

        for video in videos:
            # Use the actual path from the video track
            save_path = video.path
            if not save_path or not os.path.exists(save_path):
                raise ValueError(f"Video track {video.id} was not downloaded or path not found: {save_path}")

            if video.range == Video.Range.HDR10:
                self.extract_stream(save_path, "HDR10")
            elif video.range == Video.Range.HDR10P:
                self.extract_stream(save_path, "HDR10")
                self.hdr_type = "HDR10+"
            elif video.range == Video.Range.DV:
                self.extract_stream(save_path, "DV")

        if self.hdr10plus_to_dv:
            # Extract HDR10+ metadata and convert to DV
            hdr10p_video = next(v for v in videos if v.range == Video.Range.HDR10P)
            self.extract_hdr10plus(hdr10p_video)
            self.convert_hdr10plus_to_dv()
        else:
            # Regular DV extraction
            dv_video = next(v for v in videos if v.range == Video.Range.DV)
            self.extract_rpu(dv_video)
            if os.path.isfile(config.directories.temp / "RPU_UNT.bin"):
                self.rpu_file = "RPU_UNT.bin"
                # Mode 3 conversion already done during extraction when not untouched
            elif os.path.isfile(config.directories.temp / "RPU.bin"):
                # RPU already extracted with mode 3
                pass

        # Edit L6 with actual luminance values from RPU, then L5 active area
        self.level_6()
        base_video = next((v for v in videos if v.range in (Video.Range.HDR10, Video.Range.HDR10P)), None)
        if base_video and base_video.path:
            self.level_5(base_video.path)

        self.injecting()

        log_event(
            "hybrid_complete",
            level="INFO",
            message="Injection Completed",
            context={
                "hdr_type": self.hdr_type,
                "resolution": self.resolution,
                "hdr10plus_to_dv": self.hdr10plus_to_dv,
                "rpu_file": self.rpu_file,
                "output_file": self.hevc_file,
            },
        )
        self.log.info("✓ Injection Completed")
        if self.source == ("itunes" or "appletvplus"):
            Path.unlink(config.directories.temp / "hdr10.mkv")
            Path.unlink(config.directories.temp / "dv.mkv")
        Path.unlink(config.directories.temp / "HDR10.hevc", missing_ok=True)
        Path.unlink(config.directories.temp / "DV.hevc", missing_ok=True)
        for rpu_name in ("RPU.bin", "RPU_UNT.bin", "RPU_L5.bin", "RPU_L6.bin"):
            Path.unlink(config.directories.temp / rpu_name, missing_ok=True)
        Path.unlink(config.directories.temp / "L5.json", missing_ok=True)
        Path.unlink(config.directories.temp / "L6.json", missing_ok=True)

    def extract_stream(self, save_path, type_):
        output = Path(config.directories.temp / f"{type_}.hevc")
        try:
            run_step(
                [FFMPEG or "ffmpeg", "-nostdin", "-y", "-i", save_path, "-c:v", "copy", output],
                status=f"Extracting {type_} stream...",
                output=output,
                label=f"ffmpeg extract {type_}",
            )
        except RuntimeError as e:
            log_event(
                "hybrid_extract_stream",
                level="ERROR",
                message=f"Failed extracting {type_} stream",
                context={"type": type_, "input": str(save_path), "output": str(output), "error": str(e)},
            )
            self.log.error(f"x Failed extracting {type_} stream")
            sys.exit(1)

        log_event(
            "hybrid_extract_stream",
            level="DEBUG",
            message=f"Extracted {type_} stream",
            context={"type": type_, "input": str(save_path), "output": str(output)},
            success=True,
        )

    def extract_rpu(self, video, untouched=False):
        if os.path.isfile(config.directories.temp / "RPU.bin") or os.path.isfile(
            config.directories.temp / "RPU_UNT.bin"
        ):
            return

        rpu_name = "RPU_UNT" if untouched else "RPU"
        rpu_path = config.directories.temp / f"{rpu_name}.bin"
        dv_stream = config.directories.temp / "DV.hevc"
        spinner = f"Extracting{' untouched ' if untouched else ' '}RPU from Dolby Vision stream..."

        try:
            dovi.extract_rpu(dv_stream, rpu_path, mode=None if untouched else 3, status=spinner)
        except RuntimeError as e:
            stderr_text = str(e)
            log_event(
                "hybrid_extract_rpu",
                level="ERROR",
                message=f"Failed extracting{' untouched ' if untouched else ' '}RPU",
                context={"untouched": untouched, "error": stderr_text},
            )
            if "MAX_PQ_LUMINANCE" in stderr_text:
                self.extract_rpu(video, untouched=True)
                return
            if "Invalid PPS index" in stderr_text:
                raise ValueError("Dolby Vision VideoTrack seems to be corrupt")
            raise ValueError(f"Failed extracting{' untouched ' if untouched else ' '}RPU from Dolby Vision stream")

        log_event(
            "hybrid_extract_rpu",
            level="DEBUG",
            message=f"Extracted{' untouched ' if untouched else ' '}RPU from Dolby Vision stream",
            context={"untouched": untouched, "output": f"{rpu_name}.bin"},
            success=True,
        )

    def level_5(self, input_video):
        """Generate Level 5 active area metadata via crop detection on the HDR10 stream.

        This resolves mismatches where DV has no black bars but HDR10 does (or vice versa)
        by telling the display the correct active area.
        """
        if os.path.isfile(config.directories.temp / "RPU_L5.bin"):
            return

        ffprobe_bin = str(FFProbe) if FFProbe else "ffprobe"
        ffmpeg_bin = str(FFMPEG) if FFMPEG else "ffmpeg"

        # Get video duration for random sampling
        with console.status("Detecting active area (crop detection)...", spinner="dots"):
            result_duration = subprocess.run(
                [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(input_video)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if result_duration.returncode != 0:
                log_event(
                    "hybrid_level5",
                    level="WARNING",
                    message="Could not probe video duration",
                    context={"returncode": result_duration.returncode, "stderr": (result_duration.stderr or "")},
                )
                self.log.warning("Could not probe video duration, skipping L5 crop detection")
                return

            duration_info = json.loads(result_duration.stdout)
            duration = float(duration_info["format"]["duration"])

            # Get video resolution for proper border calculation
            result_streams = subprocess.run(
                [
                    ffprobe_bin,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    str(input_video),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if result_streams.returncode != 0:
                log_event(
                    "hybrid_level5",
                    level="WARNING",
                    message="Could not probe video resolution",
                    context={"returncode": result_streams.returncode, "stderr": (result_streams.stderr or "")},
                )
                self.log.warning("Could not probe video resolution, skipping L5 crop detection")
                return

            stream_info = json.loads(result_streams.stdout)
            original_width = int(stream_info["streams"][0]["width"])
            original_height = int(stream_info["streams"][0]["height"])

            # Sample 10 random timestamps and run cropdetect on each
            random_times = sorted(random.uniform(0, duration) for _ in range(10))

            crop_results = []
            for t in random_times:
                result_cropdetect = subprocess.run(
                    [
                        ffmpeg_bin,
                        "-y",
                        "-nostdin",
                        "-loglevel",
                        "info",
                        "-ss",
                        f"{t:.2f}",
                        "-i",
                        str(input_video),
                        "-vf",
                        "cropdetect=round=2",
                        "-vframes",
                        "10",
                        "-f",
                        "null",
                        "-",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                # cropdetect outputs crop=w:h:x:y
                crop_match = re.search(
                    r"crop=(\d+):(\d+):(\d+):(\d+)",
                    (result_cropdetect.stdout or "") + (result_cropdetect.stderr or ""),
                )
                if crop_match:
                    w, h = int(crop_match.group(1)), int(crop_match.group(2))
                    x, y = int(crop_match.group(3)), int(crop_match.group(4))
                    # Calculate actual border sizes from crop geometry
                    left = x
                    top = y
                    right = original_width - w - x
                    bottom = original_height - h - y
                    crop_results.append((left, top, right, bottom))

        if not crop_results:
            log_event(
                "hybrid_level5",
                level="WARNING",
                message="No crop data detected, skipping L5",
                context={"samples": len(random_times)},
            )
            self.log.warning("No crop data detected, skipping L5")
            return

        # Find the most common crop values
        crop_counts = {}
        for crop in crop_results:
            crop_counts[crop] = crop_counts.get(crop, 0) + 1
        most_common = max(crop_counts, key=crop_counts.get)
        left, top, right, bottom = most_common      # frame instead of leaving phantom bars from the source.

        l5_json = {
            "active_area": {
                "crop": False,
                "presets": [{"id": 0, "left": left, "right": right, "top": top, "bottom": bottom}],
                "edits": {"all": 0},
            }
        }

        l5_path = config.directories.temp / "L5.json"
        with open(l5_path, "w") as f:
            json.dump(l5_json, f, indent=4)

        try:
            dovi.editor(
                config.directories.temp / self.rpu_file,
                l5_path,
                config.directories.temp / "RPU_L5.bin",
                status="Editing RPU Level 5 active area...",
                label="dovi_tool editor (L5)",
            )
        except RuntimeError as e:
            log_event(
                "hybrid_level5",
                level="ERROR",
                message="Failed editing RPU Level 5 values",
                context={"error": str(e)},
            )
            raise ValueError("Failed editing RPU Level 5 values")

        log_event(
            "hybrid_level5",
            level="DEBUG",
            message="Edited RPU Level 5 active area",
            context={
                "crop": {"left": left, "right": right, "top": top, "bottom": bottom},
                "samples": len(crop_results),
            },
            success=True,
        )
        self.rpu_file = "RPU_L5.bin"

    @staticmethod
    def sanitize_l6(
        max_mdl: Optional[int], min_mdl: Optional[int], max_cll: Optional[int], max_fall: Optional[int]
    ) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        """Clamp static L6 values to a valid relationship.

        MaxCLL must not exceed the mastering-display peak (some sources, e.g. ATV
        HDR10+, ship MaxCLL 10000 on a 1000-nit master), and MaxFALL must not exceed
        MaxCLL. A value of 0 means "unknown" and is preserved as-is.
        """
        if max_mdl and max_cll and max_cll > max_mdl:
            max_cll = max_mdl
        if max_cll and max_fall and max_fall > max_cll:
            max_fall = max_cll
        return max_mdl, min_mdl, max_cll, max_fall

    def level_6(self):
        """Edit RPU Level 6 values using the static L6 luminance data from the RPU."""
        if os.path.isfile(config.directories.temp / "RPU_L6.bin"):
            return

        try:
            with console.status("Reading RPU luminance metadata...", spinner="dots"):
                info_text = dovi.info_summary(config.directories.temp / self.rpu_file)
        except RuntimeError as e:
            log_event(
                "hybrid_level6",
                level="ERROR",
                message="Failed reading RPU metadata for Level 6 values",
                context={"error": str(e)},
            )
            raise ValueError("Failed reading RPU metadata for Level 6 values")

        max_cll = None
        max_fall = None
        max_mdl = None
        min_mdl = None

        in_l6 = False
        for line in info_text.splitlines():
            stripped = line.strip()
            if "L6 metadata" in stripped:
                in_l6 = True
            if stripped.startswith("RPU mastering display:"):
                mastering = stripped.split(":", 1)[1].strip()
                min_lum, max_lum = mastering.split("/")[0], mastering.split("/")[1].split(" ")[0]
                min_mdl = int(float(min_lum) * 10000)
                max_mdl = int(float(max_lum))
            elif in_l6 and "MaxCLL:" in stripped and max_cll is None:
                max_cll = int(float(stripped.split("MaxCLL:")[1].split("nits")[0].strip().rstrip(",")))
                if "MaxFALL:" in stripped:
                    max_fall = int(float(stripped.split("MaxFALL:")[1].split("nits")[0].strip().rstrip(",")))

        if any(v is None for v in (max_cll, max_fall, max_mdl, min_mdl)):
            base_max_mdl, base_min_mdl, base_cll, base_fall = self._probe_hdr_metadata()
            if max_cll is None:
                max_cll = base_cll
            if max_fall is None:
                max_fall = base_fall
            if max_mdl is None:
                max_mdl = base_max_mdl
            if min_mdl is None:
                min_mdl = base_min_mdl

        max_mdl, min_mdl, max_cll, max_fall = self.sanitize_l6(max_mdl, min_mdl, max_cll, max_fall)

        if any(v is None for v in (max_cll, max_fall, max_mdl, min_mdl)):
            log_event(
                "hybrid_level6",
                level="ERROR",
                message="Could not extract Level 6 luminance data from RPU",
                context={"max_cll": max_cll, "max_fall": max_fall, "max_mdl": max_mdl, "min_mdl": min_mdl},
            )
            raise ValueError("Could not extract Level 6 luminance data from RPU")

        level6_data = {
            "level6": {
                "remove_cmv4": False,
                "remove_mapping": False,
                "max_display_mastering_luminance": max_mdl,
                "min_display_mastering_luminance": min_mdl,
                "max_content_light_level": max_cll,
                "max_frame_average_light_level": max_fall,
            }
        }

        l6_path = config.directories.temp / "L6.json"
        with open(l6_path, "w") as f:
            json.dump(level6_data, f, indent=4)

        try:
            dovi.editor(
                config.directories.temp / self.rpu_file,
                l6_path,
                config.directories.temp / "RPU_L6.bin",
                status="Editing RPU Level 6 values...",
                label="dovi_tool editor (L6)",
            )
        except RuntimeError as e:
            log_event(
                "hybrid_level6",
                level="ERROR",
                message="Failed editing RPU Level 6 values",
                context={"error": str(e)},
            )
            raise ValueError("Failed editing RPU Level 6 values")

        log_event(
            "hybrid_level6",
            level="DEBUG",
            message="Edited RPU Level 6 luminance values",
            context={
                "max_cll": max_cll,
                "max_fall": max_fall,
                "max_mdl": max_mdl,
                "min_mdl": min_mdl,
            },
            success=True,
        )
        self.rpu_file = "RPU_L6.bin"

    def injecting(self):
        if os.path.isfile(config.directories.temp / self.hevc_file):
            return

        try:
            dovi.inject_rpu(
                config.directories.temp / "HDR10.hevc",
                config.directories.temp / self.rpu_file,
                config.directories.temp / self.hevc_file,
                status=f"Injecting Dolby Vision metadata into {self.hdr_type} stream...",
                label="dovi_tool inject-rpu",
            )
        except RuntimeError as e:
            log_event(
                "hybrid_inject_rpu",
                level="ERROR",
                message="Failed injecting Dolby Vision metadata into HDR10 stream",
                context={"error": str(e)},
            )
            raise ValueError("Failed injecting Dolby Vision metadata into HDR10 stream")

        log_event(
            "hybrid_inject_rpu",
            level="DEBUG",
            message=f"Injected Dolby Vision metadata into {self.hdr_type} stream",
            context={
                "hdr_type": self.hdr_type,
                "rpu_file": self.rpu_file,
                "output": self.hevc_file,
                "drop_hdr10plus": self.hdr10plus_to_dv,
            },
            success=True,
        )

    def extract_hdr10plus(self, _video):
        """Extract HDR10+ metadata from the video stream"""
        if os.path.isfile(config.directories.temp / self.hdr10plus_file):
            return

        if not HDR10PlusTool:
            raise ValueError("HDR10Plus_tool not found. Please install it to use HDR10+ to DV conversion.")

        try:
            run_step(
                [
                    HDR10PlusTool,
                    "extract",
                    config.directories.temp / "HDR10.hevc",
                    "-o",
                    config.directories.temp / self.hdr10plus_file,
                ],
                status="Extracting HDR10+ metadata...",
                output=config.directories.temp / self.hdr10plus_file,
                label="hdr10plus_tool extract",
            )
        except RuntimeError as e:
            log_event(
                "hybrid_extract_hdr10plus",
                level="ERROR",
                message="Failed extracting HDR10+ metadata",
                context={"error": str(e)},
            )
            raise ValueError("Failed extracting HDR10+ metadata")

        file_size = os.path.getsize(config.directories.temp / self.hdr10plus_file)
        if file_size == 0:
            log_event(
                "hybrid_extract_hdr10plus",
                level="ERROR",
                message="No HDR10+ metadata found in the stream",
                context={"file_size": 0},
            )
            raise ValueError("No HDR10+ metadata found in the stream")

        log_event(
            "hybrid_extract_hdr10plus",
            level="DEBUG",
            message="Extracted HDR10+ metadata",
            context={"output": self.hdr10plus_file, "file_size": file_size},
            success=True,
        )

    def _probe_hdr_metadata(self):
        """Extract mastering display and content light level metadata from the HDR10 stream via ffprobe.

        Returns (max_mdl, min_mdl, max_cll, max_fall) in dovi_tool level6 units:
        - max_mdl: nits (integer)
        - min_mdl: 0.0001 nit units (integer)
        - max_cll / max_fall: nits (integer)
        """
        ffprobe_bin = str(FFProbe) if FFProbe else "ffprobe"
        result = subprocess.run(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream_side_data=max_luminance,min_luminance,max_content,max_average",
                "-of",
                "json",
                str(config.directories.temp / "HDR10.hevc"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        max_mdl = 1000
        min_mdl = 1
        max_cll = 0
        max_fall = 0

        if result.returncode == 0 and result.stdout:
            try:
                probe = json.loads(result.stdout)
                for stream in probe.get("streams", []):
                    for sd in stream.get("side_data_list", []):
                        if "max_luminance" in sd:
                            num, den = sd["max_luminance"].split("/")
                            max_mdl = int(int(num) / int(den))
                        if "min_luminance" in sd:
                            num, den = sd["min_luminance"].split("/")
                            min_mdl = int(int(num) / int(den) * 10000)
                        if "max_content" in sd:
                            max_cll = int(sd["max_content"])
                        if "max_average" in sd:
                            max_fall = int(sd["max_average"])
            except (json.JSONDecodeError, KeyError, ValueError, ZeroDivisionError):
                pass

        log_event(
            "hybrid_probe_hdr_metadata",
            level="DEBUG",
            message="Probed HDR metadata from source stream",
            context={"max_mdl": max_mdl, "min_mdl": min_mdl, "max_cll": max_cll, "max_fall": max_fall},
        )

        return max_mdl, min_mdl, max_cll, max_fall

    def convert_hdr10plus_to_dv(self):
        """Convert HDR10+ metadata to Dolby Vision RPU"""
        if os.path.isfile(config.directories.temp / "RPU.bin"):
            return

        with console.status("Converting HDR10+ metadata to Dolby Vision...", spinner="dots"):
            # Extract actual HDR metadata from the source stream
            max_mdl, min_mdl, max_cll, max_fall = self._probe_hdr_metadata()
            max_mdl, min_mdl, max_cll, max_fall = self.sanitize_l6(max_mdl, min_mdl, max_cll, max_fall)

            # First create the extra metadata JSON for dovi_tool
            extra_metadata = {
                "cm_version": "V29",
                "length": 0,  # dovi_tool will figure this out
                "level6": {
                    "max_display_mastering_luminance": max_mdl,
                    "min_display_mastering_luminance": min_mdl,
                    "max_content_light_level": max_cll,
                    "max_frame_average_light_level": max_fall,
                },
            }

            with open(config.directories.temp / "extra.json", "w") as f:
                json.dump(extra_metadata, f, indent=2)

        try:
            dovi.generate_from_hdr10plus(
                config.directories.temp / "extra.json",
                config.directories.temp / self.hdr10plus_file,
                config.directories.temp / "RPU.bin",
                label="dovi_tool generate",
            )
        except RuntimeError as e:
            log_event(
                "hybrid_convert_hdr10plus",
                level="ERROR",
                message="Failed converting HDR10+ to Dolby Vision",
                context={"error": str(e)},
            )
            raise ValueError("Failed converting HDR10+ to Dolby Vision")

        log_event(
            "hybrid_convert_hdr10plus",
            level="DEBUG",
            message="Converted HDR10+ metadata to Dolby Vision Profile 8",
            success=True,
        )

        # Clean up temporary files
        Path.unlink(config.directories.temp / "extra.json")
        Path.unlink(config.directories.temp / self.hdr10plus_file)
