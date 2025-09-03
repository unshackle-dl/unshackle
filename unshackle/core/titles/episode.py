import re
from abc import ABC
from collections import Counter
from typing import Any, Iterable, Optional, Union

from langcodes import Language
from pymediainfo import MediaInfo
from rich.tree import Tree
from sortedcontainers import SortedKeyList

from unshackle.core.config import config
from unshackle.core.constants import AUDIO_CODEC_MAP, DYNAMIC_RANGE_MAP, VIDEO_CODEC_MAP
from unshackle.core.titles.title import Title
from unshackle.core.utilities import sanitize_filename
from unshackle.core.utils.template_formatter import TemplateFormatter


class Episode(Title):
    def __init__(
        self,
        id_: Any,
        service: type,
        title: str,
        season: Union[int, str],
        number: Union[int, str],
        name: Optional[str] = None,
        year: Optional[Union[int, str]] = None,
        language: Optional[Union[str, Language]] = None,
        data: Optional[Any] = None,
        description: Optional[str] = None,
    ) -> None:
        super().__init__(id_, service, language, data)

        if not title:
            raise ValueError("Episode title must be provided")
        if not isinstance(title, str):
            raise TypeError(f"Expected title to be a str, not {title!r}")

        if season != 0 and not season:
            raise ValueError("Episode season must be provided")
        if isinstance(season, str) and season.isdigit():
            season = int(season)
        elif not isinstance(season, int):
            raise TypeError(f"Expected season to be an int, not {season!r}")

        if number != 0 and not number:
            raise ValueError("Episode number must be provided")
        if isinstance(number, str) and number.isdigit():
            number = int(number)
        elif not isinstance(number, int):
            raise TypeError(f"Expected number to be an int, not {number!r}")

        if name is not None and not isinstance(name, str):
            raise TypeError(f"Expected name to be a str, not {name!r}")

        if year is not None:
            if isinstance(year, str) and year.isdigit():
                year = int(year)
            elif not isinstance(year, int):
                raise TypeError(f"Expected year to be an int, not {year!r}")

        title = title.strip()

        if name is not None:
            name = name.strip()
            # ignore episode names that are the episode number or title name
            if re.match(r"Episode ?#?\d+", name, re.IGNORECASE):
                name = None
            elif name.lower() == title.lower():
                name = None

        if year is not None and year <= 0:
            raise ValueError(f"Episode year cannot be {year}")

        self.title = title
        self.season = season
        self.number = number
        self.name = name
        self.year = year
        self.description = description

    def _build_template_context(self, media_info: MediaInfo, show_service: bool = True) -> dict:
        """Build template context dictionary from MediaInfo."""
        primary_video_track = next(iter(media_info.video_tracks), None)
        primary_audio_track = next(iter(media_info.audio_tracks), None)
        unique_audio_languages = len({x.language.split("-")[0] for x in media_info.audio_tracks if x.language})

        context = {
            "title": self.title.replace("$", "S"),
            "year": self.year or "",
            "season": f"S{self.season:02}",
            "episode": f"E{self.number:02}",
            "season_episode": f"S{self.season:02}E{self.number:02}",
            "episode_name": self.name or "",
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
        return "{title}{year} S{season:02}E{number:02} {name}".format(
            title=self.title,
            year=f" {self.year}" if self.year else "",
            season=self.season,
            number=self.number,
            name=self.name or "",
        ).strip()

    def get_filename(self, media_info: MediaInfo, folder: bool = False, show_service: bool = True) -> str:
        if folder:
            # For folders, use the series template but exclude episode-specific variables
            series_template = config.output_template.get("series")
            if series_template:
                # Create a folder-friendly version by removing episode-specific variables
                folder_template = series_template
                # Remove episode number and episode name from template for folders
                folder_template = re.sub(r'\{episode\}', '', folder_template)
                folder_template = re.sub(r'\{episode_name\?\}', '', folder_template)
                folder_template = re.sub(r'\{episode_name\}', '', folder_template)
                folder_template = re.sub(r'\{season_episode\}', '{season}', folder_template)

                # Clean up any double separators that might result
                folder_template = re.sub(r'\.{2,}', '.', folder_template)
                folder_template = re.sub(r'\s{2,}', ' ', folder_template)
                folder_template = re.sub(r'^[\.\s]+|[\.\s]+$', '', folder_template)

                formatter = TemplateFormatter(folder_template)
                context = self._build_template_context(media_info, show_service)
                # Override season_episode with just season for folders
                context['season'] = f"S{self.season:02}"

                folder_name = formatter.format(context)

                # Keep the same separator style as the series template
                if '.' in series_template and ' ' not in series_template:
                    # Dot-based template - use dot separator for folders too
                    return sanitize_filename(folder_name, ".")
                else:
                    # Space-based template - use space separator
                    return sanitize_filename(folder_name, " ")
            else:
                # Fallback to simple naming if no template defined
                name = f"{self.title}"
                if self.year:
                    name += f" {self.year}"
                name += f" S{self.season:02}"
                return sanitize_filename(name, " ")

        # Use template from output_template (which includes scene_naming compatibility)
        # or fallback to default scene-style template
        template = (
            config.output_template.get("series")
            or "{title}.{year?}.{season_episode}.{episode_name?}.{quality}.{source}.WEB-DL.{dual?}.{multi?}.{audio_full}.{atmos?}.{hfr?}.{video}-{tag}"
        )

        formatter = TemplateFormatter(template)
        context = self._build_template_context(media_info, show_service)
        return formatter.format(context)


class Series(SortedKeyList, ABC):
    def __init__(self, iterable: Optional[Iterable] = None):
        super().__init__(iterable, key=lambda x: (x.season, x.number, x.year or 0))

    def __str__(self) -> str:
        if not self:
            return super().__str__()
        return self[0].title + (f" ({self[0].year})" if self[0].year else "")

    def tree(self, verbose: bool = False) -> Tree:
        seasons = Counter(x.season for x in self)
        num_seasons = len(seasons)
        sum(seasons.values())
        season_breakdown = ", ".join(f"S{season}({count})" for season, count in sorted(seasons.items()))
        tree = Tree(
            f"{num_seasons} seasons, {season_breakdown}",
            guide_style="bright_black",
        )
        if verbose:
            for season, episodes in seasons.items():
                season_tree = tree.add(
                    f"[bold]Season {str(season).zfill(len(str(num_seasons)))}[/]: [bright_black]{episodes} episodes",
                    guide_style="bright_black",
                )
                for episode in self:
                    if episode.season == season:
                        if episode.name:
                            season_tree.add(
                                f"[bold]{str(episode.number).zfill(len(str(episodes)))}.[/] "
                                f"[bright_black]{episode.name}"
                            )
                        else:
                            season_tree.add(f"[bright_black]Episode {str(episode.number).zfill(len(str(episodes)))}")

        return tree


__all__ = ("Episode", "Series")
