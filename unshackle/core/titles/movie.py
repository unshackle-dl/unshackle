from abc import ABC
from typing import Any, Iterable, Optional, Union

from langcodes import Language
from pymediainfo import MediaInfo
from rich.tree import Tree
from sortedcontainers import SortedKeyList

from unshackle.core.config import config
from unshackle.core.constants import AUDIO_CODEC_MAP, DYNAMIC_RANGE_MAP, VIDEO_CODEC_MAP
from unshackle.core.titles.title import Title
from unshackle.core.utilities import sanitize_filename


class Movie(Title):
    def __init__(
        self,
        id_: Any,
        service: type,
        name: str,
        year: Optional[Union[int, str]] = None,
        language: Optional[Union[str, Language]] = None,
        data: Optional[Any] = None,
        description: Optional[str] = None,
    ) -> None:
        super().__init__(id_, service, language, data)

        if not name:
            raise ValueError("Movie name must be provided")
        if not isinstance(name, str):
            raise TypeError(f"Expected name to be a str, not {name!r}")

        if year is not None:
            if isinstance(year, str) and year.isdigit():
                year = int(year)
            elif not isinstance(year, int):
                raise TypeError(f"Expected year to be an int, not {year!r}")

        name = name.strip()

        if year is not None and year <= 0:
            raise ValueError(f"Movie year cannot be {year}")

        self.name = name
        self.year = year
        self.description = description

    def __str__(self) -> str:
        if self.year:
            if config.dash_naming:
                return f"{self.name} - {self.year}"
            return f"{self.name} ({self.year})"
        return self.name

    def get_filename(self, media_info: MediaInfo, folder: bool = False, show_service: bool = True) -> str:
        primary_video_track = next(iter(media_info.video_tracks), None)
        primary_audio_track = None
        if media_info.audio_tracks:
            sorted_audio = sorted(
                media_info.audio_tracks,
                key=lambda x: (
                    float(x.bit_rate) if x.bit_rate else 0,
                    bool(x.format_additionalfeatures and "JOC" in x.format_additionalfeatures),
                ),
                reverse=True,
            )
            primary_audio_track = sorted_audio[0]
        unique_audio_languages = len({x.language.split("-")[0] for x in media_info.audio_tracks if x.language})

        # Name (Year)
        name = str(self).replace("$", "S")  # e.g., Arli$$

        if config.scene_naming:
            # Resolution
            if primary_video_track:
                resolution = primary_video_track.height
                aspect_ratio = [
                    int(float(plane)) for plane in primary_video_track.other_display_aspect_ratio[0].split(":")
                ]
                if len(aspect_ratio) == 1:
                    # e.g., aspect ratio of 2 (2.00:1) would end up as `(2.0,)`, add 1
                    aspect_ratio.append(1)
                if aspect_ratio[0] / aspect_ratio[1] not in (16 / 9, 4 / 3):
                    # We want the resolution represented in a 4:3 or 16:9 canvas.
                    # If it's not 4:3 or 16:9, calculate as if it's inside a 16:9 canvas,
                    # otherwise the track's height value is fine.
                    # We are assuming this title is some weird aspect ratio so most
                    # likely a movie or HD source, so it's most likely widescreen so
                    # 16:9 canvas makes the most sense.
                    resolution = int(primary_video_track.width * (9 / 16))
                # Determine scan type suffix - default to "p", use "i" only if explicitly interlaced
                scan_suffix = "p"
                scan_type = getattr(primary_video_track, 'scan_type', None)
                if scan_type and str(scan_type).lower() == "interlaced":
                    scan_suffix = "i"
                name += f" {resolution}{scan_suffix}"

            # Service (use track source if available)
            if show_service:
                source_name = None
                if self.tracks:
                    first_track = next(iter(self.tracks), None)
                    if first_track and hasattr(first_track, "source") and first_track.source:
                        source_name = first_track.source
                name += f" {source_name or self.service.__name__}"

            # 'WEB-DL'
            name += " WEB-DL"

            # DUAL
            if unique_audio_languages == 2:
                name += " DUAL"

            # MULTi
            if unique_audio_languages > 2:
                name += " MULTi"

            # Audio Codec + Channels (+ feature)
            if primary_audio_track:
                codec = primary_audio_track.format
                channel_layout = primary_audio_track.channel_layout or primary_audio_track.channellayout_original
                if channel_layout:
                    channels = float(
                        sum({"LFE": 0.1}.get(position.upper(), 1) for position in channel_layout.split(" "))
                    )
                else:
                    channel_count = primary_audio_track.channel_s or primary_audio_track.channels or 0
                    channels = float(channel_count)

                features = primary_audio_track.format_additionalfeatures or ""
                name += f" {AUDIO_CODEC_MAP.get(codec, codec)}{channels:.1f}"
                if "JOC" in features or primary_audio_track.joc:
                    name += " Atmos"

            # Video (dynamic range + hfr +) Codec
            if primary_video_track:
                codec = primary_video_track.format
                hdr_format = primary_video_track.hdr_format_commercial
                hdr_format_full = primary_video_track.hdr_format or ""
                trc = (
                    primary_video_track.transfer_characteristics
                    or primary_video_track.transfer_characteristics_original
                    or ""
                )
                frame_rate = float(primary_video_track.frame_rate)

                # Primary HDR format detection
                if hdr_format:
                    if hdr_format_full.startswith("Dolby Vision"):
                        name += " DV"
                        if any(
                            indicator in (hdr_format_full + " " + hdr_format)
                            for indicator in ["HDR10", "SMPTE ST 2086"]
                        ):
                            name += " HDR"
                    else:
                        name += f" {DYNAMIC_RANGE_MAP.get(hdr_format)} "
                elif "HLG" in trc or "Hybrid Log-Gamma" in trc or "ARIB STD-B67" in trc or "arib-std-b67" in trc.lower():
                    name += " HLG"
                elif any(indicator in trc for indicator in ["PQ", "SMPTE ST 2084", "BT.2100"]) or "smpte2084" in trc.lower() or "bt.2020-10" in trc.lower():
                    name += " HDR"
                if frame_rate > 30:
                    name += " HFR"
                name += f" {VIDEO_CODEC_MAP.get(codec, codec)}"

            if config.tag:
                name += f"-{config.tag}"

            return sanitize_filename(name)
        else:
            # Simple naming style without technical details - use spaces instead of dots
            return sanitize_filename(name, " ")


class Movies(SortedKeyList, ABC):
    def __init__(self, iterable: Optional[Iterable] = None):
        super().__init__(iterable, key=lambda x: x.year or 0)

    def __str__(self) -> str:
        if not self:
            return super().__str__()
        # TODO: Assumes there's only one movie
        return self[0].name + (f" ({self[0].year})" if self[0].year else "")

    def tree(self, verbose: bool = False) -> Tree:
        num_movies = len(self)
        tree = Tree(f"{num_movies} Movie{['s', ''][num_movies == 1]}", guide_style="bright_black")
        if verbose:
            for movie in self:
                tree.add(f"[bold]{movie.name}[/] [bright_black]({movie.year or '?'})", guide_style="bright_black")

        return tree


__all__ = ("Movie", "Movies")
