from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from enum import Enum
from functools import partial
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union

import pycaption
import pysubs2
import requests
from construct import Container
from pycaption import Caption, CaptionList, CaptionNode, WebVTTReader
from pycaption.geometry import Layout
from pymp4.parser import MP4
from subby import CommonIssuesFixer, SAMIConverter, SDHStripper, WebVTTConverter
from subtitle_filter import Subtitles

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.tracks.track import Track
from unshackle.core.utilities import try_ensure_utf8
from unshackle.core.utils.webvtt import merge_segmented_webvtt


class Subtitle(Track):
    class Codec(str, Enum):
        SubRip = "SRT"  # https://wikipedia.org/wiki/SubRip
        SubStationAlpha = "SSA"  # https://wikipedia.org/wiki/SubStation_Alpha
        SubStationAlphav4 = "ASS"  # https://wikipedia.org/wiki/SubStation_Alpha#Advanced_SubStation_Alpha=
        TimedTextMarkupLang = "TTML"  # https://wikipedia.org/wiki/Timed_Text_Markup_Language
        WebVTT = "VTT"  # https://wikipedia.org/wiki/WebVTT
        SAMI = "SMI"  # https://wikipedia.org/wiki/SAMI
        MicroDVD = "SUB"  # https://wikipedia.org/wiki/MicroDVD
        MPL2 = "MPL2"  # MPL2 subtitle format
        TMP = "TMP"  # TMP subtitle format
        # MPEG-DASH box-encapsulated subtitle formats
        fTTML = "STPP"  # https://www.w3.org/TR/2018/REC-ttml-imsc1.0.1-20180424
        fVTT = "WVTT"  # https://www.w3.org/TR/webvtt1

        @property
        def extension(self) -> str:
            return self.value.lower()

        @staticmethod
        def from_mime(mime: str) -> Subtitle.Codec:
            mime = mime.lower().strip().split(".")[0]
            if mime == "srt":
                return Subtitle.Codec.SubRip
            elif mime == "ssa":
                return Subtitle.Codec.SubStationAlpha
            elif mime == "ass":
                return Subtitle.Codec.SubStationAlphav4
            elif mime == "ttml":
                return Subtitle.Codec.TimedTextMarkupLang
            elif mime == "vtt":
                return Subtitle.Codec.WebVTT
            elif mime in ("smi", "sami"):
                return Subtitle.Codec.SAMI
            elif mime in ("sub", "microdvd"):
                return Subtitle.Codec.MicroDVD
            elif mime == "mpl2":
                return Subtitle.Codec.MPL2
            elif mime == "tmp":
                return Subtitle.Codec.TMP
            elif mime == "stpp":
                return Subtitle.Codec.fTTML
            elif mime == "wvtt":
                return Subtitle.Codec.fVTT
            raise ValueError(f"The MIME '{mime}' is not a supported Subtitle Codec")

        @staticmethod
        def from_codecs(codecs: str) -> Subtitle.Codec:
            for codec in codecs.lower().split(","):
                mime = codec.strip().split(".")[0]
                try:
                    return Subtitle.Codec.from_mime(mime)
                except ValueError:
                    pass
            raise ValueError(f"No MIME types matched any supported Subtitle Codecs in '{codecs}'")

        @staticmethod
        def from_netflix_profile(profile: str) -> Subtitle.Codec:
            profile = profile.lower().strip()
            if profile.startswith("webvtt"):
                return Subtitle.Codec.WebVTT
            if profile.startswith("dfxp"):
                return Subtitle.Codec.TimedTextMarkupLang
            raise ValueError(f"The Content Profile '{profile}' is not a supported Subtitle Codec")

    def __init__(
        self,
        *args: Any,
        codec: Optional[Subtitle.Codec] = None,
        cc: bool = False,
        sdh: bool = False,
        forced: bool = False,
        **kwargs: Any,
    ):
        """
        Create a new Subtitle track object.

        Parameters:
            codec: A Subtitle.Codec enum representing the subtitle format.
                If not specified, MediaInfo will be used to retrieve the format
                once the track has been downloaded.
            cc: Closed Caption.
                - Intended as if you couldn't hear the audio at all.
                - Can have Sound as well as Dialogue, but doesn't have to.
                - Original source would be from an EIA-CC encoded stream. Typically all
                  upper-case characters.
                Indicators of it being CC without knowing original source:
                  - Extracted with CCExtractor, or
                  - >>> (or similar) being used at the start of some or all lines, or
                  - All text is uppercase or at least the majority, or
                  - Subtitles are Scrolling-text style (one line appears, oldest line
                    then disappears).
                Just because you downloaded it as a SRT or VTT or such, doesn't mean it
                 isn't from an EIA-CC stream. And I wouldn't take the streaming services
                 (CC) as gospel either as they tend to get it wrong too.
            sdh: Deaf or Hard-of-Hearing. Also known as HOH in the UK (EU?).
                 - Intended as if you couldn't hear the audio at all.
                 - MUST have Sound as well as Dialogue to be considered SDH.
                 - It has no "syntax" or "format" but is not transmitted using archaic
                   forms like EIA-CC streams, would be intended for transmission via
                   SubRip (SRT), WebVTT (VTT), TTML, etc.
                 If you can see important audio/sound transcriptions and not just dialogue
                  and it doesn't have the indicators of CC, then it's most likely SDH.
                 If it doesn't have important audio/sounds transcriptions it might just be
                  regular subtitling (you wouldn't mark as CC or SDH). This would be the
                  case for most translation subtitles. Like Anime for example.
            forced: Typically used if there's important information at some point in time
                     like watching Dubbed content and an important Sign or Letter is shown
                     or someone talking in a different language.
                    Forced tracks are recommended by the Matroska Spec to be played if
                     the player's current playback audio language matches a subtitle
                     marked as "forced".
                    However, that doesn't mean every player works like this but there is
                     no other way to reliably work with Forced subtitles where multiple
                     forced subtitles may be in the output file. Just know what to expect
                     with "forced" subtitles.

        Note: If codec is not specified some checks may be skipped or assume a value.
        Specifying as much information as possible is highly recommended.

        Information on Subtitle Types:
            https://bit.ly/2Oe4fLC (3PlayMedia Blog on SUB vs CC vs SDH).
            However, I wouldn't pay much attention to the claims about SDH needing to
            be in the original source language. It's logically not true.

            CC == Closed Captions. Source: Basically every site.
            SDH = Subtitles for the Deaf or Hard-of-Hearing. Source: Basically every site.
            HOH = Exact same as SDH. Is a term used in the UK. Source: https://bit.ly/2PGJatz (ICO UK)

            More in-depth information, examples, and stuff to look for can be found in the Parameter
            explanation list above.
        """
        super().__init__(*args, **kwargs)

        if not isinstance(codec, (Subtitle.Codec, type(None))):
            raise TypeError(f"Expected codec to be a {Subtitle.Codec}, not {codec!r}")
        if not isinstance(cc, (bool, int)) or (isinstance(cc, int) and cc not in (0, 1)):
            raise TypeError(f"Expected cc to be a {bool} or bool-like {int}, not {cc!r}")
        if not isinstance(sdh, (bool, int)) or (isinstance(sdh, int) and sdh not in (0, 1)):
            raise TypeError(f"Expected sdh to be a {bool} or bool-like {int}, not {sdh!r}")
        if not isinstance(forced, (bool, int)) or (isinstance(forced, int) and forced not in (0, 1)):
            raise TypeError(f"Expected forced to be a {bool} or bool-like {int}, not {forced!r}")

        self.codec = codec

        self.cc = bool(cc)
        self.sdh = bool(sdh)
        self.forced = bool(forced)

        if self.cc and self.sdh:
            raise ValueError("A text track cannot be both CC and SDH.")

        if self.forced and (self.cc or self.sdh):
            raise ValueError("A text track cannot be CC/SDH as well as Forced.")

        # TODO: Migrate to new event observer system
        # Called after Track has been converted to another format
        self.OnConverted: Optional[Callable[[Subtitle.Codec], None]] = None

    def __str__(self) -> str:
        return " | ".join(
            filter(
                bool,
                ["SUB", f"[{self.codec.value}]" if self.codec else None, str(self.language), self.get_track_name()],
            )
        )

    def get_track_name(self) -> Optional[str]:
        """Return the base Track Name."""
        track_name = super().get_track_name() or ""
        flag = self.cc and "CC" or self.sdh and "SDH" or self.forced and "Forced"
        if flag:
            if track_name:
                flag = f" ({flag})"
            track_name += flag
        return track_name or None

    def download(
        self,
        session: requests.Session,
        prepare_drm: partial,
        max_workers: Optional[int] = None,
        progress: Optional[partial] = None,
        *,
        cdm: Optional[object] = None,
    ):
        super().download(session, prepare_drm, max_workers, progress, cdm=cdm)
        if not self.path:
            return

        if self.codec == Subtitle.Codec.fTTML:
            self.convert(Subtitle.Codec.TimedTextMarkupLang)
        elif self.codec == Subtitle.Codec.fVTT:
            self.convert(Subtitle.Codec.WebVTT)
        elif self.codec == Subtitle.Codec.WebVTT:
            text = self.path.read_text("utf8")
            if self.descriptor == Track.Descriptor.DASH:
                if len(self.data["dash"]["segment_durations"]) > 1:
                    text = merge_segmented_webvtt(
                        text,
                        segment_durations=self.data["dash"]["segment_durations"],
                        timescale=self.data["dash"]["timescale"],
                    )
            elif self.descriptor == Track.Descriptor.HLS:
                if len(self.data["hls"]["segment_durations"]) > 1:
                    text = merge_segmented_webvtt(
                        text,
                        segment_durations=self.data["hls"]["segment_durations"],
                        timescale=1,  # ?
                    )

            # Sanitize WebVTT timestamps before parsing
            text = Subtitle.sanitize_webvtt_timestamps(text)
            preserve_formatting = config.subtitle.get("preserve_formatting", True)

            if preserve_formatting:
                self.path.write_text(text, encoding="utf8")
            else:
                try:
                    caption_set = pycaption.WebVTTReader().read(text)
                    Subtitle.merge_same_cues(caption_set)
                    Subtitle.filter_unwanted_cues(caption_set)
                    subtitle_text = pycaption.WebVTTWriter().write(caption_set)
                    self.path.write_text(subtitle_text, encoding="utf8")
                except pycaption.exceptions.CaptionReadSyntaxError:
                    # If first attempt fails, try more aggressive sanitization
                    text = Subtitle.sanitize_webvtt(text)
                    try:
                        caption_set = pycaption.WebVTTReader().read(text)
                        Subtitle.merge_same_cues(caption_set)
                        Subtitle.filter_unwanted_cues(caption_set)
                        subtitle_text = pycaption.WebVTTWriter().write(caption_set)
                        self.path.write_text(subtitle_text, encoding="utf8")
                    except Exception:
                        # Keep the sanitized version even if parsing failed
                        self.path.write_text(text, encoding="utf8")

    @staticmethod
    def sanitize_webvtt_timestamps(text: str) -> str:
        """
        Fix invalid timestamps in WebVTT files, particularly negative timestamps.

        Parameters:
            text: The WebVTT content as string

        Returns:
            Sanitized WebVTT content
        """
        # Replace negative timestamps with 00:00:00.000
        return re.sub(r"(-\d+:\d+:\d+\.\d+)", "00:00:00.000", text)

    @staticmethod
    def sanitize_webvtt(text: str) -> str:
        """
        More thorough sanitization of WebVTT files to handle multiple potential issues.

        Parameters:
            text: The WebVTT content as string

        Returns:
            Sanitized WebVTT content
        """
        # Make sure we have a proper WEBVTT header
        if not text.strip().startswith("WEBVTT"):
            text = "WEBVTT\n\n" + text

        lines = text.split("\n")
        sanitized_lines = []
        timestamp_pattern = re.compile(r"^((?:\d+:)?\d+:\d+\.\d+)\s+-->\s+((?:\d+:)?\d+:\d+\.\d+)")

        # Skip invalid headers - keep only WEBVTT
        header_done = False
        for line in lines:
            if not header_done:
                if line.startswith("WEBVTT"):
                    sanitized_lines.append("WEBVTT")
                    header_done = True
                continue

            # Replace negative timestamps
            if "-" in line and "-->" in line:
                line = re.sub(r"(-\d+:\d+:\d+\.\d+)", "00:00:00.000", line)

            # Validate timestamp format
            match = timestamp_pattern.match(line)
            if match:
                start_time = match.group(1)
                end_time = match.group(2)

                # Ensure proper format with hours if missing
                if start_time.count(":") == 1:
                    start_time = f"00:{start_time}"
                if end_time.count(":") == 1:
                    end_time = f"00:{end_time}"

                line = f"{start_time} --> {end_time}"

            sanitized_lines.append(line)

        return "\n".join(sanitized_lines)

    def convert_with_subby(self, codec: Subtitle.Codec) -> Path:
        """
        Convert subtitle using subby library for better format support and processing.

        This method leverages subby's advanced subtitle processing capabilities
        including better WebVTT handling, SDH stripping, and common issue fixing.
        """

        if not self.path or not self.path.exists():
            raise ValueError("You must download the subtitle track first.")

        if self.codec == codec:
            return self.path

        output_path = self.path.with_suffix(f".{codec.value.lower()}")
        original_path = self.path

        try:
            # Convert to SRT using subby first
            srt_subtitles = None

            if self.codec == Subtitle.Codec.WebVTT:
                converter = WebVTTConverter()
                srt_subtitles = converter.from_file(str(self.path))
            elif self.codec == Subtitle.Codec.SAMI:
                converter = SAMIConverter()
                srt_subtitles = converter.from_file(str(self.path))

            if srt_subtitles is not None:
                # Apply common fixes
                fixer = CommonIssuesFixer()
                fixed_srt, _ = fixer.from_srt(srt_subtitles)

                # If target is SRT, we're done
                if codec == Subtitle.Codec.SubRip:
                    output_path.write_text(str(fixed_srt), encoding="utf8")
                else:
                    # Convert from SRT to target format using existing pycaption logic
                    temp_srt_path = self.path.with_suffix(".temp.srt")
                    temp_srt_path.write_text(str(fixed_srt), encoding="utf8")

                    # Parse the SRT and convert to target format
                    caption_set = self.parse(temp_srt_path.read_bytes(), Subtitle.Codec.SubRip)
                    self.merge_same_cues(caption_set)

                    writer = {
                        Subtitle.Codec.TimedTextMarkupLang: pycaption.DFXPWriter,
                        Subtitle.Codec.WebVTT: pycaption.WebVTTWriter,
                    }.get(codec)

                    if writer:
                        subtitle_text = writer().write(caption_set)
                        output_path.write_text(subtitle_text, encoding="utf8")
                    else:
                        # Fall back to existing conversion method
                        temp_srt_path.unlink()
                        return self._convert_standard(codec)

                    temp_srt_path.unlink()

                if original_path.exists() and original_path != output_path:
                    original_path.unlink()

                self.path = output_path
                self.codec = codec

                if callable(self.OnConverted):
                    self.OnConverted(codec)

                return output_path
            else:
                # Fall back to existing conversion method
                return self._convert_standard(codec)

        except Exception:
            # Fall back to existing conversion method on any error
            return self._convert_standard(codec)

    def convert_with_pysubs2(self, codec: Subtitle.Codec) -> Path:
        """
        Convert subtitle using pysubs2 library for broad format support.

        pysubs2 is a pure-Python library supporting SubRip (SRT), SubStation Alpha
        (SSA/ASS), WebVTT, TTML, SAMI, MicroDVD, MPL2, and TMP formats.
        """
        if not self.path or not self.path.exists():
            raise ValueError("You must download the subtitle track first.")

        if self.codec == codec:
            return self.path

        output_path = self.path.with_suffix(f".{codec.value.lower()}")
        original_path = self.path

        codec_to_pysubs2_format = {
            Subtitle.Codec.SubRip: "srt",
            Subtitle.Codec.SubStationAlpha: "ssa",
            Subtitle.Codec.SubStationAlphav4: "ass",
            Subtitle.Codec.WebVTT: "vtt",
            Subtitle.Codec.TimedTextMarkupLang: "ttml",
            Subtitle.Codec.SAMI: "sami",
            Subtitle.Codec.MicroDVD: "microdvd",
            Subtitle.Codec.MPL2: "mpl2",
            Subtitle.Codec.TMP: "tmp",
        }

        pysubs2_output_format = codec_to_pysubs2_format.get(codec)
        if pysubs2_output_format is None:
            return self._convert_standard(codec)

        try:
            subs = pysubs2.load(str(self.path), encoding="utf-8")

            subs.save(str(output_path), format_=pysubs2_output_format, encoding="utf-8")

            if original_path.exists() and original_path != output_path:
                original_path.unlink()

            self.path = output_path
            self.codec = codec

            if callable(self.OnConverted):
                self.OnConverted(codec)

            return output_path

        except Exception:
            return self._convert_standard(codec)

    def convert(self, codec: Subtitle.Codec) -> Path:
        """
        Convert this Subtitle to another Format.

        The conversion method is determined by the 'conversion_method' setting in config:
        - 'auto' (default): Uses subby for WebVTT/SAMI, standard for others
        - 'subby': Always uses subby with CommonIssuesFixer
        - 'subtitleedit': Uses SubtitleEdit when available, falls back to pycaption
        - 'pycaption': Uses only pycaption library
        - 'pysubs2': Uses pysubs2 library
        """
        # Check configuration for conversion method
        conversion_method = config.subtitle.get("conversion_method", "auto")

        if conversion_method == "subby":
            return self.convert_with_subby(codec)
        elif conversion_method == "subtitleedit":
            return self._convert_standard(codec)
        elif conversion_method == "pycaption":
            return self._convert_pycaption_only(codec)
        elif conversion_method == "pysubs2":
            return self.convert_with_pysubs2(codec)
        elif conversion_method == "auto":
            if self.codec in (Subtitle.Codec.WebVTT, Subtitle.Codec.SAMI):
                return self.convert_with_subby(codec)
            else:
                return self._convert_standard(codec)
        else:
            return self._convert_standard(codec)

    def _convert_pycaption_only(self, codec: Subtitle.Codec) -> Path:
        """
        Convert subtitle using only pycaption library (no SubtitleEdit, no subby).

        This is the original conversion method that only uses pycaption.
        """
        if not self.path or not self.path.exists():
            raise ValueError("You must download the subtitle track first.")

        if self.codec == codec:
            return self.path

        output_path = self.path.with_suffix(f".{codec.value.lower()}")
        original_path = self.path

        # Use only pycaption for conversion
        writer = {
            Subtitle.Codec.SubRip: pycaption.SRTWriter,
            Subtitle.Codec.TimedTextMarkupLang: pycaption.DFXPWriter,
            Subtitle.Codec.WebVTT: pycaption.WebVTTWriter,
        }.get(codec)

        if writer is None:
            raise NotImplementedError(f"Cannot convert {self.codec.name} to {codec.name} using pycaption only.")

        caption_set = self.parse(self.path.read_bytes(), self.codec)
        Subtitle.merge_same_cues(caption_set)
        if codec == Subtitle.Codec.WebVTT:
            Subtitle.filter_unwanted_cues(caption_set)
        subtitle_text = writer().write(caption_set)

        output_path.write_text(subtitle_text, encoding="utf8")

        if original_path.exists() and original_path != output_path:
            original_path.unlink()

        self.path = output_path
        self.codec = codec

        if callable(self.OnConverted):
            self.OnConverted(codec)

        return output_path

    def _convert_standard(self, codec: Subtitle.Codec) -> Path:
        """
        Convert this Subtitle to another Format.

        The file path location of the Subtitle data will be kept at the same
        location but the file extension will be changed appropriately.

        Supported formats:
        - SubRip - SubtitleEdit or pycaption.SRTWriter
        - TimedTextMarkupLang - SubtitleEdit or pycaption.DFXPWriter
        - WebVTT - SubtitleEdit or pycaption.WebVTTWriter
        - SubStationAlphav4 - SubtitleEdit
        - SAMI - subby.SAMIConverter (when available)
        - fTTML* - custom code using some pycaption functions
        - fVTT* - custom code using some pycaption functions
        *: Can read from format, but cannot convert to format

        Note: It currently prioritizes using SubtitleEdit over PyCaption as
        I have personally noticed more oddities with PyCaption parsing over
        SubtitleEdit. Especially when working with TTML/DFXP where it would
        often have timecodes and stuff mixed in/duplicated.

        Returns the new file path of the Subtitle.
        """
        if not self.path or not self.path.exists():
            raise ValueError("You must download the subtitle track first.")

        if self.codec == codec:
            return self.path

        output_path = self.path.with_suffix(f".{codec.value.lower()}")
        original_path = self.path

        if binaries.SubtitleEdit and self.codec not in (Subtitle.Codec.fTTML, Subtitle.Codec.fVTT):
            sub_edit_format = {
                Subtitle.Codec.SubStationAlphav4: "AdvancedSubStationAlpha",
                Subtitle.Codec.TimedTextMarkupLang: "TimedText1.0",
            }.get(codec, codec.name)
            sub_edit_args = [
                binaries.SubtitleEdit,
                "/Convert",
                self.path,
                sub_edit_format,
                f"/outputfilename:{output_path.name}",
                "/encoding:utf8",
            ]
            if codec == Subtitle.Codec.SubRip:
                sub_edit_args.append("/ConvertColorsToDialog")
            subprocess.run(sub_edit_args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            writer = {
                # pycaption generally only supports these subtitle formats
                Subtitle.Codec.SubRip: pycaption.SRTWriter,
                Subtitle.Codec.TimedTextMarkupLang: pycaption.DFXPWriter,
                Subtitle.Codec.WebVTT: pycaption.WebVTTWriter,
            }.get(codec)
            if writer is None:
                raise NotImplementedError(f"Cannot yet convert {self.codec.name} to {codec.name}.")

            caption_set = self.parse(self.path.read_bytes(), self.codec)
            Subtitle.merge_same_cues(caption_set)
            if codec == Subtitle.Codec.WebVTT:
                Subtitle.filter_unwanted_cues(caption_set)
            subtitle_text = writer().write(caption_set)

            output_path.write_text(subtitle_text, encoding="utf8")

        if original_path.exists() and original_path != output_path:
            original_path.unlink()

        self.path = output_path
        self.codec = codec

        if callable(self.OnConverted):
            self.OnConverted(codec)

        return output_path

    @staticmethod
    def parse(data: bytes, codec: Subtitle.Codec) -> pycaption.CaptionSet:
        if not isinstance(data, bytes):
            raise ValueError(f"Subtitle data must be parsed as bytes data, not {type(data).__name__}")

        try:
            if codec == Subtitle.Codec.SubRip:
                text = try_ensure_utf8(data).decode("utf8")
                caption_set = pycaption.SRTReader().read(text)
            elif codec == Subtitle.Codec.fTTML:
                caption_lists: dict[str, pycaption.CaptionList] = defaultdict(pycaption.CaptionList)
                for segment in (
                    Subtitle.parse(box.data, Subtitle.Codec.TimedTextMarkupLang)
                    for box in MP4.parse_stream(BytesIO(data))
                    if box.type == b"mdat"
                ):
                    for lang in segment.get_languages():
                        caption_lists[lang].extend(segment.get_captions(lang))
                caption_set: pycaption.CaptionSet = pycaption.CaptionSet(caption_lists)
            elif codec == Subtitle.Codec.TimedTextMarkupLang:
                text = try_ensure_utf8(data).decode("utf8")
                text = text.replace("tt:", "")
                # negative size values aren't allowed in TTML/DFXP spec, replace with 0
                text = re.sub(r'-(\d+(?:\.\d+)?)(px|em|%|c|pt)', r'0\2', text)
                caption_set = pycaption.DFXPReader().read(text)
            elif codec == Subtitle.Codec.fVTT:
                caption_lists: dict[str, pycaption.CaptionList] = defaultdict(pycaption.CaptionList)
                caption_list, language = Subtitle.merge_segmented_wvtt(data)
                caption_lists[language] = caption_list
                caption_set: pycaption.CaptionSet = pycaption.CaptionSet(caption_lists)
            elif codec == Subtitle.Codec.WebVTT:
                text = try_ensure_utf8(data).decode("utf8")
                text = Subtitle.sanitize_broken_webvtt(text)
                text = Subtitle.space_webvtt_headers(text)
                caption_set = pycaption.WebVTTReader().read(text)
            elif codec == Subtitle.Codec.SAMI:
                # Use subby for SAMI parsing
                converter = SAMIConverter()
                srt_subtitles = converter.from_bytes(data)
                # Convert SRT back to CaptionSet for compatibility
                srt_text = str(srt_subtitles).encode("utf8")
                caption_set = Subtitle.parse(srt_text, Subtitle.Codec.SubRip)
            else:
                raise ValueError(f'Unknown Subtitle format "{codec}"...')
        except pycaption.exceptions.CaptionReadSyntaxError as e:
            raise SyntaxError(f'A syntax error has occurred when reading the "{codec}" subtitle: {e}')
        except pycaption.exceptions.CaptionReadNoCaptions:
            return pycaption.CaptionSet({"en": []})

        # remove empty caption lists or some code breaks, especially if it's the first list
        for language in caption_set.get_languages():
            if not caption_set.get_captions(language):
                # noinspection PyProtectedMember
                del caption_set._captions[language]

        return caption_set

    @staticmethod
    def sanitize_broken_webvtt(text: str) -> str:
        """
        Remove or fix corrupted WebVTT lines, particularly those with invalid timestamps.

        Parameters:
            text: The WebVTT content as string

        Returns:
            Sanitized WebVTT content with corrupted lines removed
        """
        lines = text.splitlines()
        sanitized_lines = []

        i = 0
        while i < len(lines):
            # Skip empty lines
            if not lines[i].strip():
                sanitized_lines.append(lines[i])
                i += 1
                continue

            # Check for timestamp lines
            if "-->" in lines[i]:
                # Validate timestamp format
                timestamp_parts = lines[i].split("-->")
                if len(timestamp_parts) != 2 or not timestamp_parts[1].strip() or timestamp_parts[1].strip() == "0":
                    # Skip this timestamp and its content until next timestamp or end
                    j = i + 1
                    while j < len(lines) and "-->" not in lines[j] and lines[j].strip():
                        j += 1
                    i = j
                    continue

                # Add valid timestamp line
                sanitized_lines.append(lines[i])
            else:
                # Add non-timestamp line
                sanitized_lines.append(lines[i])

            i += 1

        return "\n".join(sanitized_lines)

    @staticmethod
    def space_webvtt_headers(data: Union[str, bytes]):
        """
        Space out the WEBVTT Headers from Captions.

        Segmented VTT when merged may have the WEBVTT headers part of the next caption
        as they were not separated far enough from the previous caption and ended up
        being considered as caption text rather than the header for the next segment.
        """
        if isinstance(data, bytes):
            data = try_ensure_utf8(data).decode("utf8")
        elif not isinstance(data, str):
            raise ValueError(f"Expecting data to be a str, not {data!r}")

        text = (
            data.replace("WEBVTT", "\n\nWEBVTT").replace("\r", "").replace("\n\n\n", "\n \n\n").replace("\n\n<", "\n<")
        )

        return text

    @staticmethod
    def merge_same_cues(caption_set: pycaption.CaptionSet):
        """Merge captions with the same timecodes and text as one in-place."""
        for lang in caption_set.get_languages():
            captions = caption_set.get_captions(lang)
            last_caption = None
            concurrent_captions = pycaption.CaptionList()
            merged_captions = pycaption.CaptionList()
            for caption in captions:
                if last_caption:
                    if (caption.start, caption.end) == (last_caption.start, last_caption.end):
                        if caption.get_text() != last_caption.get_text():
                            concurrent_captions.append(caption)
                        last_caption = caption
                        continue
                    else:
                        merged_captions.append(pycaption.base.merge(concurrent_captions))
                concurrent_captions = [caption]
                last_caption = caption

            if concurrent_captions:
                merged_captions.append(pycaption.base.merge(concurrent_captions))
            if merged_captions:
                caption_set.set_captions(lang, merged_captions)

    @staticmethod
    def filter_unwanted_cues(caption_set: pycaption.CaptionSet):
        """
        Filter out subtitle cues containing only &nbsp; or whitespace.
        """
        for lang in caption_set.get_languages():
            captions = caption_set.get_captions(lang)
            filtered_captions = pycaption.CaptionList()

            for caption in captions:
                text = caption.get_text().strip()
                if not text or text == "&nbsp;" or all(c in " \t\n\r\xa0" for c in text.replace("&nbsp;", "\xa0")):
                    continue

                filtered_captions.append(caption)

            caption_set.set_captions(lang, filtered_captions)

    @staticmethod
    def merge_segmented_wvtt(data: bytes, period_start: float = 0.0) -> tuple[CaptionList, Optional[str]]:
        """
        Convert Segmented DASH WebVTT cues into a pycaption Caption List.
        Also returns an ISO 639-2 alpha-3 language code if available.

        Code ported originally by xhlove to Python from shaka-player.
        Has since been improved upon by rlaphoenix using pymp4 and
        pycaption functions.
        """
        captions = CaptionList()

        # init:
        saw_wvtt_box = False
        timescale = None
        language = None

        # media:
        # > tfhd
        default_duration = None
        # > tfdt
        saw_tfdt_box = False
        base_time = 0
        # > trun
        saw_trun_box = False
        samples = []

        def flatten_boxes(box: Container) -> Iterable[Container]:
            for child in box:
                if hasattr(child, "children"):
                    yield from flatten_boxes(child.children)
                    del child["children"]
                if hasattr(child, "entries"):
                    yield from flatten_boxes(child.entries)
                    del child["entries"]
                # some boxes (mainly within 'entries') uses format not type
                child["type"] = child.get("type") or child.get("format")
                yield child

        for box in flatten_boxes(MP4.parse_stream(BytesIO(data))):
            # init
            if box.type == b"mdhd":
                timescale = box.timescale
                language = box.language

            if box.type == b"wvtt":
                saw_wvtt_box = True

            # media
            if box.type == b"styp":
                # essentially the start of each segment
                # media var resets
                # > tfhd
                default_duration = None
                # > tfdt
                saw_tfdt_box = False
                base_time = 0
                # > trun
                saw_trun_box = False
                samples = []

            if box.type == b"tfhd":
                if box.flags.default_sample_duration_present:
                    default_duration = box.default_sample_duration

            if box.type == b"tfdt":
                saw_tfdt_box = True
                base_time = box.baseMediaDecodeTime

            if box.type == b"trun":
                saw_trun_box = True
                samples = box.sample_info

            if box.type == b"mdat":
                if not timescale:
                    raise ValueError("Timescale was not found in the Segmented WebVTT.")
                if not saw_wvtt_box:
                    raise ValueError("The WVTT box was not found in the Segmented WebVTT.")
                if not saw_tfdt_box:
                    raise ValueError("The TFDT box was not found in the Segmented WebVTT.")
                if not saw_trun_box:
                    raise ValueError("The TRUN box was not found in the Segmented WebVTT.")

                vttc_boxes = MP4.parse_stream(BytesIO(box.data))
                current_time = base_time + period_start

                for sample, vttc_box in zip(samples, vttc_boxes):
                    duration = sample.sample_duration or default_duration
                    if sample.sample_composition_time_offsets:
                        current_time += sample.sample_composition_time_offsets

                    start_time = current_time
                    end_time = current_time + (duration or 0)
                    current_time = end_time

                    if vttc_box.type == b"vtte":
                        # vtte is a vttc that's empty, skip
                        continue

                    layout: Optional[Layout] = None
                    nodes: list[CaptionNode] = []

                    for cue_box in vttc_box.children:
                        if cue_box.type == b"vsid":
                            # this is a V(?) Source ID box, we don't care
                            continue
                        if cue_box.type == b"sttg":
                            layout = Layout(webvtt_positioning=cue_box.settings)
                        elif cue_box.type == b"payl":
                            nodes.extend(
                                [
                                    node
                                    for line in cue_box.cue_text.split("\n")
                                    for node in [
                                        CaptionNode.create_text(WebVTTReader()._decode(line)),
                                        CaptionNode.create_break(),
                                    ]
                                ]
                            )
                            nodes.pop()

                    if nodes:
                        caption = Caption(
                            start=start_time * timescale,  # as microseconds
                            end=end_time * timescale,
                            nodes=nodes,
                            layout_info=layout,
                        )
                        p_caption = captions[-1] if captions else None
                        if p_caption and caption.start == p_caption.end and str(caption.nodes) == str(p_caption.nodes):
                            # it's a duplicate, but lets take its end time
                            p_caption.end = caption.end
                            continue
                        captions.append(caption)

        return captions, language

    def strip_hearing_impaired(self) -> None:
        """
        Strip captions for hearing impaired (SDH).

        The SDH stripping method is determined by the 'sdh_method' setting in config:
        - 'auto' (default): Tries subby first, then SubtitleEdit, then filter-subs
        - 'subby': Uses subby's SDHStripper
        - 'subtitleedit': Uses SubtitleEdit when available
        - 'filter-subs': Uses subtitle-filter library
        """
        if not self.path or not self.path.exists():
            raise ValueError("You must download the subtitle track first.")

        # Check configuration for SDH stripping method
        sdh_method = config.subtitle.get("sdh_method", "auto")

        if sdh_method == "subby" and self.codec == Subtitle.Codec.SubRip:
            # Use subby's SDHStripper directly on the file
            stripper = SDHStripper()
            stripped_srt, _ = stripper.from_file(str(self.path))
            self.path.write_text(str(stripped_srt), encoding="utf8")
            return
        elif sdh_method == "subtitleedit" and binaries.SubtitleEdit:
            # Force use of SubtitleEdit
            pass  # Continue to SubtitleEdit section below
        elif sdh_method == "filter-subs":
            # Force use of filter-subs
            sub = Subtitles(self.path)
            try:
                sub.filter(rm_fonts=True, rm_ast=True, rm_music=True, rm_effects=True, rm_names=True, rm_author=True)
            except ValueError as e:
                if "too many values to unpack" in str(e):
                    # Retry without name removal if the error is due to multiple colons in time references
                    # This can happen with lines like "at 10:00 and 2:00"
                    sub = Subtitles(self.path)
                    sub.filter(
                        rm_fonts=True, rm_ast=True, rm_music=True, rm_effects=True, rm_names=False, rm_author=True
                    )
                else:
                    raise
            sub.save()
            return
        elif sdh_method == "auto":
            # Try subby first for SRT files, then fall back
            if self.codec == Subtitle.Codec.SubRip:
                try:
                    stripper = SDHStripper()
                    stripped_srt, _ = stripper.from_file(str(self.path))
                    self.path.write_text(str(stripped_srt), encoding="utf8")
                    return
                except Exception:
                    pass  # Fall through to other methods

        if binaries.SubtitleEdit:
            if self.codec == Subtitle.Codec.SubStationAlphav4:
                output_format = "AdvancedSubStationAlpha"
            elif self.codec == Subtitle.Codec.TimedTextMarkupLang:
                output_format = "TimedText1.0"
            else:
                output_format = self.codec.name
            subprocess.run(
                [
                    binaries.SubtitleEdit,
                    "/Convert",
                    self.path,
                    output_format,
                    "/encoding:utf8",
                    "/overwrite",
                    "/RemoveTextForHI",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        else:
            if config.subtitle.get("convert_before_strip", True) and self.codec != Subtitle.Codec.SubRip:
                self.path = self.convert(Subtitle.Codec.SubRip)
                self.codec = Subtitle.Codec.SubRip

            try:
                sub = Subtitles(self.path)
                try:
                    sub.filter(
                        rm_fonts=True, rm_ast=True, rm_music=True, rm_effects=True, rm_names=True, rm_author=True
                    )
                except ValueError as e:
                    if "too many values to unpack" in str(e):
                        # Retry without name removal if the error is due to multiple colons in time references
                        # This can happen with lines like "at 10:00 and 2:00"
                        sub = Subtitles(self.path)
                        sub.filter(
                            rm_fonts=True, rm_ast=True, rm_music=True, rm_effects=True, rm_names=False, rm_author=True
                        )
                    else:
                        raise
                sub.save()
            except (IOError, OSError) as e:
                if "is not valid subtitle file" in str(e):
                    self.log.warning(f"Failed to strip SDH from {self.path.name}: {e}")
                    self.log.warning("Continuing without SDH stripping for this subtitle")
                else:
                    raise

    def reverse_rtl(self) -> None:
        """
        Reverse RTL (Right to Left) Start/End on Captions.
        This can be used to fix the positioning of sentence-ending characters.
        """
        if not self.path or not self.path.exists():
            raise ValueError("You must download the subtitle track first.")

        if not binaries.SubtitleEdit:
            raise EnvironmentError("SubtitleEdit executable not found...")

        if self.codec == Subtitle.Codec.SubStationAlphav4:
            output_format = "AdvancedSubStationAlpha"
        elif self.codec == Subtitle.Codec.TimedTextMarkupLang:
            output_format = "TimedText1.0"
        else:
            output_format = self.codec.name

        subprocess.run(
            [
                binaries.SubtitleEdit,
                "/Convert",
                self.path,
                output_format,
                "/ReverseRtlStartEnd",
                "/encoding:utf8",
                "/overwrite",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )


__all__ = ("Subtitle",)
