from __future__ import annotations

import math
import subprocess
from enum import Enum
from typing import Any, Optional, Union

from unshackle.core import binaries
from unshackle.core.tracks.track import Track


class Audio(Track):
    class Codec(str, Enum):
        AAC = "AAC"  # https://wikipedia.org/wiki/Advanced_Audio_Coding
        AC3 = "DD"  # https://wikipedia.org/wiki/Dolby_Digital
        EC3 = "DD+"  # https://wikipedia.org/wiki/Dolby_Digital_Plus
        AC4 = "AC-4"  # https://wikipedia.org/wiki/Dolby_AC-4
        OPUS = "OPUS"  # https://wikipedia.org/wiki/Opus_(audio_format)
        OGG = "VORB"  # https://wikipedia.org/wiki/Vorbis
        DTS = "DTS"  # https://en.wikipedia.org/wiki/DTS,_Inc.#DTS_Digital_Surround
        DTSX = "DTS-X"  # https://en.wikipedia.org/wiki/DTS,_Inc.#DTS:X
        ALAC = "ALAC"  # https://en.wikipedia.org/wiki/Apple_Lossless_Audio_Codec
        FLAC = "FLAC"  # https://en.wikipedia.org/wiki/FLAC

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
            if mime == "alac":
                return Audio.Codec.ALAC
            if mime == "flac":
                return Audio.Codec.FLAC
            raise ValueError(f"The MIME '{mime}' is not a supported Audio Codec")

        @staticmethod
        def from_codecs(codecs: str) -> Audio.Codec:
            for codec in codecs.lower().split(","):
                mime = codec.strip().split(".")[0]
                try:
                    return Audio.Codec.from_mime(mime)
                except ValueError:
                    pass
            raise ValueError(f"No MIME types matched any supported Audio Codecs in '{codecs}'")

        @staticmethod
        def from_netflix_profile(profile: str) -> Audio.Codec:
            profile = profile.lower().strip()
            if profile.startswith("heaac") or profile.startswith("xheaac"):
                return Audio.Codec.AAC
            if profile.startswith("dd-"):
                return Audio.Codec.AC3
            if profile.startswith("ddplus"):
                return Audio.Codec.EC3
            if profile.startswith("ac4"):
                return Audio.Codec.AC4
            if profile.startswith("playready-oggvorbis"):
                return Audio.Codec.OGG
            raise ValueError(f"The Content Profile '{profile}' is not a supported Audio Codec")

    def __init__(
        self,
        *args: Any,
        codec: Optional[Audio.Codec] = None,
        bitrate: Optional[Union[str, int, float]] = None,
        channels: Optional[Union[str, int, float]] = None,
        joc: Optional[int] = None,
        descriptive: Union[bool, int] = False,
        **kwargs: Any,
    ):
        """
        Make a new Audio track object.

        Parameters:
            codec: An Audio.Codec enum representing the audio codec.
                If not specified, unshackle uses MediaInfo to get the codec
                after it downloads the track.
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
                bool,
                [
                    "AUD",
                    f"[{self.codec.value}]" if self.codec else None,
                    str(self.language),
                    ", ".join(
                        filter(
                            bool,
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
            # TODO: Support all possible DASH channel configurations (https://datatracker.ietf.org/doc/html/rfc8216)
            if channels.upper() == "A000":
                return 2.0
            elif channels.upper() == "F801":
                return 5.1
            layout = channels.replace("ch", "")
            if layout.count(".") == 2 and all(part.isdigit() for part in layout.split(".")):
                return layout
            elif layout.replace(".", "", 1).isdigit():
                return float(layout)
            raise NotImplementedError(f"Unsupported Channels string value, '{channels}'")

        return float(channels)

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

    def to_music_container(self) -> bool:
        """Remux the track into the container of its codec, as a standalone audio file.

        Music titles never reach the muxer, so the downloaded file is the delivered file, and
        after decryption that is still a fragmented MP4. Its header states a length of zero, so
        a player stops after the first fragment. FLAC inside an MP4 is also not a FLAC stream.
        A rename cannot correct either fault. Returns True once the new file replaces the
        downloaded one; every failure raises.
        """
        if not self.path or not self.path.exists():
            raise ValueError("Cannot remux a Track that has not been downloaded.")

        if not binaries.FFMPEG:
            raise EnvironmentError('FFmpeg executable "ffmpeg" was not found but is required for this call.')

        containers: dict[Optional[Audio.Codec], str] = {
            Audio.Codec.FLAC: ".flac",
            Audio.Codec.OPUS: ".opus",
            Audio.Codec.OGG: ".ogg",
        }
        extension = containers.get(self.codec, ".m4a")
        original_path = self.path
        output_path = original_path.with_name(f"{original_path.stem}_music{extension}")

        def ffmpeg(*extra_args: str) -> None:
            subprocess.run(
                [
                    str(binaries.FFMPEG),
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(original_path),
                    "-map",
                    "0:a:0",
                    "-map_metadata",
                    "-1",
                    # a copy keeps the source STREAMINFO, whose sample count a fragmenting writer
                    # leaves at zero, so the FLAC states no length; the encoder writes a real one
                    *(["-c:a", "flac"] if extension == ".flac" else ["-c", "copy"]),
                    # the fragmented moov is what cuts playback short, so ask for one contiguous header
                    *(["-movflags", "+faststart"] if extension == ".m4a" else []),
                    *extra_args,
                    str(output_path),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        try:
            try:
                ffmpeg()
            except subprocess.CalledProcessError as e:
                if b"not currently supported in container" not in e.stderr:
                    raise
                # a .m4a name picks FFmpeg's ipod muxer, which refuses Dolby Digital Plus and the
                # other codecs Apple never put in an M4A; the plain mp4 muxer accepts them
                ffmpeg("-f", "mp4")
        except subprocess.CalledProcessError:
            # FFmpeg creates the output before it fails, and nothing sweeps the shared temp
            # directory for it, so a failed run must remove its own partial file
            output_path.unlink(missing_ok=True)
            raise

        original_path.unlink()
        self.path = output_path
        return True

    def get_track_name(self) -> Optional[str]:
        """Return the base Track Name."""
        track_name = super().get_track_name() or ""
        flag = self.descriptive and "Descriptive"
        if flag:
            if track_name:
                flag = f" ({flag})"
            track_name += flag
        return track_name or None


__all__ = ("Audio",)
