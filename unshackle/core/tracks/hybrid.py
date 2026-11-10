import json
import logging
import os
import re
import subprocess
import tempfile
from collections import Counter
from concurrent import futures
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional

from rich.padding import Padding
from rich.rule import Rule

from unshackle.core.binaries import FFMPEG, FFProbe, HDR10PlusTool
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.utilities import log_event, standard_resolution
from unshackle.core.utils import dovi
from unshackle.core.utils.subprocess import run_step

if TYPE_CHECKING:
    from unshackle.core.tracks.video import Video

# HDR10+ histogram slots that form the maxRGB distribution, as (DistributionIndex, fraction of pixels).
# Index 99 holds the 99.98% percentile. Index 5 (a luminance-Y value) and index 10 (the share of pixels
# at or under 100 nits) are not part of the distribution.
# config.generate_dv_mode values: unshackle's L1 with the CM v2.9 or the CM v4.0 avg floor, or dovi_tool's own L1.
GENERATE_DV_MODES = ("v29", "v40", "legacy")


def generate_dv_mode() -> str:
    """config.generate_dv_mode, checked against GENERATE_DV_MODES."""
    mode = config.generate_dv_mode
    if mode not in GENERATE_DV_MODES:
        raise ValueError(f"generate_dv_mode must be one of {', '.join(GENERATE_DV_MODES)}, not {mode!r}")
    return mode


HDR10PLUS_PERCENTILES = ((1, 0.01), (25, 0.25), (50, 0.5), (75, 0.75), (90, 0.9), (95, 0.95), (99, 0.9998))


ST2084 = (0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875)


def nits_to_pq(nits: float) -> float:
    """SMPTE ST 2084 inverse EOTF, as a 12-bit code value (0-4095)."""
    m1, m2, c1, c2, c3 = ST2084
    y = (max(nits, 0.0) / 10000) ** m1
    return 4095 * ((c1 + c2 * y) / (1 + c3 * y)) ** m2


def pq_to_nits(code: float, peak: int = 4095) -> float:
    """SMPTE ST 2084 EOTF of a PQ code value on a 0-``peak`` scale."""
    m1, m2, c1, c2, c3 = ST2084
    v = (max(code, 0.0) / peak) ** (1 / m2)
    return 10000 * (max(v - c1, 0.0) / (c2 - c3 * v)) ** (1 / m1)


# maxRGB as one PQ-coded 16-bit plane, with scene cuts marked. The picture is first averaged over cells of
# 1/32 of its width: against a studio RPU the L1 max of a shot is the peak of such cells (close to the
# 99.98% percentile of HDR10+), not of single pixels, and a finer cell reads up to 600 PQ codes too high.
# scdet scores PQ-coded video low, so its threshold is 4: the default of 10 found 6 of 18 studio cuts. The second branch applies
# the ST 2084 EOTF (65535 = 10000 nits), because MaxFALL is a mean of linear light. It prints to a file
# of its own: two filters that print to stdout mix their lines.
PQ_TO_LINEAR_LUT = "65535*pow(max(pow(val/65535\\,1/{1})-{2}\\,0)/({3}-{4}*pow(val/65535\\,1/{1}))\\,1/{0})".format(
    *ST2084
)
MEASURE_GRAPH = (
    "[0:v]{crop}scale=iw/32:ih/32:flags=area,scdet=threshold=4,"
    "scale=in_color_matrix=bt2020:in_range=tv:out_range=pc,format=gbrp16le,"
    "extractplanes=g+b+r[g][b][r];[g][b]blend=all_mode=lighten[m];[m][r]blend=all_mode=lighten,split[p][l];"
    "[p]signalstats,metadata=print:file=-[pq];"
    "[l]lut=y='" + PQ_TO_LINEAR_LUT + "',"
    "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file={linear}[linear]"
)


def frame_stats(lines: Iterable[str]) -> list[tuple[int, float, int, bool]]:
    """(min, avg, max, scene cut) of the PQ maxRGB of each frame as 16-bit codes, from MEASURE_GRAPH's stdout."""
    frames: list[tuple[int, float, int, bool]] = []
    block: dict[str, str] = {}

    def flush() -> None:
        if "YMIN" in block:
            frames.append((int(block["YMIN"]), float(block["YAVG"]), int(block["YMAX"]), "time" in block))

    for line in lines:
        if line.startswith("frame:"):
            flush()
            block = {}
        elif line.startswith("lavfi.") and "=" in line:
            key, _, value = line.strip().partition("=")
            block[key.rsplit(".", 1)[-1]] = value
    flush()
    return frames


