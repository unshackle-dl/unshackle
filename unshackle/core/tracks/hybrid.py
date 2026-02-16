import json
import logging
import os
import random
import re
import subprocess
import sys
from pathlib import Path

from rich.padding import Padding
from rich.rule import Rule

from unshackle.core.binaries import FFMPEG, DoviTool, FFProbe, HDR10PlusTool
from unshackle.core.config import config
from unshackle.core.console import console


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

        for video in self.videos:
            if not video.path or not os.path.exists(video.path):
                raise ValueError(f"Video track {video.id} was not downloaded before injection.")

        # Check if we have DV track available
        has_dv = any(video.range == Video.Range.DV for video in self.videos)
        has_hdr10 = any(video.range == Video.Range.HDR10 for video in self.videos)
        has_hdr10p = any(video.range == Video.Range.HDR10P for video in self.videos)

        if not has_hdr10:
            raise ValueError("No HDR10 track available for hybrid processing.")

        # If we have HDR10+ but no DV, we can convert HDR10+ to DV
        if not has_dv and has_hdr10p:
            self.log.info("✓ No DV track found, but HDR10+ is available. Will convert HDR10+ to DV.")
            self.hdr10plus_to_dv = True
        elif not has_dv:
            raise ValueError("No DV track available and no HDR10+ to convert.")

        if os.path.isfile(config.directories.temp / self.hevc_file):
            self.log.info("✓ Already Injected")
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
        hdr10_video = next((v for v in videos if v.range == Video.Range.HDR10), None)
        hdr10_input = hdr10_video.path if hdr10_video else None
        if hdr10_input:
            self.level_5(hdr10_input)

        self.injecting()

        self.log.info("✓ Injection Completed")
        if self.source == ("itunes" or "appletvplus"):
            Path.unlink(config.directories.temp / "hdr10.mkv")
            Path.unlink(config.directories.temp / "dv.mkv")
        Path.unlink(config.directories.temp / "HDR10.hevc", missing_ok=True)
        Path.unlink(config.directories.temp / "DV.hevc", missing_ok=True)
        Path.unlink(config.directories.temp / f"{self.rpu_file}", missing_ok=True)
        Path.unlink(config.directories.temp / "RPU_L6.bin", missing_ok=True)
        Path.unlink(config.directories.temp / "RPU_L5.bin", missing_ok=True)
        Path.unlink(config.directories.temp / "L5.json", missing_ok=True)
        Path.unlink(config.directories.temp / "L6.json", missing_ok=True)

    def ffmpeg_simple(self, save_path, output):
        """Simple ffmpeg execution without progress tracking"""
        p = subprocess.run(
            [
                str(FFMPEG) if FFMPEG else "ffmpeg",
                "-nostdin",
                "-i",
                str(save_path),
                "-c:v",
                "copy",
                str(output),
                "-y",  # overwrite output
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return p.returncode

    def extract_stream(self, save_path, type_):
        output = Path(config.directories.temp / f"{type_}.hevc")

        with console.status(f"Extracting {type_} stream...", spinner="dots"):
            returncode = self.ffmpeg_simple(save_path, output)

        if returncode:
            output.unlink(missing_ok=True)
            self.log.error(f"x Failed extracting {type_} stream")
            sys.exit(1)

        self.log.info(f"Extracted {type_} stream")

    def extract_rpu(self, video, untouched=False):
        if os.path.isfile(config.directories.temp / "RPU.bin") or os.path.isfile(
            config.directories.temp / "RPU_UNT.bin"
        ):
            return

        with console.status(
            f"Extracting{' untouched ' if untouched else ' '}RPU from Dolby Vision stream...", spinner="dots"
        ):
            extraction_args = [str(DoviTool)]
            if not untouched:
                extraction_args += ["-m", "3"]
            extraction_args += [
                "extract-rpu",
                config.directories.temp / "DV.hevc",
                "-o",
                config.directories.temp / f"{'RPU' if not untouched else 'RPU_UNT'}.bin",
            ]

            rpu_extraction = subprocess.run(
                extraction_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        if rpu_extraction.returncode:
            Path.unlink(config.directories.temp / f"{'RPU' if not untouched else 'RPU_UNT'}.bin")
            if b"MAX_PQ_LUMINANCE" in rpu_extraction.stderr:
                self.extract_rpu(video, untouched=True)
            elif b"Invalid PPS index" in rpu_extraction.stderr:
                raise ValueError("Dolby Vision VideoTrack seems to be corrupt")
            else:
                raise ValueError(f"Failed extracting{' untouched ' if untouched else ' '}RPU from Dolby Vision stream")

        self.log.info(f"Extracted{' untouched ' if untouched else ' '}RPU from Dolby Vision stream")

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
            self.log.warning("No crop data detected, skipping L5")
            return

        # Find the most common crop values
        crop_counts = {}
        for crop in crop_results:
            crop_counts[crop] = crop_counts.get(crop, 0) + 1
        most_common = max(crop_counts, key=crop_counts.get)
        left, top, right, bottom = most_common

        # If all borders are 0 there's nothing to correct
        if left == 0 and top == 0 and right == 0 and bottom == 0:
            return

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

        with console.status("Editing RPU Level 5 active area...", spinner="dots"):
            result = subprocess.run(
                [
                    str(DoviTool),
                    "editor",
                    "-i",
                    str(config.directories.temp / self.rpu_file),
                    "-j",
                    str(l5_path),
                    "-o",
                    str(config.directories.temp / "RPU_L5.bin"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        if result.returncode:
            Path.unlink(config.directories.temp / "RPU_L5.bin", missing_ok=True)
            raise ValueError("Failed editing RPU Level 5 values")

        self.rpu_file = "RPU_L5.bin"

    def level_6(self):
        """Edit RPU Level 6 values using actual luminance data from the RPU."""
        if os.path.isfile(config.directories.temp / "RPU_L6.bin"):
            return

        with console.status("Reading RPU luminance metadata...", spinner="dots"):
            result = subprocess.run(
                [str(DoviTool), "info", "-i", str(config.directories.temp / self.rpu_file), "-s"],
                capture_output=True,
                text=True,
            )

        if result.returncode != 0:
            raise ValueError("Failed reading RPU metadata for Level 6 values")

        max_cll = None
        max_fall = None
        max_mdl = None
        min_mdl = None

        for line in result.stdout.splitlines():
            if "RPU content light level (L1):" in line:
                parts = line.split("MaxCLL:")[1].split(",")
                max_cll = int(float(parts[0].strip().split()[0]))
                if len(parts) > 1 and "MaxFALL:" in parts[1]:
                    max_fall = int(float(parts[1].split("MaxFALL:")[1].strip().split()[0]))
            elif "RPU mastering display:" in line:
                mastering = line.split(":", 1)[1].strip()
                min_lum, max_lum = mastering.split("/")[0], mastering.split("/")[1].split(" ")[0]
                min_mdl = int(float(min_lum) * 10000)
                max_mdl = int(float(max_lum))

        if any(v is None for v in (max_cll, max_fall, max_mdl, min_mdl)):
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

        with console.status("Editing RPU Level 6 values...", spinner="dots"):
            result = subprocess.run(
                [
                    str(DoviTool),
                    "editor",
                    "-i",
                    str(config.directories.temp / self.rpu_file),
                    "-j",
                    str(l6_path),
                    "-o",
                    str(config.directories.temp / "RPU_L6.bin"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        if result.returncode:
            Path.unlink(config.directories.temp / "RPU_L6.bin", missing_ok=True)
            raise ValueError("Failed editing RPU Level 6 values")

        self.rpu_file = "RPU_L6.bin"

    def injecting(self):
        if os.path.isfile(config.directories.temp / self.hevc_file):
            return

        with console.status(f"Injecting Dolby Vision metadata into {self.hdr_type} stream...", spinner="dots"):
            inject_cmd = [
                str(DoviTool),
                "inject-rpu",
                "-i",
                config.directories.temp / "HDR10.hevc",
                "--rpu-in",
                config.directories.temp / self.rpu_file,
            ]

            # If we converted from HDR10+, optionally remove HDR10+ metadata during injection
            # Default to removing HDR10+ metadata since we're converting to DV
            if self.hdr10plus_to_dv:
                inject_cmd.append("--drop-hdr10plus")
                self.log.info("  - Removing HDR10+ metadata during injection")

            inject_cmd.extend(["-o", config.directories.temp / self.hevc_file])

            inject = subprocess.run(
                inject_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        if inject.returncode:
            Path.unlink(config.directories.temp / self.hevc_file)
            raise ValueError("Failed injecting Dolby Vision metadata into HDR10 stream")

        self.log.info(f"Injected Dolby Vision metadata into {self.hdr_type} stream")

    def extract_hdr10plus(self, _video):
        """Extract HDR10+ metadata from the video stream"""
        if os.path.isfile(config.directories.temp / self.hdr10plus_file):
            return

        if not HDR10PlusTool:
            raise ValueError("HDR10Plus_tool not found. Please install it to use HDR10+ to DV conversion.")

        with console.status("Extracting HDR10+ metadata...", spinner="dots"):
            # HDR10Plus_tool needs raw HEVC stream
            extraction = subprocess.run(
                [
                    str(HDR10PlusTool),
                    "extract",
                    str(config.directories.temp / "HDR10.hevc"),
                    "-o",
                    str(config.directories.temp / self.hdr10plus_file),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        if extraction.returncode:
            raise ValueError("Failed extracting HDR10+ metadata")

        # Check if the extracted file has content
        if os.path.getsize(config.directories.temp / self.hdr10plus_file) == 0:
            raise ValueError("No HDR10+ metadata found in the stream")

        self.log.info("Extracted HDR10+ metadata")

    def convert_hdr10plus_to_dv(self):
        """Convert HDR10+ metadata to Dolby Vision RPU"""
        if os.path.isfile(config.directories.temp / "RPU.bin"):
            return

        with console.status("Converting HDR10+ metadata to Dolby Vision...", spinner="dots"):
            # First create the extra metadata JSON for dovi_tool
            extra_metadata = {
                "cm_version": "V29",
                "length": 0,  # dovi_tool will figure this out
                "level6": {
                    "max_display_mastering_luminance": 1000,
                    "min_display_mastering_luminance": 1,
                    "max_content_light_level": 0,
                    "max_frame_average_light_level": 0,
                },
            }

            with open(config.directories.temp / "extra.json", "w") as f:
                json.dump(extra_metadata, f, indent=2)

            # Generate DV RPU from HDR10+ metadata
            conversion = subprocess.run(
                [
                    str(DoviTool),
                    "generate",
                    "-j",
                    str(config.directories.temp / "extra.json"),
                    "--hdr10plus-json",
                    str(config.directories.temp / self.hdr10plus_file),
                    "-o",
                    str(config.directories.temp / "RPU.bin"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        if conversion.returncode:
            raise ValueError("Failed converting HDR10+ to Dolby Vision")

        self.log.info("Converted HDR10+ metadata to Dolby Vision")
        self.log.info("✓ HDR10+ successfully converted to Dolby Vision Profile 8")

        # Clean up temporary files
        Path.unlink(config.directories.temp / "extra.json")
        Path.unlink(config.directories.temp / self.hdr10plus_file)
