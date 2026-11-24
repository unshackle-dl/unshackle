from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import time
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

from unshackle.core import binaries
from unshackle.core.tracks.track import (
    MPEGH_SAMPLE_ENTRIES,
    Track,
    has_mp4_only_audio_entry,
    prepare_for_mp4box,
)
from unshackle.core.utils.subprocess import log_tool_run, run_step

log = logging.getLogger("Audio")

DOLBY_CHANNEL_BITS = (
    (1, "main"),
    (1, "main"),
    (1, "main"),
    (1, "main"),
    (1, "main"),
    (2, "main"),
    (2, "main"),
    (1, "main"),
    (1, "height"),
    (2, "main"),
    (2, "main"),
    (2, "height"),
    (1, "height"),
    (2, "height"),
    (1, "lfe"),
    (1, "lfe"),
)

COUNT_CHANNEL_LAYOUTS: dict[int, float] = {6: 5.1, 7: 6.1, 8: 7.1}


class Audio(Track):
    class Codec(str, Enum):
        AAC = "AAC"
        AC3 = "DD"
        EC3 = "DD+"
        AC4 = "AC-4"
        OPUS = "OPUS"
        OGG = "VORB"
        DTS = "DTS"
        DTSX = "DTS-X"
        DTSHD = "DTS-HD"
        MPEGH = "MPEG-H"
        AV3A = "Audio Vivid"
        ALAC = "ALAC"
        FLAC = "FLAC"
        MP3 = "MP3"
        PCM = "PCM"

        @property
        def extension(self) -> str:
            return self.name.lower()

        @staticmethod
        def from_mime(mime: str) -> Audio.Codec:
            mime = mime.lower().strip().split(".")[0]
            if mime == "mp4a":
                return Audio.Codec.AAC
            if mime == "ac-3":
                return Audio.Codec.AC3
            if mime == "ec-3":
                return Audio.Codec.EC3
            if mime == "ac-4":
                return Audio.Codec.AC4
            if mime == "opus":
                return Audio.Codec.OPUS
            if mime in ("dtsx", "dtsy"):
                return Audio.Codec.DTSX
            if mime == "dtsc":
                return Audio.Codec.DTS
            if mime in ("dtsh", "dtsl", "dtse"):
                return Audio.Codec.DTSHD
            if mime in MPEGH_SAMPLE_ENTRIES:
                return Audio.Codec.MPEGH
            if mime == "av3a":
                return Audio.Codec.AV3A
            if mime == "alac":
                return Audio.Codec.ALAC
            if mime == "flac":
                return Audio.Codec.FLAC
            if mime == "vorbis":
                return Audio.Codec.OGG
            if mime == "mp3":
                return Audio.Codec.MP3
            raise ValueError(f"The MIME '{mime}' is not a supported Audio Codec")

        @staticmethod
        def from_ts(stream_type: int, registration: bytes) -> Optional[Audio.Codec]:
            """The codec an MPEG-TS PMT entry names by its registration descriptor or ``stream_type``, else None."""
            if registration in (b"AVSA", b"av3a"):
                return Audio.Codec.AV3A
            return {0x0F: Audio.Codec.AAC, 0x81: Audio.Codec.AC3, 0x87: Audio.Codec.EC3, 0xD5: Audio.Codec.AV3A}.get(
                stream_type
            )

        @staticmethod
        def from_ffprobe(codec_name: str, profile: str = "") -> Optional[Audio.Codec]:
            """Map an FFprobe ``codec_name`` to a codec, or None for one core does not name."""
            if codec_name.startswith("pcm_"):
                return Audio.Codec.PCM
            if codec_name == "dts":
                return Audio.Codec.DTSHD if "DTS-HD" in profile or "Express" in profile else Audio.Codec.DTS
            return FFPROBE_CODECS.get(codec_name)

        @staticmethod
        def from_codecs(codecs: str) -> Audio.Codec:
            for codec in codecs.lower().split(","):
                mime = codec.strip().split(".")[0]
                try:
                    return Audio.Codec.from_mime(mime)
                except ValueError:
                    pass
            raise ValueError(f"No MIME types matched any supported Audio Codecs in '{codecs}'")

    def __init__(
        self,
        *args: Any,
        codec: Optional[Audio.Codec] = None,
        bitrate: Optional[Union[str, int, float]] = None,
        channels: Optional[Union[str, int, float]] = None,
        joc: Optional[int] = None,
        descriptive: Union[bool, int] = False,
        **kwargs: Any,
    ) -> None:
        """
        Make a new Audio track object.

        Parameters:
            codec: An Audio.Codec enum representing the audio codec. The music remux
                replaces it with the codec FFprobe reads from the downloaded file.
            bitrate: A number or float representing the average bandwidth in bits/s.
                unshackle rounds float values up to the nearest integer.
            channels: A number, float, or string representing the number of audio channels.
                Strings may represent numbers or floats. Expanded layouts like 7.1.1 is
                not supported. All numbers and strings will be cast to float.
            joc: The number of Joint-Object-Coding Channels/Objects in the audio track.
            descriptive: Mark this audio as being descriptive audio for the blind.

        Note: If codec, bitrate, channels, or joc is not specified some checks may be
        skipped or assume a value. Specifying as much information as possible is highly
        recommended.
        """
        super().__init__(*args, **kwargs)

        if not isinstance(codec, (Audio.Codec, type(None))):
            raise TypeError(f"Expected codec to be a {Audio.Codec}, not {codec!r}")
        if not isinstance(bitrate, (str, int, float, type(None))):
            raise TypeError(f"Expected bitrate to be a {str}, {int}, or {float}, not {bitrate!r}")
        if not isinstance(channels, (str, int, float, type(None))):
            raise TypeError(f"Expected channels to be a {str}, {int}, or {float}, not {channels!r}")
        if not isinstance(joc, (int, type(None))):
            raise TypeError(f"Expected joc to be a {int}, not {joc!r}")
        if not isinstance(descriptive, (bool, int)) or (isinstance(descriptive, int) and descriptive not in (0, 1)):
            raise TypeError(f"Expected descriptive to be a {bool} or bool-like {int}, not {descriptive!r}")

        self.codec = codec

        try:
            self.bitrate = int(math.ceil(float(bitrate))) if bitrate else None
        except (ValueError, TypeError) as e:
            raise ValueError(f"Expected bitrate to be a number or float, {e}")

        try:
            self.channels = self.parse_channels(channels) if channels else None
        except (ValueError, NotImplementedError) as e:
            raise ValueError(f"Expected channels to be a number, float, or a string, {e}")

        self.joc = joc
        self.descriptive = bool(descriptive)

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "codec": self.codec.name if self.codec else None,
                "bitrate": self.bitrate,
                "channels": self.channels,
                "joc": self.joc,
                "descriptive": self.descriptive,
            }
        )
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Audio:
        kwargs = Track.base_kwargs_from_dict(data)
        return cls(
            **kwargs,
            codec=Audio.Codec[data["codec"]] if data.get("codec") else None,
            bitrate=data.get("bitrate"),
            channels=data.get("channels"),
            joc=data.get("joc"),
            descriptive=data.get("descriptive", False),
        )

    @property
    def atmos(self) -> bool:
        """Return True if the audio track contains Atmos."""
        if self.joc:
            return True
        if isinstance(self.extra, dict):
            return bool(self.extra.get("atmos") or self.extra.get("joc"))
        return False

    def __str__(self) -> str:
        return " | ".join(
            filter(
                None,
                [
                    "AUD",
                    f"[{self.codec.value}]" if self.codec else None,
                    str(self.language),
                    ", ".join(
                        filter(
                            None,
                            [
                                str(self.channels) if self.channels else None,
                                "Atmos" if self.atmos else None,
                                f"JOC {self.joc}" if self.joc else None,
                            ],
                        )
                    ),
                    f"{self.bitrate // 1000} kb/s" if self.bitrate else None,
                    self.get_track_name(),
                    ", ".join(self.edition) if self.edition else None,
                    self.note,
                ],
            )
        )

    @staticmethod
    def parse_channels(channels: Union[str, int, float]) -> Union[float, str]:
        """
        Converts a Channel string to a float representing the audio channel layout.
        E.g. "3" -> "3.0", "2.1" -> "2.1", ".1" -> "0.1".

        An immersive layout names its height channels in a third figure, e.g. "5.1.4",
        which no float can hold, so it stays the string it came as. Use channel_total for
        arithmetic on either form.

        This does not validate channel strings as genuine channel counts or valid layouts.
        It does not convert the value to assume a sub speaker channel layout, e.g. 5.1->6.0.
        """
        if isinstance(channels, str):
            if re.fullmatch(r"[0-9A-Fa-f]{4}", channels) and not channels.isdigit():
                return Audio.dolby_channel_layout(int(channels, 16))
            layout = channels.replace("ch", "")
            if layout.count(".") == 2 and all(part.isdigit() for part in layout.split(".")):
                return layout
            elif layout.replace(".", "", 1).isdigit():
                return float(layout)
            raise NotImplementedError(f"Unsupported Channels string value, '{channels}'")

        return float(channels)

    @staticmethod
    def dolby_channel_layout(mask: int) -> Union[float, str]:
        """Channel layout of a Dolby DASH channel configuration mask, e.g. 0xF801 -> 5.1."""
        counts = {"main": 0, "lfe": 0, "height": 0}
        for bit, (count, kind) in enumerate(DOLBY_CHANNEL_BITS):
            if mask & (0x8000 >> bit):
                counts[kind] += count
        if counts["height"]:
            return f"{counts['main']}.{counts['lfe']}.{counts['height']}"
        return float(f"{counts['main']}.{counts['lfe']}")

    @staticmethod
    def channels_from_count(count: Union[str, int, None]) -> Union[float, str, None]:
        """
        Channel layout for a bare number of channels, as HLS CHANNELS, Smooth Streaming and most DASH schemes give it.

        A count does not say which channel is the LFE, so 6, 7 and 8 take the 5.1, 6.1 and 7.1 layouts those streams
        carry in practice. Any other value goes back as-is, for parse_channels to read.
        """
        if count is None or (isinstance(count, str) and not count.isdigit()):
            return count
        return COUNT_CHANNEL_LAYOUTS.get(int(count), float(count))

    @staticmethod
    def channel_total(channels: Union[str, int, float]) -> float:
        """
        Total number of channels in either channel form, so a caller can compare both.

        A float layout keeps its own value, because a caller compares those by rounding
        the sub channel up, e.g. 5.1 and 6.0 both match. An immersive layout has no such
        float, so this adds its figures: "5.1.4" -> 10.0.
        """
        if isinstance(channels, str) and channels.count(".") == 2:
            return float(sum(int(part) for part in channels.split(".")))
        return float(channels)

    def probe_stream(self) -> dict[str, Any]:
        """FFprobe codec fields of the first audio track plus the file ``duration``; {} when it cannot probe."""
        if not binaries.FFProbe or not self.path or not self.path.exists():
            return {}
        probe_start = time.monotonic()
        probe = subprocess.run(
            [
                str(binaries.FFProbe),
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,profile,sample_fmt,bits_per_raw_sample,sample_rate:format=duration",
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
            "ffprobe audio", "ffprobe", probe.returncode, duration_ms=round((time.monotonic() - probe_start) * 1000, 1)
        )
        try:
            data = json.loads(probe.stdout)
            stream: dict[str, Any] = data["streams"][0]
            stream["duration"] = float((data.get("format") or {}).get("duration") or 0)
        except (ValueError, KeyError, IndexError, TypeError):
            return {}
        return stream

    def to_music_container(self, start: float = 0.0, duration: Optional[float] = None) -> None:
        """Remux the track into the container of the codec FFprobe reads from the file, cut to the sample window.

        The declared codec is used only when FFprobe cannot read the file. Every failure raises and keeps the
        downloaded file. Audio Vivid raises: FFprobe reads it as MP3, and no music container carries it.
        """
        if not self.path or not self.path.exists():
            raise ValueError("Cannot remux a Track that has not been downloaded.")
        if self.codec == Audio.Codec.AV3A:
            raise ValueError(f"No music container carries Audio Vivid track {self.id}")

        stream = self.probe_stream()
        probed = Audio.Codec.from_ffprobe(stream.get("codec_name", ""), stream.get("profile", ""))
        if probed and probed != self.codec:
            log.debug(f"Audio track {self.id} declares {getattr(self.codec, 'value', None)} but holds {probed.value}")
            self.codec = probed
        if start > 0 and 0 < stream.get("duration", 0) <= start:
            raise ValueError(f"Sample window starts at {start:.0f}s, after the {stream['duration']:.0f}s track ends")

        source = self.path
        if self.codec in MP4BOX_CODECS or has_mp4_only_audio_entry(source):
            output = self.mp4box_music_container(source, start, duration)
        else:
            output = self.ffmpeg_music_container(source, stream, start, duration)
        source.unlink()
        self.path = output

    def ffmpeg_music_container(
        self, source: Path, stream: dict[str, Any], start: float, duration: Optional[float]
    ) -> Path:
        """Remux with FFmpeg. FLAC is encoded, never copied: FFmpeg's FLAC muxer writes a real sample count
        only from an encoder. Float PCM and PCM above 24 bits go to WAV: FLAC has no float samples, and FFmpeg
        writes 32-bit FLAC only as an experimental feature. 16- and 24-bit float, which FFmpeg cannot write,
        widen to 32-bit float. DTS goes to Matroska, because FFmpeg writes it into
        MP4 with an mp4a sample entry. Only AAC, ALAC and AC-3 take the ipod muxer (the M4A brand)."""
        if not binaries.FFMPEG:
            raise EnvironmentError('FFmpeg executable "ffmpeg" was not found but is required for this call.')

        sample_fmt = stream.get("sample_fmt")
        lossless_pcm = (
            self.codec == Audio.Codec.PCM
            and sample_fmt not in ("flt", "fltp", "dbl", "dblp")
            and (sample_fmt in ("u8", "s16", "s16p") or 4 <= int(stream.get("bits_per_raw_sample") or 0) <= 24)
        )
        if self.codec == Audio.Codec.FLAC or lossless_pcm:
            extension, codec_args = ".flac", ["-c:a", "flac"]
            if int(stream.get("sample_rate") or 0) > 384000:
                codec_args += ["-frame_size", "4608"]
        elif self.codec == Audio.Codec.PCM:
            pcm = (stream.get("codec_name") or "pcm_s32le").replace("be", "le")
            if pcm in ("pcm_f16le", "pcm_f24le"):
                pcm = "pcm_f32le"
            extension, codec_args = ".wav", ["-c:a", pcm]
        elif self.codec in COPY_CONTAINERS:
            extension, codec_args = COPY_CONTAINERS[self.codec], ["-c", "copy"]
        else:
            ipod = self.codec in (Audio.Codec.AAC, Audio.Codec.ALAC, Audio.Codec.AC3)
            extension, codec_args = ".m4a", ["-c", "copy", "-movflags", "+faststart", *([] if ipod else ["-f", "mp4"])]

        output = source.with_name(f"{source.stem}_music{extension}")
        run_step(
            [
                binaries.FFMPEG,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                *(["-ss", f"{start:.3f}"] if start > 0 else []),
                "-i",
                source,
                *(["-t", str(duration)] if duration else []),
                "-map",
                "0:a:0",
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-flags:a",
                "+bitexact",
                *codec_args,
                output,
            ],
            output=output,
            label="music remux",
        )
        return output

    def mp4box_music_container(self, source: Path, start: float, duration: Optional[float]) -> Path:
        """Rewrite the track as an unfragmented .m4a with MP4Box, for the codecs FFmpeg cannot write into MP4."""
        if not binaries.MP4Box:
            raise EnvironmentError(f"MP4Box (GPAC) not found, the {getattr(self.codec, 'value', None)} remux needs it")
        candidate = source.with_name(f"{source.stem}_init.mp4")
        output = source.with_name(f"{source.stem}_music.m4a")
        try:
            prepared = prepare_for_mp4box(source, candidate, self.id)
            run_step(
                [binaries.MP4Box, "-for-test", "-add", f"{prepared}#audio", "-new", output],
                output=output,
                label="music MP4Box remux",
            )
            if duration:
                cut = source.with_name(f"{source.stem}_cut.m4a")
                run_step(
                    [
                        binaries.MP4Box,
                        "-for-test",
                        "-splitx",
                        f"{start:.3f}:{start + duration:.3f}",
                        output,
                        "-out",
                        cut,
                    ],
                    output=cut,
                    label="music MP4Box sample cut",
                )
                os.replace(cut, output)
        except Exception:
            output.unlink(missing_ok=True)
            raise
        finally:
            candidate.unlink(missing_ok=True)
        return output

    def get_track_name(self) -> Optional[str]:
        """Return the base Track Name."""
        track_name = super().get_track_name() or ""
        flag = self.descriptive and "Descriptive"
        if flag:
            if track_name:
                flag = f" ({flag})"
            track_name += flag
        return track_name or None


FFPROBE_CODECS: dict[str, Audio.Codec] = {
    "aac": Audio.Codec.AAC,
    "alac": Audio.Codec.ALAC,
    "flac": Audio.Codec.FLAC,
    "opus": Audio.Codec.OPUS,
    "vorbis": Audio.Codec.OGG,
    "mp3": Audio.Codec.MP3,
    "ac3": Audio.Codec.AC3,
    "eac3": Audio.Codec.EC3,
    "ac4": Audio.Codec.AC4,
    "mpegh_3d_audio": Audio.Codec.MPEGH,
}

COPY_CONTAINERS: dict[Optional[Audio.Codec], str] = {
    Audio.Codec.OPUS: ".opus",
    Audio.Codec.OGG: ".ogg",
    Audio.Codec.MP3: ".mp3",
    Audio.Codec.DTS: ".mka",
    Audio.Codec.DTSHD: ".mka",
}

MP4BOX_CODECS = (Audio.Codec.AC4, Audio.Codec.MPEGH, Audio.Codec.DTSX)


__all__ = ("Audio",)
