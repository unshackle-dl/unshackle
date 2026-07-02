import re
from abc import ABC
from collections import Counter
from datetime import date, datetime
from typing import Any, Iterable, Optional, Union

from langcodes import Language
from pymediainfo import MediaInfo
from rich.tree import Tree
from sortedcontainers import SortedKeyList

from unshackle.core.config import config
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
        air_date: Optional[Union[date, str]] = None,
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

        if isinstance(air_date, datetime):
            air_date = air_date.date()  # avoid leaking time into the {date} token
        elif isinstance(air_date, str):
            # keep as date when parseable so naming can format it
            try:
                air_date = date.fromisoformat(air_date[:10])
            except ValueError:
                pass

        self.title = title
        self.season = season
        self.number = number
        self.name = name
        self.year = year
        self.description = description
        self.air_date = air_date

    def _build_template_context(self, media_info: MediaInfo, show_service: bool = True) -> dict:
        """Build template context dictionary from MediaInfo."""
        context = self._build_base_template_context(media_info, show_service)
        context["title"] = self.title.replace("$", "S")
        context["year"] = self.year or ""
        context["season"] = f"S{self.season:02}"
        context["episode"] = f"E{self.number:02}"
        context["season_episode"] = f"S{self.season:02}E{self.number:02}"
        context["episode_name"] = self.name or ""
        context["date"] = ""
        if self.air_date:
            # daily/sports: air date replaces SxxExx
            disp = self._air_date_display()
            context["season"] = disp
            context["episode"] = ""
            context["season_episode"] = disp
            context["year"] = ""  # air date is the sole date in the file; folders keep the year
            context["date"] = self.air_date.isoformat() if isinstance(self.air_date, date) else str(self.air_date)
        return context

    def _air_date_display(self) -> str:
        """Render air_date using the series template's own separator (dots or spaces)."""
        if isinstance(self.air_date, date):
            sep = config.get_template_separator("series") if config.output_template.get("series") else "."
            return f"{self.air_date.year:04}{sep}{self.air_date.month:02}{sep}{self.air_date.day:02}"
        return str(self.air_date)

    def _folder_season(self) -> str:
        """Season folder label: air year for dated content, else SxxExx-style season."""
        if isinstance(self.air_date, date):
            return f"{self.air_date.year:04}"
        return f"S{self.season:02}"

    def __str__(self) -> str:
        if self.air_date:
            return "{title}{year} {date} {name}".format(
                title=self.title,
                year=f" {self.year}" if self.year else "",
                date=self._air_date_display(),
                name=self.name or "",
            ).strip()
        return "{title}{year} S{season:02}E{number:02} {name}".format(
            title=self.title,
            year=f" {self.year}" if self.year else "",
            season=self.season,
            number=self.number,
            name=self.name or "",
        ).strip()

    def get_filename(self, media_info: MediaInfo, folder: bool = False, show_service: bool = True) -> str:
        if folder:
            template = config.get_folder_template("series")
            if template:
                context = self._build_template_context(media_info, show_service)
                context["season"] = self._folder_season()
                context["year"] = self.year or ""  # folders keep the year
                segments = [
                    TemplateFormatter(seg).format(context) for seg in re.split(r"[\\/]", template) if seg.strip()
                ]
                return "/".join(s for s in segments if s)

            series_template = config.output_template.get("series")
            if series_template:
                derived_template = series_template
                derived_template = re.sub(r"\{episode\}", "", derived_template)
                derived_template = re.sub(r"\{episode_name\?\}", "", derived_template)
                derived_template = re.sub(r"\{episode_name\}", "", derived_template)
                derived_template = re.sub(r"\{season_episode\}", "{season}", derived_template)

                derived_template = re.sub(r"\.{2,}", ".", derived_template)
                derived_template = re.sub(r"\s{2,}", " ", derived_template)
                derived_template = re.sub(r"^[\.\s]+|[\.\s]+$", "", derived_template)

                formatter = TemplateFormatter(derived_template)
                context = self._build_template_context(media_info, show_service)
                context["season"] = self._folder_season()
                context["year"] = self.year or ""  # folders keep the year

                folder_name = formatter.format(context)

                separators = re.sub(r"\{[^}]*\}", "", derived_template)
                spacer = "." if "." in separators and " " not in separators else " "
                return sanitize_filename(folder_name, spacer)
            else:
                name = f"{self.title}"
                if self.year:
                    name += f" {self.year}"
                name += f" {self._folder_season()}"
                return sanitize_filename(name, " ")

        formatter = TemplateFormatter(config.output_template["series"])
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
            f"{num_seasons} season{'s'[: num_seasons ^ 1]}, {season_breakdown}",
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
                        label = (
                            episode._air_date_display()
                            if episode.air_date
                            else str(episode.number).zfill(len(str(episodes)))
                        )
                        if episode.name:
                            season_tree.add(f"[bold]{label}.[/] [bright_black]{episode.name}")
                        elif episode.air_date:
                            season_tree.add(f"[bright_black]{label}")
                        else:
                            season_tree.add(f"[bright_black]Episode {label}")

        return tree


__all__ = ("Episode", "Series")
