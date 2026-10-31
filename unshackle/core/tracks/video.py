from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import time
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional, Union

from langcodes import Language

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.tracks.subtitle import Subtitle
from unshackle.core.tracks.track import Track
from unshackle.core.utilities import FPS, get_boxes, log_event
from unshackle.core.utils.subprocess import log_tool_run


class Video(Track):
    sample_tail = 1

    class Codec(str, Enum):
        AVC = "H.264"
        HEVC = "H.265"
        VC1 = "VC-1"
        VP8 = "VP8"
        VP9 = "VP9"
        AV1 = "AV1"
        LCEVC = "LCEVC"

        @property
        def extension(self) -> str:
            return self.value.lower().replace(".", "").replace("-", "")

        @staticmethod
        def from_mime(mime: str) -> Video.Codec:
            mime = mime.lower().strip().split(".")[0]
            if mime in (
                "avc1",
                "avc2",
                "avc3",
                "dva1",
                "dvav",  # Dolby Vision
            ):
                return Video.Codec.AVC
            if mime in (
                "hev1",
                "hev2",
                "hev3",
                "hvc1",
                "hvc2",
                "hvc3",
                "dvh1",
                "dvhe",  # Dolby Vision
                "lhv1",
                "lhe1",  # Layered
            ):
                return Video.Codec.HEVC
            if mime == "vc-1":
                return Video.Codec.VC1
            if mime in ("vp08", "vp8"):
                return Video.Codec.VP8
            if mime in ("vp09", "vp9"):
                return Video.Codec.VP9
            if mime == "av01":
                return Video.Codec.AV1
            if mime == "lvc1":
                return Video.Codec.LCEVC
            raise ValueError(f"The MIME '{mime}' is not a supported Video Codec")

        @staticmethod
        def from_codecs(codecs: str) -> Video.Codec:
            for codec in codecs.lower().split(","):
                codec = codec.strip()
                mime = codec.split(".")[0]
                try:
                    return Video.Codec.from_mime(mime)
                except ValueError:
                    pass
            raise ValueError(f"No MIME types matched any supported Video Codecs in '{codecs}'")

        @staticmethod
        def from_netflix_profile(profile: str) -> Video.Codec:
            profile = profile.lower().strip()
            if profile.startswith(("h264", "playready-h264")):
                return Video.Codec.AVC
            if profile.startswith("hevc"):
                return Video.Codec.HEVC
            if profile.startswith("vp9"):
                return Video.Codec.VP9
            if profile.startswith("av1"):
                return Video.Codec.AV1
            raise ValueError(f"The Content Profile '{profile}' is not a supported Video Codec")

    class Range(str, Enum):
        SDR = "SDR"  # No Dynamic Range
        HLG = "HLG"  # https://en.wikipedia.org/wiki/Hybrid_log%E2%80%93gamma
        HDR10 = "HDR10"  # https://en.wikipedia.org/wiki/HDR10
        HDR10P = "HDR10+"  # https://en.wikipedia.org/wiki/HDR10%2B
        DV = "DV"  # https://en.wikipedia.org/wiki/Dolby_Vision
        HYBRID = "HYBRID"  # Selects both HDR10 and DV tracks for hybrid processing with DoviTool

        @staticmethod
        def from_cicp(primaries: int, transfer: int, matrix: int) -> Video.Range:
            """
            Convert CICP (Coding-Independent Code Points) values to Video Range.

            ITU-T H.273 and ISO/IEC 23091-2 define CICP for signalling video
            colour properties independently of the compression codec. These values are
            used across AVC (H.264), HEVC (H.265), VVC, AV1, and other modern codecs.

            The enum values (Primaries, Transfer, Matrix) match the official specifications:
            - ITU-T H.273: Coding-independent code points for video signal type identification
            - ISO/IEC 23091-2: Information technology - Coding-independent code points - Part 2: Video
            - H.264 Table E-3 (Colour Primaries) and Table E-4 (Transfer Characteristics)
            - H.265 Table E.3 and E.4 (identical to H.264)

            Note: Value 0 = "Reserved" and Value 2 = "Unspecified" per specification.
            While both effectively mean "unknown" in practice, the distinction matters for
            spec compliance. Value 2 was added after user feedback (GitHub issue) and matches
            FFmpeg's AVColorPrimaries/AVColorTransferCharacteristic enums.

            Sources:
            - https://www.itu.int/rec/T-REC-H.273
            - https://www.itu.int/rec/T-REC-H.Sup19-202104-I
            - https://github.com/FFmpeg/FFmpeg/blob/master/libavutil/pixfmt.h
            """

            class Primaries(Enum):
                Reserved = 0
                BT_709 = 1
                Unspecified = 2
                BT_470_M = 4
                BT_601_625 = 5
                BT_601_525 = 6
                SMPTE_240M = 7
                Generic_Film = 8
                BT_2020_and_2100 = 9
                SMPTE_ST_428_1 = 10
                SMPTE_RP_431_2 = 11  # P3DCI
                SMPTE_ST_2113_and_EG_4321 = 12  # P3D65
                EBU_Tech_3213_E = 22

            class Transfer(Enum):
                Reserved = 0
                BT_709 = 1
                Unspecified = 2
                BT_470_M = 4
                BT_601 = 6
                SMPTE_240M = 7
                Linear = 8
                Log_100 = 9
                Log_316 = 10
                IEC_61966_2_4 = 11
                BT_1361 = 12
                IEC_61966_2_1 = 13  # sRGB / sYCC
                BT_2020 = 14
                BT_2100 = 15
                BT_2100_PQ = 16
                SMPTE_ST_428_1 = 17
                BT_2100_HLG = 18

            class Matrix(Enum):
                RGB = 0
                YCbCr_BT_709 = 1
                Unspecified = 2
                YCbCr_FCC_73_682 = 4
                YCbCr_BT_601_625 = 5
                YCbCr_BT_601_525 = 6
                SMPTE_240M = 7
                YCgCo = 8
                YCbCr_BT_2020_and_2100 = 9  # YCbCr BT.2100 shares the same CP
                YCbCr_BT_2020_CL = 10
                YCbCr_SMPTE_ST_2085 = 11
                ICtCp_BT_2100 = 14

            if transfer == 5:
                # While not part of any standard, it is typically used as a PAL variant of Transfer.BT_601=6.
                # i.e. where Transfer 6 would be for BT.601-NTSC and Transfer 5 would be for BT.601-PAL.
                # The codebase is currently agnostic to either, so a manual conversion to 6 is done.
                transfer = 6

            def safe(enum_cls: type[Enum], value: int) -> Any:
                try:
                    return enum_cls(value)
                except ValueError:
                    return enum_cls(2)  # Unspecified for unknown/private-use codes

            primaries = safe(Primaries, primaries)
            transfer = safe(Transfer, transfer)
            matrix = safe(Matrix, matrix)

            # primaries and matrix does not strictly correlate to a range

            if (primaries, transfer, matrix) == (Primaries.Reserved, Transfer.Reserved, Matrix.RGB):
                return Video.Range.SDR
            elif primaries in (Primaries.BT_601_625, Primaries.BT_601_525):
                return Video.Range.SDR
            elif transfer == Transfer.BT_2100_PQ:
                return Video.Range.HDR10
            elif transfer == Transfer.BT_2100_HLG:
                return Video.Range.HLG
            else:
                return Video.Range.SDR

        @staticmethod
        def from_m3u_range_tag(tag: str) -> Optional[Video.Range]:
            tag = (tag or "").upper().replace('"', "").strip()
            if not tag:
                return None
            if tag == "SDR":
                return Video.Range.SDR
            elif tag == "PQ":
                return Video.Range.HDR10  # technically could be any PQ-transfer range
            elif tag == "HLG":
                return Video.Range.HLG
            # for some reason there's no Dolby Vision info tag
            raise ValueError(f"The M3U Range Tag '{tag}' is not a supported Video Range")

    class ScanType(str, Enum):
        PROGRESSIVE = "progressive"
        INTERLACED = "interlaced"

    def __init__(
        self,
        *args: Any,
        codec: Optional[Video.Codec] = None,
        range_: Optional[Video.Range] = None,
        bitrate: Optional[Union[str, int, float]] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: Optional[Union[str, int, float]] = None,
        scan_type: Optional[Video.ScanType] = None,
        closed_captions: Optional[list[dict[str, Any]]] = None,
        dv_compatible_bitstream: bool = False,
        hdr10plus: bool = False,
        hdr10_base: bool = False,
        **kwargs: Any,
    ) -> None:
        """
        Make a new Video track object.

        Parameters:
            codec: A Video.Codec enum representing the video codec.
                If not specified, unshackle uses MediaInfo to get the codec
                after it downloads the track.
            range_: A Video.Range enum representing the video colour range.
                Defaults to SDR if not specified.
            bitrate: A number or float representing the average bandwidth in bytes/s.
                unshackle rounds float values up to the nearest integer.
            width: The horizontal resolution of the video.
            height: The vertical resolution of the video.
            fps: A number, float, or string representing the frames/s of the video.
                Strings may represent numbers, floats, or a fraction (num/den).
                All strings will be cast to either a number or float.
            hdr10plus: The DV track also carries HDR10+ metadata. The HDR10+ probe sets it.
            hdr10_base: The DV track has an HDR10 base layer, as DV profile 8.1 does.

        Note: If codec, bitrate, width, height, or fps is not specified, unshackle can
        skip some checks or assume a value. Give as much information as possible.
        """
        super().__init__(*args, **kwargs)

        if not isinstance(codec, (Video.Codec, type(None))):
            raise TypeError(f"Expected codec to be a {Video.Codec}, not {codec!r}")
        if not isinstance(range_, (Video.Range, type(None))):
            raise TypeError(f"Expected range_ to be a {Video.Range}, not {range_!r}")
        if not isinstance(bitrate, (str, int, float, type(None))):
            raise TypeError(f"Expected bitrate to be a {str}, {int}, or {float}, not {bitrate!r}")
        if not isinstance(width, (int, str, type(None))):
            raise TypeError(f"Expected width to be a {int}, not {width!r}")
        if not isinstance(height, (int, str, type(None))):
            raise TypeError(f"Expected height to be a {int}, not {height!r}")
        if not isinstance(fps, (str, int, float, type(None))):
            raise TypeError(f"Expected fps to be a {str}, {int}, or {float}, not {fps!r}")
        if not isinstance(scan_type, (Video.ScanType, type(None))):
            raise TypeError(f"Expected scan_type to be a {Video.ScanType}, not {scan_type!r}")

        self.codec = codec
        self.range = range_ or Video.Range.SDR

        try:
            self.bitrate = int(math.ceil(float(bitrate))) if bitrate else None
        except (ValueError, TypeError) as e:
            raise ValueError(f"Expected bitrate to be a number or float, {e}")

        try:
            self.width = int(width or 0) or None
        except ValueError as e:
            raise ValueError(f"Expected width to be a number, not {width!r}, {e}")

        try:
            self.height = int(height or 0) or None
        except ValueError as e:
            raise ValueError(f"Expected height to be a number, not {height!r}, {e}")

        try:
            self.fps = (FPS.parse(str(fps)) or None) if fps else None
        except Exception as e:
            raise ValueError("Expected fps to be a number, float, or a string as numerator/denominator form, " + str(e))

        self.scan_type = scan_type
        self.closed_captions: list[dict[str, Any]] = closed_captions or []
        self.needs_duration_fix = False
        self.dv_compatible_bitstream = dv_compatible_bitstream
        self.hdr10plus = hdr10plus
        self.hdr10_base = hdr10_base
        self.hybrid_base_only = False
        self.native_hybrid = False

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "codec": self.codec.name if self.codec else None,
                "range": self.range.name if self.range else None,
                "bitrate": self.bitrate,
                "width": self.width,
                "height": self.height,
                "fps": str(self.fps) if self.fps else None,
                "scan_type": self.scan_type.name if self.scan_type else None,
                "dv_compatible_bitstream": self.dv_compatible_bitstream,
                "hdr10plus": self.hdr10plus,
                "hdr10_base": self.hdr10_base,
            }
        )
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Video:
        kwargs = Track.base_kwargs_from_dict(data)
        return cls(
            **kwargs,
            codec=Video.Codec[data["codec"]] if data.get("codec") else None,
            range_=Video.Range[data["range"]] if data.get("range") else None,
            bitrate=data.get("bitrate"),
            width=data.get("width"),
            height=data.get("height"),
            fps=data.get("fps"),
            scan_type=Video.ScanType[data["scan_type"]] if data.get("scan_type") else None,
            dv_compatible_bitstream=data.get("dv_compatible_bitstream", False),
            hdr10plus=data.get("hdr10plus", False),
            hdr10_base=data.get("hdr10_base", False),
        )

    @staticmethod
    def dv_fourccs(codecs: Optional[str]) -> set[str]:
        """The Dolby Vision fourccs in a codecs list (comma or space separated, ``/brand`` suffixes allowed)."""
        fourccs = {token.split("/")[0].split(".")[0] for token in re.split(r"[\s,]+", (codecs or "").lower())}
        return fourccs & {"dva1", "dvav", "dvhe", "dvh1"}

    @staticmethod
    def has_dv_codec(codecs: Optional[str]) -> bool:
        """Whether a codecs list names a Dolby Vision codec."""
        return bool(Video.dv_fourccs(codecs))

    @staticmethod
    def is_dv_composite(
        codec: Optional[Video.Codec], codecs: Optional[str], supplemental_codecs: Optional[str]
    ) -> bool:
        """Whether an HEVC track names DV only in its supplemental codecs: DV profile 8 on an HDR10, SDR or HLG base."""
        return (
            codec == Video.Codec.HEVC
            and not Video.has_dv_codec(codecs)
            and bool(Video.dv_fourccs(supplemental_codecs) & {"dvh1", "dvhe"})
        )

    @property
    def carries_hdr10plus(self) -> bool:
        """Whether the track carries HDR10+, as its range or, on a DV track, found by the HDR10+ probe."""
        return self.hdr10plus or self.range == Video.Range.HDR10P

    @property
    def finished_hybrid(self) -> bool:
        """Whether the track already carries DV and an HDR10 or HDR10+ base, so ``-r HYBRID`` needs no build."""
        if self.range == Video.Range.DV:
            return self.hdr10plus
        return self.dv_compatible_bitstream and self.range in (Video.Range.HDR10, Video.Range.HDR10P)

    @property
    def range_label(self) -> str:
        """The range that ``-r`` selects the track by, then any second format it carries: HDR10+DV, DV+HDR10, DV+HDR10P."""
        label = self.range.name + ("+HDR10P" if self.hdr10plus else "+HDR10" if self.hdr10_base else "")
        return f"{label}+DV" if self.dv_compatible_bitstream and self.range != Video.Range.DV else label

    def __str__(self) -> str:
        return " | ".join(
            filter(
                None,
                [
                    "VID",
                    "[" + (", ".join(filter(None, [self.codec.value if self.codec else None, self.range_label]))) + "]",
                    str(self.language),
                    ", ".join(
                        filter(
                            None,
                            [
                                " @ ".join(
                                    filter(
                                        None,
                                        [
                                            f"{self.width}x{self.height}" if self.width and self.height else None,
                                            f"{self.bitrate // 1000} kb/s" if self.bitrate else None,
                                        ],
                                    )
                                ),
                                f"{self.fps:.3f} FPS" if self.fps else None,
                            ],
                        )
                    ),
                    ", ".join(self.edition) if self.edition else None,
                    self.note,
                ],
            )
        )

    def change_color_range(self, range_: int) -> None:
        """Change the Video's Colour Range to Limited (0) or Full (1)."""
        if not self.path or not self.path.exists():
            raise ValueError("Cannot change the color range flag on a Video that has not been downloaded.")
        if not self.codec:
            raise ValueError("Cannot change the color range flag on a Video that has no codec specified.")
        if self.codec not in (Video.Codec.AVC, Video.Codec.HEVC):
            raise NotImplementedError(
                "Cannot change the color range flag on this Video as "
                f"it's codec, {self.codec.value}, is not yet supported."
            )

        if not binaries.FFMPEG:
            raise EnvironmentError('FFmpeg executable "ffmpeg" was not found but is required for this call.')

        filter_key = {Video.Codec.AVC: "h264_metadata", Video.Codec.HEVC: "hevc_metadata"}[self.codec]

        original_path = self.path
        output_path = original_path.with_stem(f"{original_path.stem}_{['limited', 'full'][range_]}_range")

        range_start = time.monotonic()
        subprocess.run(
            [
                binaries.FFMPEG,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "panic",
                "-i",
                original_path,
                "-codec",
                "copy",
                "-strict",
                "unofficial",
                "-bsf:v",
                f"{filter_key}=video_full_range_flag={range_}",
                str(output_path),
            ],
            check=True,
        )
        log_tool_run(
            "ffmpeg change range",
            "ffmpeg",
            0,
            duration_ms=round((time.monotonic() - range_start) * 1000, 1),
            codec=str(self.codec),
            full_range=bool(range_),
        )

        self.path = output_path
        original_path.unlink()

    def vui_bsf(self) -> Optional[str]:
        """Return the ``-bsf:v`` value that rewrites SPS VUI colour metadata to match ``self.range``.

        Returns None when a rewrite is not necessary: SDR/DV/HYBRID ranges, non-AVC/HEVC codecs, a
        missing file, or when the bitstream already ships the correct colour metadata. Otherwise
        returns a ``{h264,hevc}_metadata=...`` string suitable for FFmpeg ``-bsf:v``.
        """
        if not self.path or not self.path.exists():
            return None
        if self.codec not in (Video.Codec.AVC, Video.Codec.HEVC):
            return None
        if self.range in (Video.Range.SDR, Video.Range.DV, Video.Range.HYBRID):
            return None

        transfer_name = HDR_TRANSFER.get(self.range)
        if not transfer_name:
            return None

        # Skip the rewrite when the bitstream VUI already matches the target, which avoids a full
        # bitstream pass on services that already ship correct colour metadata
        expected = ("bt2020", transfer_name, "bt2020nc")
        if binaries.FFProbe:
            probe = subprocess.run(
                [
                    binaries.FFProbe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=color_primaries,color_transfer,color_space",
                    "-of",
                    "default=noprint_wrappers=1",
                    str(self.path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            vals = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
            current = (vals.get("color_primaries"), vals.get("color_transfer"), vals.get("color_space"))
            if current == expected:
                return None

        primaries, transfer, matrix = TRANSFER_CICP[transfer_name]
        filter_key = {Video.Codec.AVC: "h264_metadata", Video.Codec.HEVC: "hevc_metadata"}[self.codec]
        return (
            f"{filter_key}=colour_primaries={primaries}"
            f":transfer_characteristics={transfer}"
            f":matrix_coefficients={matrix}"
        )

    def normalize_vui(self) -> bool:
        """Rewrite SPS VUI colour metadata to match ``self.range``.

        Some services ship HDR10/HLG bitstreams with stale BT.709 VUI, which makes
        downstream tools mis-classify the file. The manifest-derived range is the
        source of truth. Skips SDR, DV, and HYBRID. Returns True if unshackle rewrote
        the bitstream.
        """
        bsf = self.vui_bsf()
        if bsf is None:
            return False
        assert self.path is not None  # vui_bsf() returns None when path is unset

        if not binaries.FFMPEG:
            raise EnvironmentError('FFmpeg executable "ffmpeg" was not found but is required for this call.')

        original_path = self.path
        output_path = original_path.with_stem(f"{original_path.stem}_vui")
        try:
            subprocess.run(
                [
                    binaries.FFMPEG,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    # ffmpeg exits 0 after dropping packets the bsf cannot parse
                    "-xerror",
                    "-i",
                    str(original_path),
                    "-codec",
                    "copy",
                    "-strict",
                    "unofficial",
                    "-bsf:v",
                    bsf,
                    str(output_path),
                ],
                check=True,
            )
        except subprocess.CalledProcessError:
            output_path.unlink(missing_ok=True)
            return False

        self.path = output_path
        original_path.unlink()

        log_event(
            "normalize_vui",
            level="DEBUG",
            message=f"Rewrote {self.codec} VUI colour metadata to match {self.range}",
            context={
                "codec": str(self.codec),
                "range": str(self.range),
                "vui": bsf,
                "id": self.id,
            },
        )
        return True

    def vp9_colour_args(self, track_id: int) -> list[str]:
        """Return mkvmerge colour args for an HDR VP9 track.

        mkvmerge drops the MP4 vpcC/SmDm/CoLL colour boxes of VP9, which leaves an HDR10 file tagged as SDR.
        The transfer the file states wins; ``self.range`` applies only when the file states none.
        """
        stream = self._probe_colour()
        transfer = stream.get("color_transfer", "unknown")
        codes = TRANSFER_CICP.get(transfer if transfer != "unknown" else HDR_TRANSFER.get(self.range, ""))
        if not codes:
            return []

        opts: dict[str, object] = dict(
            zip(("--colour-primaries", "--colour-transfer-characteristics", "--colour-matrix-coefficients"), codes)
        )
        for data in stream.get("side_data_list", []):
            # ffprobe leaves out the fields a source does not carry, so a partial entry adds nothing
            try:
                if data.get("side_data_type") == "Mastering display metadata":
                    xy = ("red_x", "red_y", "green_x", "green_y", "blue_x", "blue_y", "white_point_x", "white_point_y")
                    coords = [f"{float(Fraction(data[key])):.5f}" for key in xy]
                    opts.update(
                        {
                            "--chromaticity-coordinates": ",".join(coords[:6]),
                            "--white-colour-coordinates": ",".join(coords[6:]),
                            "--max-luminance": f"{float(Fraction(data['max_luminance'])):.6f}",
                            "--min-luminance": f"{float(Fraction(data['min_luminance'])):.6f}",
                        }
                    )
                elif data.get("side_data_type") == "Content light level metadata" and data.get("max_content"):
                    opts.update(
                        {"--max-content-light": int(data["max_content"]), "--max-frame-light": int(data["max_average"])}
                    )
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
        return [arg for flag, value in opts.items() for arg in (flag, f"{track_id}:{value}")]

    def _probe_colour(self) -> dict[str, Any]:
        """Return the FFprobe colour transfer and side data of the first video track, or {} when it cannot probe."""
        if not binaries.FFProbe or not self.path or not self.path.exists():
            return {}
        probe_start = time.monotonic()
        probe = subprocess.run(
            [
                binaries.FFProbe,
                "-v",
                "error",
                # without it ffprobe walks every moof of a fragmented MP4 to read the header
                "-seekable",
                "0",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=color_transfer:stream_side_data",
                "-of",
                "json",
                str(self.path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        log_tool_run(
            "ffprobe colour", "ffprobe", probe.returncode, duration_ms=round((time.monotonic() - probe_start) * 1000, 1)
        )
        try:
            stream: dict[str, Any] = json.loads(probe.stdout)["streams"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            return {}
        return stream

    def ccextractor(
        self, track_id: Any, out_path: Union[Path, str], language: Language, original: bool = False
    ) -> Optional[Subtitle]:
        """Return a TextTrack object representing CC track extracted by CCExtractor."""
        if not self.path:
            raise ValueError("You must download the track first.")

        if not binaries.CCExtractor:
            raise EnvironmentError("ccextractor executable was not found.")

        out_path = Path(out_path)
        video_path, ccextractor = self.path, binaries.CCExtractor

        def run_ccextractor(fallback: str) -> bool:
            cc_start = time.monotonic()
            try:
                subprocess.run(
                    [ccextractor, "-trim", "-nobom", "-noru", "-ru1", "-o", out_path, video_path],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except subprocess.CalledProcessError as e:
                out_path.unlink(missing_ok=True)
                log_tool_run(
                    "ccextractor",
                    "ccextractor",
                    e.returncode,
                    duration_ms=round((time.monotonic() - cc_start) * 1000, 1),
                )
                if e.returncode < 0:
                    logging.getLogger("Video").warning(
                        f"ccextractor crashed (signal {-e.returncode}) on {video_path.name}; {fallback}"
                    )
                    return out_path.exists()
                if e.returncode != 10:  # 10 = No captions found
                    raise
                return out_path.exists()
            log_tool_run(
                "ccextractor",
                "ccextractor",
                0,
                duration_ms=round((time.monotonic() - cc_start) * 1000, 1),
            )
            return out_path.exists()

        # Try on the original file first (preserves container-level CC data like c608 boxes),
        # then fall back to repacked file (ccextractor can fail on some container formats).
        if not run_ccextractor("retrying on a repackaged copy"):
            self.repackage()
            run_ccextractor("skipping CC extraction")

        if out_path.exists():
            cc_track = Subtitle(
                id_=track_id,
                url="",  # doesn't need to be downloaded
                codec=Subtitle.Codec.SubRip,
                language=language,
                is_original_lang=original,
                cc=True,
            )
            cc_track.path = out_path
            return cc_track

        return None

    def extract_c608(self) -> list[Subtitle]:
        """
        Extract Apple-Style c608 box (CEA-608) subtitle using CCExtractor.

        This is not much more than a wrapper to the track.ccextractor function.
        All this does is examine if a c608 box exists, and only then
        does it call CCExtractor.

        Even though there is a possibility of more than one c608 box, unshackle
        can extract only one. It is also very possible this
        needs to be done before any decryption as the decryption may destroy
        some of the metadata.

        TODO: Need a test file with more than one c608 box, before unshackle can
              extract more than one CEA-608 track.
        """
        if not self.path:
            raise ValueError("You must download the track first.")
        with self.path.open("rb") as f:
            # assuming 20KB is enough to contain the c608 box.
            # ffprobe will fail, so a c608 box check must be done.
            c608_count = len(list(get_boxes(f.read(20000), b"c608")))
        if c608_count > 0:
            # TODO: Figure out the real language, it might be different
            #       CEA-608 boxes doesnt seem to carry language information :(
            # TODO: Figure out if the CC language is original lang or not.
            #       Will need to figure out above first to do so.
            track_id = f"ccextractor-{self.id}"
            cc_lang = self.language
            cc_track = self.ccextractor(
                track_id=track_id,
                out_path=config.directories.temp / config.filenames.subtitle.format(id=track_id, language=cc_lang),
                language=cc_lang,
                original=False,
            )
            if not cc_track:
                return []
            return [cc_track]
        return []

    def remove_eia_cc(self) -> bool:
        """
        Remove EIA-CC data from Bitstream while keeping SEI data.

        This works by removing all NAL Unit's with the Type of 6 from the bistream
        and then re-adding SEI data (effectively a new NAL Unit with only the SEI data).
        unshackle can do this only for bitstreams with x264 encoding information, because of
        the obscurity on the MDAT mp4 box structure. Therefore, we need to use hacky regex.
        """
        if not self.path or not self.path.exists():
            raise ValueError("Cannot clean a Track that has not been downloaded.")

        if not binaries.FFMPEG:
            raise EnvironmentError('FFmpeg executable "ffmpeg" was not found but is required for this call.')

        log = logging.getLogger("x264-clean")
        log.info("Removing EIA-CC from Video Track with FFMPEG")

        with open(self.path, "rb") as f:
            file = f.read(60000)

        x264 = re.search(rb"(.{16})(x264)", file)
        if not x264:
            log.info(" - No x264 encode settings were found, unsupported...")
            return False

        uuid = x264.group(1).hex()
        i = file.index(b"x264")
        encoding_settings = file[i : i + file[i:].index(b"\x00")].replace(b":", rb"\\:").replace(b",", rb"\,").decode()

        original_path = self.path
        cleaned_path = original_path.with_suffix(f".cleaned{original_path.suffix}")
        eia_start = time.monotonic()
        subprocess.run(
            [
                binaries.FFMPEG,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "panic",
                "-i",
                original_path,
                "-map_metadata",
                "-1",
                "-fflags",
                "bitexact",
                "-bsf:v",
                f"filter_units=remove_types=6,h264_metadata=sei_user_data={uuid}+{encoding_settings}",
                "-codec",
                "copy",
                str(cleaned_path),
            ],
            check=True,
        )
        log_tool_run(
            "ffmpeg remove EIA-CC",
            "ffmpeg",
            0,
            duration_ms=round((time.monotonic() - eia_start) * 1000, 1),
        )

        log.info(" + Removed")

        self.path = cleaned_path
        original_path.unlink()

        return True


HDR_TRANSFER = {
    Video.Range.HDR10: "smpte2084",
    Video.Range.HDR10P: "smpte2084",
    Video.Range.HLG: "arib-std-b67",
}
TRANSFER_CICP = {"smpte2084": (9, 16, 9), "arib-std-b67": (9, 18, 9)}

__all__ = ("Video",)
