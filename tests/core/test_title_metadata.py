"""TitleMetadata: the external-ID lookups and the episode numbering a run applies to its titles."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pytest
from langcodes import Language

from unshackle.core import providers
from unshackle.core.console import console
from unshackle.core.providers import ExternalIds, MetadataResult
from unshackle.core.title_metadata import TitleMetadata
from unshackle.core.title_source import CacheIdentity
from unshackle.core.titles import Episode, Movie, Movies, Series

CACHE = CacheIdentity("cacher", "show-1", "gb", "hash")
CACHE_KWARGS = {
    "title_cacher": "cacher",
    "cache_title_id": "show-1",
    "cache_region": "gb",
    "cache_account_hash": "hash",
}


def ids(tmdb_id: int | None = None, imdb_id: str | None = None) -> dict[str, Any]:
    return {"tmdb_id": tmdb_id, "imdb_id": imdb_id, "tvdb_id": None, "anilist_id": None}


class DummyService:
    pass


def episode(season: int, number: int, **kwargs: Any) -> Episode:
    return Episode(
        id_=f"ep-{season}x{number}", service=DummyService, title="The Show", season=season, number=number, **kwargs
    )


def movie() -> Movie:
    return Movie(id_="movie-1", service=DummyService, name="The Film", year=2001)


class Providers:
    """Records each provider call and answers resolve_by_ids with one result."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, result: MetadataResult | None, tvdb: Any = None) -> None:
        self.result = result
        self.resolved: list[tuple[tuple, dict]] = []
        self.fetched: list[tuple] = []
        monkeypatch.setattr(providers, "resolve_by_ids", self.resolve)
        monkeypatch.setattr(providers, "fetch_external_ids", lambda *a, **k: self.fetched.append((*a, *k.values())))
        monkeypatch.setattr(providers, "get_provider", lambda _name: tvdb)

    def resolve(self, *args: Any, **kwargs: Any) -> MetadataResult | None:
        self.resolved.append((args, kwargs))
        return self.result


class FakeTvdb:
    def __init__(self, mapping: dict, episodes: list | None = None) -> None:
        self.mapping = mapping
        self.episodes = episodes or []
        self.ids: list = []

    def detect_order(self, tvdb_id: int, _keys: list) -> str:
        self.ids.append(tvdb_id)
        return "official"

    def get_order_map(self, tvdb_id: int, order: str, source_order: str | None = None) -> dict:
        return self.mapping

    def get_episodes(self, tvdb_id: int, order: str) -> list:
        return self.episodes


def found(tmdb_id: int | None = 1396, tvdb_id: int | None = None, **kwargs: Any) -> MetadataResult:
    return MetadataResult(external_ids=ExternalIds(tmdb_id=tmdb_id, tvdb_id=tvdb_id), source="tmdb", **kwargs)


