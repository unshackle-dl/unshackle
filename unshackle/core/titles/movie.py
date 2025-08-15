from abc import ABC
from typing import Any, Iterable, Optional, Union

from langcodes import Language
from pymediainfo import MediaInfo
from rich.tree import Tree
from sortedcontainers import SortedKeyList

from unshackle.core.config import config
from unshackle.core.constants import AUDIO_CODEC_MAP, DYNAMIC_RANGE_MAP, VIDEO_CODEC_MAP
from unshackle.core.titles.title import Title
from unshackle.core.utils.template_formatter import TemplateFormatter


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

    def _build_template_context(self, media_info: MediaInfo, show_service: bool = True) -> dict:
        """Build template context dictionary from MediaInfo."""
        primary_video_track = next(iter(media_info.video_tracks), None)
        primary_audio_track = next(iter(media_info.audio_tracks), None)
        unique_audio_languages = len({x.language.split("-")[0] for x in media_info.audio_tracks if x.language})

        context = {
            "title": self.name.replace("$", "S"),
            "year": self.year or "",
            "tag": config.tag or "",
            "source": self.service.__name__ if show_service else "",
        }

        # Video information
        if primary_video_track:
            resolution = primary_video_track.height
            aspect_ratio = [int(float(plane)) for plane in primary_video_track.other_display_aspect_ratio[0].split(":")]
            if len(aspect_ratio) == 1:
                aspect_ratio.append(1)
            if aspect_ratio[0] / aspect_ratio[1] not in (16 / 9, 4 / 3):
                resolution = int(primary_video_track.width * (9 / 16))

            context.update(
                {
                    "quality": f"{resolution}p",
                    "resolution": str(resolution),
                    "video": VIDEO_CODEC_MAP.get(primary_video_track.format, primary_video_track.format),
                }
            )

            # HDR information
            hdr_format = primary_video_track.hdr_format_commercial
            trc = primary_video_track.transfer_characteristics or primary_video_track.transfer_characteristics_original
            if hdr_format:
                if (primary_video_track.hdr_format or "").startswith("Dolby Vision"):
                    context["hdr"] = "DV"
                    base_layer = DYNAMIC_RANGE_MAP.get(hdr_format)
                    if base_layer and base_layer != "DV":
                        context["hdr"] += f".{base_layer}"
                else:
                    context["hdr"] = DYNAMIC_RANGE_MAP.get(hdr_format, "")
            elif trc and "HLG" in trc:
                context["hdr"] = "HLG"
            else:
                context["hdr"] = ""

            # High frame rate
            frame_rate = float(primary_video_track.frame_rate)
            context["hfr"] = "HFR" if frame_rate > 30 else ""

        # Audio information
        if primary_audio_track:
            codec = primary_audio_track.format
            channel_layout = primary_audio_track.channel_layout or primary_audio_track.channellayout_original

            if channel_layout:
                channels = float(sum({"LFE": 0.1}.get(position.upper(), 1) for position in channel_layout.split(" ")))
            else:
                channel_count = primary_audio_track.channel_s or primary_audio_track.channels or 0
                channels = float(channel_count)

            features = primary_audio_track.format_additionalfeatures or ""

            context.update(
                {
                    "audio": AUDIO_CODEC_MAP.get(codec, codec),
                    "audio_channels": f"{channels:.1f}",
                    "audio_full": f"{AUDIO_CODEC_MAP.get(codec, codec)}{channels:.1f}",
                    "atmos": "Atmos" if ("JOC" in features or primary_audio_track.joc) else "",
                }
            )

        # Multi-language audio
        if unique_audio_languages == 2:
            context["dual"] = "DUAL"
            context["multi"] = ""
        elif unique_audio_languages > 2:
            context["dual"] = ""
            context["multi"] = "MULTi"
        else:
            context["dual"] = ""
            context["multi"] = ""

        return context

    def __str__(self) -> str:
        if self.year:
            return f"{self.name} ({self.year})"
        return self.name

    def get_filename(self, media_info: MediaInfo, folder: bool = False, show_service: bool = True) -> str:
        # Use custom template if defined, otherwise use default scene-style template
        template = (
            config.output_template.get("movies")
            or "{title}.{year}.{quality}.{source}.WEB-DL.{dual?}.{multi?}.{audio_full}.{atmos?}.{hdr?}.{hfr?}.{video}-{tag}"
        )

        formatter = TemplateFormatter(template)
        context = self._build_template_context(media_info, show_service)
        return formatter.format(context)


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