# Half a second at 24 fps. A burst of flashes scores a scene cut on each flash.
MIN_SHOT_FRAMES = 12


def measured_shots(frames: list[tuple[int, float, int, bool]]) -> list[dict]:
    """One dovi_tool generator shot per scene, with L1 from the frame_stats values of its frames.

    The shot takes the lowest min, the highest max and the mean of the frame averages. A cut that
    comes less than MIN_SHOT_FRAMES after the last one is ignored.
    """
    cuts = [0]
    for i, frame in enumerate(frames):
        if frame[3] and i - cuts[-1] >= MIN_SHOT_FRAMES:
            cuts.append(i)
    cuts.append(len(frames))
    to_12_bit = 4095 / 65535
    shots = []
    for start, end in zip(cuts, cuts[1:]):
        shot = frames[start:end]
        level1 = {
            "min_pq": round(min(f[0] for f in shot) * to_12_bit),
            "max_pq": round(max(f[2] for f in shot) * to_12_bit),
            "avg_pq": round(sum(f[1] for f in shot) / len(shot) * to_12_bit),
        }
        shots.append({"start": start, "duration": end - start, "metadata_blocks": [{"Level1": level1}]})
    return shots


def hdr10plus_mean_pq(values: dict[int, float], active_fraction: float = 1.0) -> Optional[float]:
    """Mean PQ code of the maxRGB distribution in one HDR10+ histogram, or None if a slot is missing.

    Dolby Vision L1 avg is a mean of PQ-coded maxRGB over the active area. HDR10+ AverageMaxRGB is a
    linear mean over the whole frame, so the mean comes from the percentiles instead. The darkest
    ``1 - active_fraction`` of the pixels are the letterbox bars, and the mean leaves them out. A scene
    whose 1st percentile is above black has no bars, so its whole frame counts.
    """
    if any(index not in values for index, _ in HDR10PLUS_PERCENTILES):
        return None
    if values[1] > 0:
        active_fraction = 1.0
    knots = [(0.0, nits_to_pq(values[1]))]
    knots += [(fraction, nits_to_pq(values[index])) for index, fraction in HDR10PLUS_PERCENTILES]
    knots.append((1.0, knots[-1][1]))

    low = 1.0 - active_fraction
    area = 0.0
    for (x1, y1), (x2, y2) in zip(knots, knots[1:]):
        if x2 <= low:
            continue
        if x1 < low:
            y1 += (y2 - y1) * (low - x1) / (x2 - x1)
            x1 = low
        area += (x2 - x1) * (y1 + y2) / 2
    return area / active_fraction


def hdr10plus_shots(metadata: dict, active_fraction: float = 1.0) -> list[dict]:
    """One dovi_tool generator shot with L1 metadata per HDR10+ scene.

    Each scene keeps its own cut. max_pq is the histogram peak, as in dovi_tool's default peak source,
    without index 10: that slot is the share of pixels at or under 100 nits, not a brightness, and would
    put a near-black scene at 10 nits. Index 5, the 99.99% luminance, stays: against decoded pixels it
    tracked the true peak better than index 99 alone. MaxScl is the peak when the histogram is empty.
    A scene whose histogram has no complete distribution falls back to dovi_tool's average,
    PQ(AverageMaxRGB), capped at max_pq.
    """
    scenes = metadata["SceneInfo"]
    summary = metadata["SceneInfoSummary"]
    first_frames = summary["SceneFirstFrameIndex"]
    offset = first_frames[0]

    shots = []
    fallbacks = 0
    for first_frame, length in zip(first_frames, summary["SceneFrameNumbers"]):
        start = first_frame - offset
        luminance = scenes[start]["LuminanceParameters"]
        distribution = luminance["LuminanceDistributions"]
        values = {
            index: value / 10
            for index, value in zip(distribution["DistributionIndex"], distribution["DistributionValues"])
        }
        peaks = [value for index, value in values.items() if index != 10]
        max_pq = nits_to_pq(max(peaks or [v / 10 for v in luminance["MaxScl"]]))
        avg_pq = hdr10plus_mean_pq(values, active_fraction)
        if avg_pq is None:
            fallbacks += 1
            avg_pq = min(nits_to_pq(luminance["AverageRGB"] / 10), max_pq)
        shots.append(
            {
                "start": start,
                "duration": length,
                "metadata_blocks": [{"Level1": {"min_pq": 0, "max_pq": round(max_pq), "avg_pq": round(avg_pq)}}],
            }
        )
    if fallbacks:
        log_event(
            "hybrid_convert_hdr10plus",
            level="DEBUG",
            message="HDR10+ scenes without a full histogram use PQ(AverageMaxRGB) for L1 avg",
            context={"scenes": fallbacks, "total": len(shots)},
        )
    return shots


