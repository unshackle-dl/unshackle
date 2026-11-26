"""The external IDs of a download's titles and the episode numbering the metadata providers give them."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import groupby
from operator import attrgetter
from typing import Any, NamedTuple

from langcodes import Language, tag_is_valid
from rich.padding import Padding

from unshackle.core import providers
from unshackle.core.console import console
from unshackle.core.providers import MetadataResult
from unshackle.core.providers.tvdb import parse_int
from unshackle.core.title_source import CacheIdentity
from unshackle.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from unshackle.core.utilities import apply_original_language


def parse_language(tag: str | None) -> Language | None:
    """A provider's original-language tag as a Language, or None if it is absent or not a real tag."""
    tag = (tag or "").strip()
    if not tag or not tag_is_valid(tag) or tag.startswith("und"):
        return None
    return Language.get(tag)


def apply_enrich(title: Title_T, name: str | None, year: int | None, language: Language | None) -> None:
    """Set the --enrich name, year and original language on a title. A song takes only the language.

    A service can set the title again in get_tracks, so dl applies them after that call too.
    """
    if isinstance(title, Episode):
        title.title = name or title.title
    elif isinstance(title, Movie):
        title.name = name or title.name
    if isinstance(title, (Episode, Movie)):
        title.year = year or title.year
    if language:
        apply_original_language(title, language)


class Enrichment(NamedTuple):
    """The --enrich name, year and original language; None where the metadata source gave none."""

    name: str | None = None
    year: int | None = None
    language: Language | None = None


