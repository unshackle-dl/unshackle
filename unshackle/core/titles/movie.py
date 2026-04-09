import re
from abc import ABC
from typing import Any, Iterable, Optional, Union

from langcodes import Language
from pymediainfo import MediaInfo
from rich.tree import Tree
from sortedcontainers import SortedKeyList

from unshackle.core.config import config
from unshackle.core.titles.title import Title
from unshackle.core.utilities import sanitize_filename
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
        context = self._build_base_template_context(media_info, show_service)
        context["title"] = self.name.replace("$", "S")
        context["year"] = self.year or ""
        return context

    def __str__(self) -> str:
        if self.year:
            return f"{self.name} ({self.year})"
        return self.name

    def get_filename(self, media_info: MediaInfo, folder: bool = False, show_service: bool = True) -> str:
        if folder:
            if config.folder_template:
                formatter = TemplateFormatter(config.folder_template)
                context = self._build_template_context(media_info, show_service)
                folder_name = formatter.format(context)

                separators = re.sub(r"\{[^}]*\}", "", config.folder_template)
                spacer = "." if "." in separators and " " not in separators else " "
                return sanitize_filename(folder_name, spacer)
            name = f"{self.name}"
            if self.year:
                name += f" ({self.year})"
            return sanitize_filename(name, " ")

        formatter = TemplateFormatter(config.output_template["movies"])
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