class NoHDR10PlusMetadata(ValueError):
    """A track labelled HDR10+ carries no HDR10+ metadata."""


class Hybrid:
    def __init__(self, videos: list["Video"], source: str, output_name: Optional[str] = None) -> None:
        self.log = logging.getLogger("hybrid")

        """
            Takes the Dolby Vision and HDR10(+) streams out of the VideoTracks.
            It will then attempt to inject the Dolby Vision metadata layer to the HDR10(+) stream.
            If no DV track is available, it makes the DV from the HDR10+ metadata, or from the
            decoded video when there is no HDR10+ or no hdr10plus_tool.
            """
        global directories
        from unshackle.core.tracks import Video

        self.videos = videos
        self.source = source
        self.rpu_file = "RPU.bin"
        self.hdr_type = "HDR10"
        self.hevc_file = output_name or f"{self.hdr_type}-DV.hevc"
        self.hdr10plus_to_dv = False
        self.hdr10plus_file = "HDR10Plus.json"

        hdr10_track = next((v for v in videos if v.range == Video.Range.HDR10), None)
        hdr10p_track = next((v for v in videos if v.range == Video.Range.HDR10P), None)
        track_for_res = hdr10_track or hdr10p_track
        self.resolution = (
            f"{standard_resolution(track_for_res.width, track_for_res.height)}p"
            if track_for_res and track_for_res.height
            else "Unknown"
        )

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

        has_dv = any(video.range == Video.Range.DV for video in self.videos)
        has_hdr10 = any(video.range == Video.Range.HDR10 for video in self.videos)
        has_hdr10p = any(video.range == Video.Range.HDR10P for video in self.videos)

        if not has_hdr10 and not has_hdr10p:
            raise ValueError("No HDR10 or HDR10+ track available for hybrid processing.")

        if not has_dv:
            self.hdr10plus_to_dv = has_hdr10p and bool(HDR10PlusTool)
            if has_hdr10p and not self.hdr10plus_to_dv:
                self.log.warning("hdr10plus_tool not found, measuring the decoded video instead of HDR10+")

        if os.path.isfile(config.directories.temp / self.hevc_file):
            console.status("Already Injected")
            return

        try:
            for video in videos:
                save_path = video.path
                if not save_path or not os.path.exists(save_path):
                    raise ValueError(f"Video track {video.id} was not downloaded or path not found: {save_path}")

                if video.range == Video.Range.HDR10:
                    self.extract_stream(save_path, "HDR10")
                elif video.range == Video.Range.HDR10P:
                    self.extract_stream(save_path, "HDR10")
                    self.hdr_type = "HDR10+"
                elif video.range == Video.Range.DV:
                    if not os.path.isfile(config.directories.temp / "DV.hevc"):
                        self.extract_stream(save_path, "DV")

            base_video = next((v for v in videos if v.range in (Video.Range.HDR10, Video.Range.HDR10P)), None)
            area = self.detect_active_area(base_video.path) if base_video and base_video.path else None

            if not has_dv:
                # a base that failed earlier in this title can leave its edited RPUs, which level_5/level_6 reuse
                for stale in ("RPU_L5.bin", "RPU_L6.bin"):
                    Path.unlink(config.directories.temp / stale, missing_ok=True)
            if self.hdr10plus_to_dv:
                hdr10p_video = next(v for v in videos if v.range == Video.Range.HDR10P)
                try:
                    self.extract_hdr10plus(hdr10p_video)
                except NoHDR10PlusMetadata:
                    self.log.warning("No HDR10+ metadata in the track, measuring the decoded video instead")
                    self.hdr10plus_to_dv = False
                else:
                    self.convert_hdr10plus_to_dv(area[1] if area else 1.0)
            if has_dv:
                dv_video = next(v for v in videos if v.range == Video.Range.DV)
                self.extract_rpu(dv_video)
                if os.path.isfile(config.directories.temp / "RPU_UNT.bin"):
                    self.rpu_file = "RPU_UNT.bin"
                    # Mode 3 conversion already done during extraction when not untouched
                elif os.path.isfile(config.directories.temp / "RPU.bin"):
                    # RPU already extracted with mode 3
                    pass
            elif not self.hdr10plus_to_dv:
                self.measure_dv(area[0] if area else None)

            self.level_6()
            if base_video and has_dv:
                self.align_rpu(base_video, dv_video)
            if area:
                self.level_5(area[0])

            self.injecting()
        finally:
            # The title goes on after a failed resolution, and level_5 reuses an RPU_L5.bin it finds,
            # so the files of this resolution's crop must not outlive it.
            for own in ("HDR10.hevc", "RPU_L5.bin", "L5.json", "L6.json", "RPU_ALIGN.bin", "ALIGN.json"):
                Path.unlink(config.directories.temp / own, missing_ok=True)
            if not has_dv:
                # The RPU comes from this base's own metadata or frames, so the next resolution must not reuse it.
                Path.unlink(config.directories.temp / "RPU.bin", missing_ok=True)
                Path.unlink(config.directories.temp / "RPU_L6.bin", missing_ok=True)

        log_event(
            "hybrid_complete",
            level="INFO",
            message="Injection Completed",
            context={
                "hdr_type": self.hdr_type,
                "resolution": self.resolution,
                "hdr10plus_to_dv": self.hdr10plus_to_dv,
                "measured_dv": not has_dv and not self.hdr10plus_to_dv,
                "rpu_file": self.rpu_file,
                "output_file": self.hevc_file,
            },
        )
        self.log.info("✓ Injection Completed")
        if self.source == ("itunes" or "appletvplus"):
            Path.unlink(config.directories.temp / "hdr10.mkv")
            Path.unlink(config.directories.temp / "dv.mkv")

    def extract_stream(self, save_path: Path, type_: str) -> None:
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
            raise ValueError(f"Failed extracting {type_} stream")

        log_event(
            "hybrid_extract_stream",
            level="DEBUG",
            message=f"Extracted {type_} stream",
            context={"type": type_, "input": str(save_path), "output": str(output)},
            success=True,
        )

    def extract_rpu(self, video: "Video", untouched: bool = False) -> None:
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

    def align_rpu(self, base: "Video", dv: "Video") -> None:
        """Shift the RPU so that it starts on the first frame of the base.

        --sample cuts each track on its own segment grid, so the DV track can start before or after the
        base. An RPU that starts early loses its first frames; one that starts late repeats its first frame.
        """
        seconds = dv.sample_start - base.sample_start
        if not seconds:
            return
        fps = float(base.fps or dv.fps or 0)
        if not fps:
            raise ValueError("--sample cut the HDR10 and Dolby Vision tracks at different points")
        frames = round(seconds * fps)
        if not frames:
            return
        edit: dict[str, Any] = (
            {"duplicate": [{"source": 0, "offset": 0, "length": frames}]}
            if frames > 0
            else {"remove": [f"0-{-frames - 1}"]}
        )
        align_path = config.directories.temp / "ALIGN.json"
        with open(align_path, "w", encoding="utf-8") as f:
            json.dump(edit, f)
        try:
            dovi.editor(
                config.directories.temp / self.rpu_file,
                align_path,
                config.directories.temp / "RPU_ALIGN.bin",
                status="Aligning the RPU with the sample...",
                label="dovi_tool editor (align)",
            )
        except RuntimeError as e:
            log_event("hybrid_align", level="ERROR", message="Failed aligning the RPU", context={"error": str(e)})
            raise ValueError("Failed aligning the Dolby Vision RPU with the sample")
        log_event("hybrid_align", level="DEBUG", message="Aligned the RPU", context={"frames": frames}, success=True)
        self.rpu_file = "RPU_ALIGN.bin"

    def level_5(self, crop: tuple[int, int, int, int]) -> None:
        """Write the active area of the HDR10 track, as (left, top, right, bottom), to Level 5.

        This resolves mismatches where DV has no black bars but HDR10 does (or vice versa)
        by telling the display the correct active area.
        """
        if os.path.isfile(config.directories.temp / "RPU_L5.bin"):
            self.rpu_file = "RPU_L5.bin"
            return

        left, top, right, bottom = crop

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
            context={"crop": {"left": left, "right": right, "top": top, "bottom": bottom}},
            success=True,
        )
        self.rpu_file = "RPU_L5.bin"

    def detect_active_area(self, input_video: Path) -> Optional[tuple[tuple[int, int, int, int], float]]:
        """The picture's crop as (left, top, right, bottom) and its share of the frame, or None.

        None also when the picture is under 40% of the frame: cropdetect on dark samples can report a tiny
        picture, which would be wrong in L5 and would divide the HDR10+ conversion's mean by a tiny share.
        """
        ffprobe_bin = str(FFProbe) if FFProbe else "ffprobe"
        ffmpeg_bin = str(FFMPEG) if FFMPEG else "ffmpeg"

        with console.status("Detecting active area (crop detection)...", spinner="dots"):
            result_duration = subprocess.run(
                [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(input_video)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            if result_duration.returncode != 0:
                log_event(
                    "hybrid_level5",
                    level="WARNING",
                    message="Could not probe video duration",
                    context={"returncode": result_duration.returncode, "stderr": (result_duration.stderr or "")},
                )
                self.log.warning("Could not probe video duration, skipping crop detection")
                return None

            duration_value = json.loads(result_duration.stdout).get("format", {}).get("duration")
            if duration_value is None:
                log_event(
                    "hybrid_level5",
                    level="WARNING",
                    message="Video has no container duration; RPU keeps the active area of the DV track",
                    context={"input": str(input_video)},
                )
                self.log.warning("Video has no container duration, skipping crop detection")
                return None
            duration = float(duration_value)

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
                encoding="utf-8",
                errors="replace",
            )

            if result_streams.returncode != 0:
                log_event(
                    "hybrid_level5",
                    level="WARNING",
                    message="Could not probe video resolution",
                    context={"returncode": result_streams.returncode, "stderr": (result_streams.stderr or "")},
                )
                self.log.warning("Could not probe video resolution, skipping crop detection")
                return None

            stream_info = json.loads(result_streams.stdout)
            original_width = int(stream_info["streams"][0]["width"])
            original_height = int(stream_info["streams"][0]["height"])

            sample_times = [duration * (i + 0.5) / 10 for i in range(10)]

            def sample_crop(t: float) -> Optional[tuple[int, int, int, int]]:
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
                        # cropdetect skips its first 2 frames and only ever grows the picture, so 12 frames
                        # give each sample 10 evaluated ones
                        "-vframes",
                        "12",
                        "-f",
                        "null",
                        "-",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )

                # cropdetect prints crop=w:h:x:y per frame and settles over the frames, so the last line counts
                crop_matches = re.findall(
                    r"crop=(\d+):(\d+):(\d+):(\d+)",
                    (result_cropdetect.stdout or "") + (result_cropdetect.stderr or ""),
                )
                if not crop_matches:
                    return None
                w, h, x, y = map(int, crop_matches[-1])
                return (x, y, original_width - w - x, original_height - h - y)

            with futures.ThreadPoolExecutor(max_workers=len(sample_times)) as pool:
                crop_results = [crop for crop in pool.map(sample_crop, sample_times) if crop]

        if not crop_results:
            log_event(
                "hybrid_level5",
                level="WARNING",
                message="No crop data detected",
                context={"samples": len(sample_times)},
            )
            self.log.warning("No crop data detected, skipping the active area")
            return None

        left, top, right, bottom = Counter(crop_results).most_common(1)[0][0]
        fraction = (
            (original_width - left - right) * (original_height - top - bottom) / (original_width * original_height)
        )
        if fraction < 0.4:
            self.log.warning("The detected picture is under 40% of the frame, skipping the active area")
            return None
        log_event(
            "hybrid_level5",
            level="DEBUG",
            message="Detected active area",
            context={
                "crop": {"left": left, "right": right, "top": top, "bottom": bottom},
                "samples": len(crop_results),
            },
        )
        return (left, top, right, bottom), fraction

    @staticmethod
    def sanitize_l6(
        max_mdl: Optional[int], min_mdl: Optional[int], max_cll: Optional[int], max_fall: Optional[int]
    ) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        """Clamp static L6 values to a valid relationship.

        MaxCLL must not exceed the mastering-display peak (some HDR10+ sources ship
        MaxCLL 10000 on a 1000-nit master), and MaxFALL must not exceed
        MaxCLL. A value of 0 means "unknown", and this method keeps it as-is.
        """
        if max_mdl and max_cll and max_cll > max_mdl:
            max_cll = max_mdl
        if max_cll and max_fall and max_fall > max_cll:
            max_fall = max_cll
        return max_mdl, min_mdl, max_cll, max_fall

    def level_6(self) -> None:
        """Edit RPU Level 6 values using the static L6 luminance data from the RPU."""
        if os.path.isfile(config.directories.temp / "RPU_L6.bin"):
            self.rpu_file = "RPU_L6.bin"
            return

        try:
            with console.status("Reading RPU luminance metadata...", spinner="dots"):
                rpu_info = dovi.info_frame(config.directories.temp / self.rpu_file)
        except RuntimeError as e:
            log_event(
                "hybrid_level6",
                level="ERROR",
                message="Failed reading RPU metadata for Level 6 values",
                context={"error": str(e)},
            )
            raise ValueError("Failed reading RPU metadata for Level 6 values")

        blocks = rpu_info.get("vdr_dm_data", {}).get("cmv29_metadata", {}).get("ext_metadata_blocks", [])
        level6: dict[str, Any] = next((b["Level6"] for b in blocks if isinstance(b, dict) and "Level6" in b), {})

        max_cll = level6.get("max_content_light_level")
        max_fall = level6.get("max_frame_average_light_level")
        max_mdl = level6.get("max_display_mastering_luminance")
        min_mdl = level6.get("min_display_mastering_luminance")

        rpu_values = (max_mdl, min_mdl, max_cll, max_fall)

        if not max_cll or not max_fall or max_mdl is None or min_mdl is None:
            base_max_mdl, base_min_mdl, base_cll, base_fall = self.probe_hdr_metadata()
            if not max_cll and base_cll:
                max_cll = base_cll
            if not max_fall and base_fall:
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

        if (max_mdl, min_mdl, max_cll, max_fall) == rpu_values:
            log_event(
                "hybrid_level6",
                level="DEBUG",
                message="Level 6 luminance values already correct, skipping edit",
                context={
                    "max_cll": max_cll,
                    "max_fall": max_fall,
                    "max_mdl": max_mdl,
                    "min_mdl": min_mdl,
                },
                success=True,
            )
            return

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

    def injecting(self) -> None:
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
            },
            success=True,
        )

    def extract_hdr10plus(self, video: "Video") -> None:
        """Extract HDR10+ metadata from the video track"""
        if os.path.isfile(config.directories.temp / self.hdr10plus_file):
            return

        if not HDR10PlusTool:
            raise ValueError("HDR10Plus_tool not found, HDR10+ to DV conversion needs it")

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
            if "dynamic metadata" in str(e):
                raise NoHDR10PlusMetadata("No HDR10+ metadata found in the stream")
            raise ValueError("Failed extracting HDR10+ metadata")

        file_size = os.path.getsize(config.directories.temp / self.hdr10plus_file)
        if file_size == 0:
            # left in place, the next resolution would take the empty file for its metadata
            Path.unlink(config.directories.temp / self.hdr10plus_file, missing_ok=True)
            log_event(
                "hybrid_extract_hdr10plus",
                level="ERROR",
                message="No HDR10+ metadata found in the stream",
                context={"file_size": 0},
            )
            raise NoHDR10PlusMetadata("No HDR10+ metadata found in the stream")

        log_event(
            "hybrid_extract_hdr10plus",
            level="DEBUG",
            message="Extracted HDR10+ metadata",
            context={"output": self.hdr10plus_file, "file_size": file_size},
            success=True,
        )

    def probe_hdr_metadata(self) -> tuple[int, int, int, int]:
        """Extract mastering display and Content Light Level metadata from the HDR10 track with FFprobe.

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
                "-read_intervals",
                "%+#1",
                "-show_frames",
                "-show_entries",
                "frame_side_data_list",
                "-of",
                "json",
                str(config.directories.temp / "HDR10.hevc"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        max_mdl = 1000
        min_mdl = 1
        max_cll = 0
        max_fall = 0

        if result.returncode == 0 and result.stdout:
            try:
                probe = json.loads(result.stdout)
                for frame in probe.get("frames", []):
                    for sd in frame.get("side_data_list", []):
                        if "max_luminance" in sd:
                            num, den = sd["max_luminance"].split("/")
                            max_mdl = int(int(num) / int(den))
                        if "min_luminance" in sd:
                            num, den = sd["min_luminance"].split("/")
                            # Integer math, because float rounding turns 0.0003 nits into 2 units.
                            min_mdl = int(num) * 10000 // int(den)
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

    def convert_hdr10plus_to_dv(self, active_fraction: float = 1.0) -> None:
        """Generate a Dolby Vision RPU with one shot per HDR10+ scene.

        By default unshackle builds the L1 of each shot itself (see hdr10plus_shots), because dovi_tool's
        --hdr10plus-json sets L1 avg from the linear AverageMaxRGB and so makes most scenes too bright.
        config.generate_dv_mode picks the L1 avg floor: v29 (about 2.4 nits, closest to the pixels) or v40
        (about 10 nits, as in studio CM v4.0 RPUs). legacy hands the HDR10+ JSON to dovi_tool instead.
        """
        mode = generate_dv_mode()
        hdr10plus_path = config.directories.temp / self.hdr10plus_file
        config_path = config.directories.temp / "generate.json"
        try:
            with console.status("Converting HDR10+ metadata to Dolby Vision...", spinner="dots"):
                with open(hdr10plus_path, encoding="utf-8") as f:
                    metadata = json.load(f)
                profile = metadata.get("JSONInfo", {}).get("HDR10plusProfile")
                shots = hdr10plus_shots(metadata, active_fraction)
                light = [frame["LuminanceParameters"] for frame in metadata["SceneInfo"]]
                generator_config = self.generator_config(
                    round(max(max(frame["MaxScl"]) for frame in light) / 10),
                    round(max(frame["AverageRGB"] for frame in light) / 10),
                )
                # one entry per frame: a feature film is hundreds of MB of objects, freed before dovi_tool runs
                del metadata, light

                if mode == "legacy":
                    generator_config["length"] = 0  # dovi_tool takes the length from the HDR10+ JSON
                else:
                    generator_config["shots"] = shots
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(generator_config, f)

            if profile == "A":
                self.log.warning("The HDR10+ metadata is Profile A, less reliable than Profile B")

            try:
                dovi.generate(
                    config_path,
                    config.directories.temp / "RPU.bin",
                    hdr10plus_json=hdr10plus_path if mode == "legacy" else None,
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
        finally:
            config_path.unlink(missing_ok=True)
            hdr10plus_path.unlink(missing_ok=True)

        log_event(
            "hybrid_convert_hdr10plus",
            level="DEBUG",
            message="Converted HDR10+ metadata to Dolby Vision Profile 8.1",
            context={
                "shots": len(shots),
                "mode": mode,
                "hdr10plus_profile": profile,
                "active_fraction": round(active_fraction, 3),
            },
            success=True,
        )
        self.log.info(f"Dolby Vision generated from HDR10+ metadata ({len(shots)} scenes, {mode})")

    def measure_frames(
        self, input_video: Path, crop: Optional[tuple[int, int, int, int]]
    ) -> tuple[list[tuple[int, float, int, bool]], float]:
        """Decode the whole video through MEASURE_GRAPH.

        Returns its frame_stats and the highest linear frame average (65535 = 10000 nits).
        """
        crop_filter = ""
        if crop:
            left, top, right, bottom = crop
            crop_filter = f"crop=iw-{left + right}:ih-{top + bottom}:{left}:{top},"
        linear_name = "measure_linear.txt"
        linear_path = config.directories.temp / linear_name
        args = [
            str(FFMPEG) if FFMPEG else "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            # one filter thread: after the downscale a frame is tiny, and handing its slices to a thread
            # pool costs more than the filters do. The pass then runs close to the speed of the decoder.
            "-filter_complex_threads",
            "1",
            "-i",
            str(Path(input_video).resolve()),
            "-filter_complex",
            MEASURE_GRAPH.format(crop=crop_filter, linear=linear_name),
            "-map",
            "[pq]",
            "-f",
            "null",
            "-",
            "-map",
            "[linear]",
            "-f",
            "null",
            "-",
        ]
        max_linear = 0.0
        try:
            with console.status("Measuring Dolby Vision brightness from the decoded video...", spinner="dots"):
                # the linear file is named relative to cwd, because a Windows path needs escapes in a filter graph
                with (
                    tempfile.TemporaryFile() as stderr,
                    subprocess.Popen(
                        args,
                        stdout=subprocess.PIPE,
                        stderr=stderr,
                        cwd=config.directories.temp,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    ) as process,
                ):
                    assert process.stdout
                    try:
                        frames = frame_stats(process.stdout)
                    except BaseException:
                        # a cancelled run must not leave FFmpeg decoding the rest of the title
                        process.kill()
                        raise
                    process.wait()
                    stderr.seek(0)
                    error_tail = stderr.read()[-2000:].decode("utf-8", "replace")
            if linear_path.is_file():
                with open(linear_path, encoding="utf-8", errors="replace") as f:
                    max_linear = max((float(line.partition("=")[2]) for line in f if "YAVG=" in line), default=0.0)
        finally:
            linear_path.unlink(missing_ok=True)
        if process.returncode != 0 or not frames:
            log_event(
                "hybrid_measure_dv",
                level="ERROR",
                message="Failed measuring the decoded video",
                context={"returncode": process.returncode, "frames": len(frames), "stderr": error_tail},
            )
            raise ValueError("Failed measuring the decoded video for Dolby Vision")
        return frames, max_linear

    def dropped_leading_frames(self, stream: Path) -> int:
        """How many frames at the head of a raw HEVC file the decoder gives no picture for.

        A file that starts on an open GOP holds frames that need the GOP before it.
        """
        result = subprocess.run(
            [
                str(FFProbe) if FFProbe else "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_packets",
                "-count_frames",
                "-read_intervals",
                "%+#64",
                "-show_entries",
                "stream=nb_read_packets,nb_read_frames",
                "-of",
                "default=nw=1",
                str(stream),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        counts = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        try:
            return max(int(counts["nb_read_packets"]) - int(counts["nb_read_frames"]), 0)
        except (KeyError, ValueError):
            return 0

    def generator_config(self, measured_cll: int, measured_fall: int) -> dict[str, Any]:
        """The dovi_tool generator config without its shots, for config.generate_dv_mode.

        Level 6 comes from the HDR10 track. The measured light levels fill only what the track does not state.
        """
        stream_mdl, stream_min_mdl, stream_cll, stream_fall = self.probe_hdr_metadata()
        max_mdl, min_mdl, max_cll, max_fall = self.sanitize_l6(
            stream_mdl, stream_min_mdl, stream_cll or measured_cll, stream_fall or measured_fall
        )
        generator_config: dict[str, Any] = {
            "cm_version": "V29",
            "level6": {
                "max_display_mastering_luminance": max_mdl,
                "min_display_mastering_luminance": min_mdl,
                "max_content_light_level": max_cll,
                "max_frame_average_light_level": max_fall,
            },
        }
        if config.generate_dv_mode == "v40":
            generator_config["l1_avg_pq_cm_version"] = "V40"
        return generator_config

    def measure_dv(self, crop: Optional[tuple[int, int, int, int]] = None) -> None:
        """Generate a Dolby Vision RPU with L1 measured from the decoded video, one shot per scene cut.

        It measures HDR10.hevc, the file that gets the RPU, so the shots count the same frames. The first
        shot takes the leading frames that the decoder drops.
        """
        mode = generate_dv_mode()
        stream = config.directories.temp / "HDR10.hevc"
        frames, max_linear = self.measure_frames(stream, crop)
        dropped = self.dropped_leading_frames(stream)
        frames = [(*frames[0][:3], False)] * dropped + frames
        shots = measured_shots(frames)
        generator_config = self.generator_config(
            round(pq_to_nits(max(f[2] for f in frames), 65535)), round(max_linear * 10000 / 65535)
        )
        generator_config["shots"] = shots

        config_path = config.directories.temp / "generate.json"
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(generator_config, f)
            dovi.generate(config_path, config.directories.temp / "RPU.bin", label="dovi_tool generate")
        except RuntimeError as e:
            log_event(
                "hybrid_measure_dv",
                level="ERROR",
                message="Failed generating Dolby Vision from the decoded video",
                context={"error": str(e)},
            )
            raise ValueError("Failed generating Dolby Vision from the decoded video")
        finally:
            config_path.unlink(missing_ok=True)

        log_event(
            "hybrid_measure_dv",
            level="DEBUG",
            message="Generated Dolby Vision Profile 8.1 from the decoded video",
            context={
                "frames": len(frames),
                "dropped_leading_frames": dropped,
                "shots": len(shots),
                "mode": mode,
                "level6": generator_config["level6"],
            },
            success=True,
        )
        self.log.info(f"Dolby Vision generated from the decoded video ({len(shots)} scenes, {mode})")