def test_enrich_resolves_the_given_ids_and_sets_the_metadata_on_each_title(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Providers(monkeypatch, found(title="Real Name", year=1999, original_language="ko"))
    metadata = TitleMetadata(tmdb_id=5, imdb_id="tt1", cache=CACHE, anime=True)
    titles = metadata.prepare(Series([episode(1, 1), episode(1, 2)]), enrich=True, tvdb_order="", continuous=False)

    assert calls.resolved == [((), {**ids(5, "tt1"), **CACHE_KWARGS, "kind": "tv", "anime": True})]
    assert [(t.title, t.year, str(t.language)) for t in titles] == [
        ("Real Name", 1999, "ko"),
        ("Real Name", 1999, "ko"),
    ]
    assert metadata.enrichment == ("Real Name", 1999, Language.get("ko"))


def test_enrich_fills_absolute_numbers_from_the_resolved_tvdb_id(monkeypatch: pytest.MonkeyPatch) -> None:
    tvdb = FakeTvdb({(1, 1): (1, 1, None)})
    Providers(monkeypatch, found(tvdb_id=73871, title="Show"), tvdb)
    titles = TitleMetadata(tmdb_id=5).prepare(Series([episode(1, 1)]), enrich=True, tvdb_order="", continuous=False)
    assert tvdb.ids == [73871]
    assert [t.absolute for t in titles] == [1]


def test_enrich_with_no_answer_keeps_the_service_values(monkeypatch: pytest.MonkeyPatch) -> None:
    Providers(monkeypatch, None)
    metadata = TitleMetadata(tmdb_id=5)
    titles = metadata.prepare(Movies([movie()]), enrich=True, tvdb_order="", continuous=False)
    assert [(t.name, t.year) for t in titles] == [("The Film", 2001)]
    assert metadata.enrichment == (None, None, None)


def test_no_option_makes_no_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Providers(monkeypatch, found())
    series = Series([episode(1, 1)])
    assert TitleMetadata(tmdb_id=5).prepare(series, enrich=False, tvdb_order="", continuous=False) is series
    assert calls.resolved == []


def test_the_tvdb_order_resolves_a_tvdb_id_and_keeps_it_for_the_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Providers(monkeypatch, found(tvdb_id=73871), FakeTvdb({(1, 1): (2, 1, None), (1, 2): (2, 2, None)}))
    metadata = TitleMetadata(tmdb_id=5, cache=CACHE)
    titles = metadata.prepare(Series([episode(1, 1), episode(1, 2)]), enrich=False, tvdb_order="dvd", continuous=True)

    assert calls.resolved == [((), {**ids(5), **CACHE_KWARGS, "title": "The Show", "year": None, "kind": "tv"})]
    assert metadata.tvdb_id == 73871
    assert [(t.season, t.number) for t in titles] == [(2, 1), (2, 2)]


def test_a_series_is_searched_once_and_each_title_fetches_its_external_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Providers(monkeypatch, found(title="The Show"))
    monkeypatch.setattr(providers, "fuzzy_match", lambda a, b: a == b)
    metadata = TitleMetadata(cache=CACHE)
    metadata.search(episode(1, 1))
    metadata.search(episode(1, 2))

    assert len(calls.resolved) == 1
    assert calls.resolved[0][1]["title"] == "The Show"
    assert (metadata.tmdb_id, metadata.search_source) == (1396, "tmdb")
    assert calls.fetched == [(1396, "tv", *CACHE)] * 2


def test_a_search_that_matches_another_show_sets_no_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Providers(monkeypatch, found(title="Another Show"))
    monkeypatch.setattr(providers, "fuzzy_match", lambda a, b: a == b)
    metadata = TitleMetadata()
    metadata.search(episode(1, 1))
    assert (metadata.tmdb_id, metadata.searched, calls.fetched) == (None, True, [])


def test_a_given_tmdb_id_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    Providers(monkeypatch, found(tmdb_id=1, title="Other"))
    metadata = TitleMetadata(tmdb_id=5)
    metadata.search(episode(1, 1))
    assert metadata.tmdb_id == 5


@pytest.mark.parametrize("source", ["simkl", "imdb"])
def test_a_match_from_a_non_tmdb_source_fetches_no_tmdb_ids(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    calls = Providers(monkeypatch, None)
    metadata = TitleMetadata(tmdb_id=5)
    metadata.searched, metadata.search_source = True, source
    metadata.search(episode(1, 1))
    assert calls.fetched == []


def test_a_movie_is_looked_up_only_for_a_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Providers(monkeypatch, found())
    metadata = TitleMetadata(imdb_id="tt1")
    metadata.search(movie())
    assert calls.resolved == []
    metadata.search(movie(), listing=True)
    assert [kwargs["tmdb_id"] for _, kwargs in calls.resolved] == [None]
    assert metadata.tmdb_id is None


def test_the_hints_take_the_title_before_the_source() -> None:
    anime_title = episode(1, 1)
    anime_title.anime = False
    daily_title = episode(1, 1)
    daily_title.daily = False
    metadata = TitleMetadata(anime=True, daily=True)
    assert (metadata.anime_hint(episode(1, 1)), metadata.anime_hint(anime_title)) == (True, False)
    assert (metadata.daily_hint(episode(1, 1)), metadata.daily_hint(daily_title)) == (True, False)
    assert TitleMetadata(force_daily=True).daily_hint(daily_title) is True


@pytest.mark.parametrize(
    ("result", "level", "message"),
    [
        (None, logging.WARNING, "--enrich found no metadata"),
        (
            found(title="Real Name"),
            logging.WARNING,
            "--enrich source gave no year, language, kept the service's values",
        ),
        (
            found(title="Real", year=1999, original_language="ko"),
            logging.INFO,
            "Original language from metadata: ko",
        ),
    ],
    ids=["nothing", "missing", "language"],
)
def test_enrich_reports_what_the_source_gave(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    result: MetadataResult | None,
    level: int,
    message: str,
) -> None:
    Providers(monkeypatch, result)
    caplog.set_level(logging.INFO, logger="download")
    TitleMetadata(tmdb_id=5).prepare(Movies([movie()]), enrich=True, tvdb_order="", continuous=False)
    assert (level, message) in [(r.levelno, r.getMessage()) for r in caplog.records]


def test_enrich_fills_the_air_dates_of_a_daily_series(monkeypatch: pytest.MonkeyPatch) -> None:
    tvdb = FakeTvdb({}, [{"seasonNumber": 1, "number": 1, "aired": "2026-08-10"}])
    Providers(monkeypatch, found(tvdb_id=73871), tvdb)
    metadata = TitleMetadata(tmdb_id=5, daily=True)
    titles = metadata.prepare(Series([episode(1, 1)]), enrich=True, tvdb_order="", continuous=False)
    assert [t.air_date for t in titles] == [date(2026, 8, 10)]


def test_a_listing_prints_the_search_match(monkeypatch: pytest.MonkeyPatch) -> None:
    Providers(monkeypatch, found(title="The Show"))
    monkeypatch.setattr(providers, "fuzzy_match", lambda a, b: a == b)
    with console.capture() as captured:
        TitleMetadata().search(episode(1, 1), listing=True)
    assert "Search -> The Show (ID 1396)" in captured.get()


def test_a_listing_with_no_movie_match_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    Providers(monkeypatch, None)
    with console.capture() as captured:
        TitleMetadata().search(movie(), listing=True)
    assert "Search -> No match found" in captured.get()


def test_a_movie_fetches_the_movie_external_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Providers(monkeypatch, None)
    TitleMetadata(tmdb_id=5, cache=CACHE).search(movie())
    assert calls.fetched == [(5, "movie", *CACHE)]
