import re
from abc import ABC
from collections import Counter
from datetime import date, datetime
from typing import Any, Collection, Iterable, Optional, Union

from langcodes import Language
from pymediainfo import MediaInfo
from rich.tree import Tree
from sortedcontainers import SortedKeyList

from unshackle.core.config import config
from unshackle.core.titles.title import Title
from unshackle.core.utilities import sanitize_filename
from unshackle.core.utils.template_formatter import TemplateFormatter, detect_spacer


class Episode(Title):
    # class-level defaults so Episodes restored from an older title cache read as part-less
    # and without an absolute number
    part: Optional[int] = None
    absolute: Optional[int] = None

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
        part: Optional[Union[int, str]] = None,
        absolute: Optional[Union[int, str]] = None,
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

        if part is not None:
            if isinstance(part, str) and part.isdigit():
                part = int(part)
            # bool is an int subclass; True would render as ".True" in keys and filenames
            elif isinstance(part, bool) or not isinstance(part, int):
                raise TypeError(f"Expected part to be an int, not {part!r}")
            # parts are 1-based; a falsy 0 would read as "no part" downstream
            if part <= 0:
                raise ValueError(f"Episode part cannot be {part}")

        if absolute is not None:
            if isinstance(absolute, str) and absolute.isdigit():
                absolute = int(absolute)
            # bool is an int subclass; True would render as "001" in filenames
            elif isinstance(absolute, bool) or not isinstance(absolute, int):
                raise TypeError(f"Expected absolute to be an int, not {absolute!r}")
            if absolute <= 0:
                raise ValueError(f"Episode absolute cannot be {absolute}")

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

        if isinstance(air_date, date) and air_date.year < 1970:
            raise ValueError(f"Episode air date cannot be {air_date}")

        self.title = title
        self.season = season
        self.number = number
        self.name = name
        self.year = year
        self.description = description
        self.air_date = air_date
        self.part = part
        self.absolute = absolute

    def matches_wanted(self, wanted: Collection[str]) -> bool:
        """Whether a parsed ``-w`` key set selects this episode.

        A part-ful episode answers to both its base key and its part key, so ``-w s1e1``
        takes every part. A dated episode also answers to its ISO air date.
        ``!`` keys are part-qualified exclusions resolved here.
        """
        base = f"{self.season}x{self.number}"
        keys = (base,) if self.part is None else (base, f"{base}.{self.part}")
        if isinstance(self.air_date, date):
            keys = (*keys, self.air_date.isoformat())
        if any(f"!{k}" in wanted for k in keys):
            return False
        return any(k in wanted for k in keys)

    def _part_suffix(self) -> str:
        """``.Part.2`` / `` Part 2`` in the series template's own separator style."""
        if self.part is None:
            return ""
        sep = config.get_template_separator("series") if config.output_template.get("series") else "."
        return f"{sep}Part{sep}{self.part}"

    def _build_template_context(
        self, media_info: MediaInfo, show_service: bool = True, include_part: bool = True
    ) -> dict:
        """Build template context dictionary from MediaInfo."""
        context = self._build_base_template_context(media_info, show_service)
        context["title"] = self.title.replace("$", "S")
        context["year"] = self.year or ""
        context["season"] = f"S{self.season:02}"
        context["episode"] = f"E{self.number:02}"
        context["season_episode"] = f"S{self.season:02}E{self.number:02}"
        context["episode_name"] = self.name or ""
        context["date"] = ""
        context["part"] = self.part if include_part and self.part is not None else ""
        context["absolute"] = f"{self.absolute:03}" if self.absolute is not None else ""
        if self.air_date:
            # daily/sports: air date replaces SxxExx
            disp = self._air_date_display()
            context["season"] = disp
            context["episode"] = ""
            context["season_episode"] = disp
            context["year"] = ""  # air date is the sole date in the file; folders keep the year
            context["date"] = self.air_date.isoformat() if isinstance(self.air_date, date) else str(self.air_date)
        if include_part and self.part is not None:
            # folded into the identity tokens, not a token of its own: a standalone {part} is
            # absent from every shipped template, so two parts would render the same filename
            suffix = self._part_suffix()
            context["episode"] = f"{context['episode']}{suffix}"
            context["season_episode"] = f"{context['season_episode']}{suffix}"
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

    def _part_label(self) -> str:
        """``.2`` selection-syntax suffix for console output, empty when part-less."""
        return f".{self.part}" if self.part is not None else ""

    def __str__(self) -> str:
        if self.air_date:
            # dated content has no SxxExx to hang the part off, but the parts still have to
            # be told apart, so the same .N suffix follows the date
            return "{title}{year} {date}{part} {name}".format(
                title=self.title,
                year=f" {self.year}" if self.year else "",
                date=self._air_date_display(),
                part=self._part_label(),
                name=self.name or "",
            ).strip()
        # the console shows the -w selection syntax (S01E01.2), not the filename form
        return "{title}{year} S{season:02}E{number:02}{part} {name}".format(
            title=self.title,
            year=f" {self.year}" if self.year else "",
            season=self.season,
            number=self.number,
            part=self._part_label(),
            name=self.name or "",
        ).strip()

    def get_filename(self, media_info: MediaInfo, folder: bool = False, show_service: bool = True) -> str:
        if folder:
            template = config.get_folder_template("series")
            if template:
                # all parts of an episode land in the same season folder
                context = self._build_template_context(media_info, show_service, include_part=False)
                context["season"] = self._folder_season()
                context["year"] = self.year or ""  # folders keep the year
                spacer = detect_spacer(template)  # one style for the whole path
                segments = [
                    TemplateFormatter(seg, spacer).format(context)
                    for seg in re.split(r"[\\/]", template)
                    if seg.strip()
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
                context = self._build_template_context(media_info, show_service, include_part=False)
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
        # part slots before year so existing ties still resolve by year
        super().__init__(iterable, key=lambda x: (x.season, x.number, x.part or 0, x.year or 0))

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
                        ) + episode._part_label()
                        if episode.name:
                            season_tree.add(f"[bold]{label}.[/] [bright_black]{episode.name}")
                        elif episode.air_date:
                            season_tree.add(f"[bright_black]{label}")
                        else:
                            season_tree.add(f"[bright_black]Episode {label}")

        return tree


__all__ = ("Episode", "Series")