@dataclass
class TitleMetadata:
    """The external IDs of one run, and the metadata lookups that resolve them and renumber its episodes.

    The IDs start as the ones the user gave. ``prepare`` and ``search`` add the IDs that the lookups resolve,
    and tagging and post-scripts read them as they stand.
    """

    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None
    anilist_id: int | str | None = None
    cache: CacheIdentity = CacheIdentity(None, None, None, None)
    service: str | None = None
    anime: bool = False
    daily: bool = False
    force_daily: bool = False
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("download"))
    enrichment: Enrichment = field(default=Enrichment(), init=False)
    searched: bool = field(default=False, init=False)
    search_source: str | None = field(default=None, init=False)

    def post_script_ids(self) -> dict[str, Any]:
        """Tagging IDs as they stand right now.

        Read at each post-script call, never snapshotted: the search inside the title loop
        finds an Episode's TMDB ID, so a snapshot taken before it is always empty.
        """
        return {"tmdb": self.tmdb_id, "imdb": self.imdb_id, "tvdb": self.tvdb_id}

    def anime_hint(self, title: Title_T | None = None) -> bool:
        """Whether metadata lookups for this title should prefer AniList."""
        per_title = getattr(title, "anime", None)
        return self.anime if per_title is None else bool(per_title)

    def daily_hint(self, title: Title_T | None = None) -> bool:
        """Whether this title is daily content named by air date."""
        per_title = getattr(title, "daily", None)
        return self.force_daily or (self.daily if per_title is None else bool(per_title))

    def cache_kwargs(self) -> dict[str, Any]:
        """The cache identity of this run as the cache keywords of the metadata lookups."""
        return {
            "title_cacher": self.cache.cacher,
            "cache_title_id": self.cache.title_id,
            "cache_region": self.cache.region,
            "cache_account_hash": self.cache.account_hash,
        }

    def resolve(self, **kwargs: Any) -> MetadataResult | None:
        """``providers.resolve_by_ids`` with the IDs and the cache of this run; a keyword overrides either."""
        ids = {"tmdb_id": self.tmdb_id, "imdb_id": self.imdb_id, "tvdb_id": self.tvdb_id, "anilist_id": self.anilist_id}
        return providers.resolve_by_ids(**{**ids, **self.cache_kwargs(), **kwargs})

    def prepare(self, titles: Titles_T, *, enrich: bool, tvdb_order: str, continuous: bool) -> Titles_T:
        """Apply --enrich, then --tvdb-order, then --continuous-episodes, and return the titles."""
        if enrich:
            self.enrich(titles)
        if tvdb_order and isinstance(titles, Series):
            titles = self.apply_tvdb_order(titles, tvdb_order)
        if continuous and isinstance(titles, Series):
            titles = self.apply_continuous_episodes(titles)
        return titles

    def enrich(self, titles: Titles_T) -> None:
        """Set the provider's name, year and original language on each title, then fill a series from TVDB."""
        sample_title = titles[0] if hasattr(titles, "__getitem__") else titles
        kind = "tv" if isinstance(sample_title, Episode) else "movie"

        result = self.resolve(kind=kind, anime=self.anime_hint(sample_title))
        self.enrichment = (
            Enrichment(result.title, result.year, parse_language(result.original_language)) if result else Enrichment()
        )
        name, year, language = self.enrichment

        if language:
            self.log.info(f"Original language from metadata: {language}")
        if not (name or year or language):
            self.log.warning("--enrich found no metadata")
        elif missing := [
            label for label, value in (("title", name), ("year", year), ("language", language)) if not value
        ]:
            self.log.warning(f"--enrich source gave no {', '.join(missing)}, kept the service's values")

        if isinstance(titles, (Series, Movies)):
            for t in titles:
                apply_enrich(t, name, year, language)

        if isinstance(titles, Series):
            tvdb_id = self.tvdb_id or (result.external_ids.tvdb_id if result else None)
            self.fill_absolute_numbers(titles, tvdb_id)
            if any(self.daily_hint(t) for t in titles):
                self.fill_air_dates(titles, tvdb_id)

    def search(self, title: Title_T, listing: bool = False) -> None:
        """Find the TMDB ID of a series once, and fetch the external IDs of each title.

        ``listing`` prints the match, and looks a movie up as well.
        """
        if isinstance(title, Episode) and not self.searched:
            tmdb_title: str | None = None
            has_ids = bool(self.tmdb_id or self.imdb_id or self.tvdb_id or self.anilist_id)
            result = self.resolve(
                title=title.title,
                year=title.year,
                kind="tv",
                anime=self.anime_hint(title),
            )
            if result:
                if has_ids:
                    tmdb_title = result.title
                    if not self.tmdb_id:
                        self.tmdb_id = result.external_ids.tmdb_id
                elif result.title and providers.fuzzy_match(result.title, title.title):
                    self.tmdb_id = result.external_ids.tmdb_id
                    tmdb_title = result.title
                    self.search_source = result.source
            if listing:
                if self.tmdb_id:
                    console.print(Padding(f"Search -> {tmdb_title or '?'} [bright_black](ID {self.tmdb_id})", (0, 5)))
                else:
                    console.print(Padding("Search -> [bright_black]No match found[/]", (0, 5)))
            self.searched = True

        if isinstance(title, Movie) and listing and not self.tmdb_id:
            movie_result = self.resolve(
                tmdb_id=None,
                title=title.name,
                year=title.year,
                kind="movie",
                anime=self.anime_hint(title),
            )
            if movie_result and movie_result.external_ids.tmdb_id:
                console.print(
                    Padding(
                        f"Search -> {movie_result.title or '?'} [bright_black](ID {movie_result.external_ids.tmdb_id})",
                        (0, 5),
                    )
                )
            else:
                console.print(Padding("Search -> [bright_black]No match found[/]", (0, 5)))

        if self.tmdb_id and self.search_source not in ("simkl", "imdb"):
            providers.fetch_external_ids(
                self.tmdb_id,
                "tv" if isinstance(title, Episode) else "movie",
                **self.cache_kwargs(),
            )

    def fill_air_dates(self, titles: Series, tvdb_id: int | None = None) -> None:
        """Fill in missing episode air dates from TVDB.

        Additive only: an air date the service already set is kept.
        """
        tvdb = providers.get_provider("tvdb")
        if not tvdb:
            self.log.debug("Air dates need a tvdb_api_key in your config, skipping.")
            return

        if not tvdb_id:
            self.log.debug("Could not resolve a TVDB ID, skipping air dates.")
            return

        wanted = [t for t in titles if self.daily_hint(t)]
        if not wanted:
            return

        keys = list(dict.fromkeys((t.season, t.number) for t in titles))
        source_order = tvdb.detect_order(tvdb_id, keys)  # type: ignore[attr-defined]
        episodes = tvdb.get_episodes(tvdb_id, source_order)  # type: ignore[attr-defined]
        if not episodes:
            self.log.debug("TVDB listed no episodes for series %s, skipping air dates.", tvdb_id)
            return

        aired_by_key: dict[tuple[int, int], str] = {}
        for episode in episodes:
            season, number = parse_int(episode.get("seasonNumber")), parse_int(episode.get("number"))
            aired = episode.get("aired")
            if season is None or number is None or not aired:
                continue
            aired_by_key[(season, number)] = str(aired)

        # TVDB carries placeholder schedule dates for unaired episodes. Stamping one would
        # name the file with a date the episode never aired on.
        limit = date.today() + timedelta(days=1)

        filled = 0
        for title in wanted:
            if title.air_date is not None:
                continue
            aired = aired_by_key.get((title.season, title.number))
            if not aired:
                continue
            try:
                air_date = date.fromisoformat(aired[:10])
            except ValueError:
                continue
            if air_date.year < 1970 or air_date > limit:
                continue
            title.air_date = air_date
            filled += 1

        if filled:
            self.log.info("Filled air dates for %d episode(s) from TVDB (ID %s).", filled, tvdb_id)

    def fill_absolute_numbers(self, titles: Series, tvdb_id: int | None = None) -> None:
        """Fill in missing absolute episode numbers from TVDB's absolute order.

        Additive only: season and number are never written, and an absolute number the
        service already set is kept.
        """
        tvdb = providers.get_provider("tvdb")
        if not tvdb:
            self.log.debug("Absolute episode numbers need a tvdb_api_key in your config, skipping.")
            return

        if not tvdb_id:
            self.log.debug("Could not resolve a TVDB ID, skipping absolute episode numbers.")
            return

        keys = list(dict.fromkeys((t.season, t.number) for t in titles))
        source_order = tvdb.detect_order(tvdb_id, keys)  # type: ignore[attr-defined]
        if source_order == "absolute":
            # the service already numbers in absolute order; get_order_map would return the
            # empty same-order map, but here the episode numbers are their own fill
            mapping = {key: (*key, None) for key in keys}
        else:
            mapping = tvdb.get_order_map(tvdb_id, "absolute", source_order)  # type: ignore[attr-defined]
        if not mapping:
            self.log.debug("TVDB has no absolute order for series %s, skipping absolute episode numbers.", tvdb_id)
            return

        filled = 0
        for title in titles:
            if title.absolute is not None:
                continue
            entry = mapping.get((title.season, title.number))
            if not entry:
                continue
            title.absolute = entry[1]
            filled += 1

        if filled:
            self.log.info("Filled absolute numbers for %d episode(s) from TVDB (ID %s).", filled, tvdb_id)

    def apply_continuous_episodes(self, titles: Series) -> Series:
        """Number each season on from the last episode of the season before it.

        Season 0 and episode 0 keep their numbers, and a season that already starts past the season before it is kept.
        The Series is rebuilt, as it does not re-sort an episode whose number changed.
        """
        last = 0
        renumbered = 0
        for season, group in groupby(titles, key=attrgetter("season")):
            episodes = [t for t in group if t.number]
            if not season or not episodes:
                continue
            if min(t.number for t in episodes) <= last:
                for t in episodes:
                    t.number += last
                renumbered += len(episodes)
            last = max(t.number for t in episodes)
        self.log.info("Numbered %d episode(s) on from the season before.", renumbered)
        return type(titles)(list(titles))

    def apply_tvdb_order(self, titles: Series, order: str) -> Series:
        """Renumber episodes from the service's own numbering into a TVDB season order."""
        tvdb = providers.get_provider("tvdb")
        if not tvdb:
            self.log.warning("--tvdb-order needs a tvdb_api_key in your config, skipping renumber.")
            return titles

        tvdb_id = self.tvdb_id
        if not tvdb_id:
            show = titles[0]
            result = self.resolve(tvdb_id=None, anilist_id=None, title=show.title, year=show.year, kind="tv")
            tvdb_id = result.external_ids.tvdb_id if result else None
        if not tvdb_id:
            self.log.warning("Could not resolve a TVDB ID for %r, skipping %s renumber.", str(titles), order)
            return titles
        self.tvdb_id = tvdb_id  # keep the mux tag on the series the renumbering used

        # parts of one episode share a (season, number) slot; without the dedupe the
        # collision check below reads them as several episodes clashing on one slot
        keys = list(dict.fromkeys((t.season, t.number) for t in titles))
        source_order = tvdb.detect_order(tvdb_id, keys)  # type: ignore[attr-defined]
        if source_order == order:
            self.log.info("%s already lists this series in TVDB %s order.", self.service, order)
            return titles

        mapping = tvdb.get_order_map(tvdb_id, order, source_order)  # type: ignore[attr-defined]
        if not mapping:
            return titles

        # an unmapped episode keeps its own numbering and can land on a remapped episode's slot
        renumbered = [mapping.get(key, (*key, None))[:2] for key in keys]
        unmapped = sum(1 for key in keys if key not in mapping)
        collisions = [slot for slot, count in Counter(renumbered).items() if count > 1]
        if collisions:
            self.log.error(
                "TVDB's %s order misses %d episode(s) and puts two in %d slot(s), keeping the %s numbering of %s",
                order,
                unmapped,
                len(collisions),
                source_order,
                self.service,
            )
            return titles

        for title in titles:
            entry = mapping.get((title.season, title.number))
            if not entry:
                continue
            title.season, title.number, tvdb_name = entry
            if tvdb_name and not title.name:
                title.name = tvdb_name

        if unmapped:
            self.log.warning(
                "%d episode(s) are not in TVDB's %s order and keep their %s numbering.",
                unmapped,
                order,
                source_order,
            )
        self.log.info("Renumbered episodes from TVDB %s to %s order (ID %s).", source_order, order, tvdb_id)

        # Series is a SortedKeyList; renumbering in place does not re-sort it
        return type(titles)(list(titles))
