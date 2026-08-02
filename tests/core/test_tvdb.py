"""TVDB provider ordering and season-order remapping."""

from __future__ import annotations

import pytest

from unshackle.core import providers
from unshackle.core.config import config
from unshackle.core.providers.tvdb import TVDBProvider, _ids_from_remote, _pick_match
from unshackle.core.utils.tags import _build_tags_from_ids

REMOTE_IDS = [
    {"id": "tt0149460", "type": 2, "sourceName": "IMDB"},
    {"id": "http://example.com", "type": 4, "sourceName": "Official Website"},
    {"id": "615", "type": 12, "sourceName": "TheMovieDB.com"},
]

# Futurama: aired S1 stops at E9, the four held-back episodes open aired S2.
# DVD order folds them back into S1 as E10-E13.
AIRED = [
    {"id": 1, "seasonNumber": 1, "number": 9, "name": "Hell Is Other Robots"},
    {"id": 2, "seasonNumber": 2, "number": 1, "name": "A Flight to Remember"},
    {"id": 3, "seasonNumber": 2, "number": 2, "name": "Mars University"},
    {"id": 99, "seasonNumber": 2, "number": 3, "name": "Only In Aired"},
]
DVD = [
    {"id": 1, "seasonNumber": 1, "number": 9, "name": "Hell Is Other Robots"},
    {"id": 2, "seasonNumber": 1, "number": 10, "name": "A Flight to Remember"},
    {"id": 3, "seasonNumber": 1, "number": 11, "name": "Mars University"},
]


def test_ids_from_remote() -> None:
    ext = _ids_from_remote(REMOTE_IDS, 73871)
    assert ext.imdb_id == "tt0149460"
    assert ext.tmdb_id == 615
    assert ext.tvdb_id == 73871


def _stub(p: TVDBProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(p, "get_episodes", lambda _id, order: {"official": AIRED, "dvd": DVD}.get(order, []))


def test_order_map_joins_on_episode_id(monkeypatch: pytest.MonkeyPatch) -> None:
    p = TVDBProvider()
    _stub(p, monkeypatch)

    mapping = p.get_order_map(73871, "dvd")
    assert mapping[(1, 9)] == (1, 9, "Hell Is Other Robots")
    assert mapping[(2, 1)] == (1, 10, "A Flight to Remember")
    assert mapping[(2, 2)] == (1, 11, "Mars University")
    # an episode absent from the target order is left out, so it keeps its own numbering
    assert (2, 3) not in mapping


def test_order_map_reverses_with_source_order(monkeypatch: pytest.MonkeyPatch) -> None:
    p = TVDBProvider()
    _stub(p, monkeypatch)

    mapping = p.get_order_map(73871, "official", source_order="dvd")
    assert mapping[(1, 10)] == (2, 1, "A Flight to Remember")


def test_order_map_is_noop_when_orders_match() -> None:
    assert TVDBProvider().get_order_map(73871, "official") == {}


def test_order_map_empty_when_order_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    p = TVDBProvider()
    _stub(p, monkeypatch)
    assert p.get_order_map(73871, "alternate") == {}


def test_detect_order_picks_the_order_the_numbering_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    p = TVDBProvider()
    _stub(p, monkeypatch)
    # a service may number a series in a non-aired order, so detection must not assume aired
    assert p.detect_order(73871, [(1, 10), (1, 11)]) == "dvd"
    assert p.detect_order(73871, [(2, 1), (2, 2), (2, 3)]) == "official"


def test_detect_order_falls_back_to_aired(monkeypatch: pytest.MonkeyPatch) -> None:
    p = TVDBProvider()
    monkeypatch.setattr(p, "get_episodes", lambda _id, order: [])
    assert p.detect_order(73871, [(1, 1)]) == "official"


# TVDB holds both Battlestar Galactica series; the 2003 revival is filed under year 2005
BSG = [
    {"name": "Battlestar Galactica", "year": "1978", "tvdb_id": "71173"},
    {"name": "Battlestar Galactica (2003)", "year": "2005", "tvdb_id": "73545"},
]


def test_pick_match_prefers_the_right_era() -> None:
    # exact title match on the 1978 series must not beat the era the caller asked for
    best, _, _ = _pick_match(BSG, "Battlestar Galactica", 2004)
    assert best["tvdb_id"] == "73545"
    best, _, _ = _pick_match(BSG, "Battlestar Galactica", 1978)
    assert best["tvdb_id"] == "71173"


def test_pick_match_without_a_year_scores_on_title_alone() -> None:
    best, _, _ = _pick_match(BSG, "Battlestar Galactica", None)
    assert best["tvdb_id"] == "71173"


def test_pick_match_returns_nothing_when_no_year_fits() -> None:
    assert _pick_match(BSG, "Battlestar Galactica", 1995)[0] is None


def test_tvdb2_tag_follows_the_matroska_spec() -> None:
    # TVDB2 IDs are prefixed by entity type: matroska.org/technical/tagging.html
    from unshackle.core.providers import ExternalIds

    assert _build_tags_from_ids(ExternalIds(tvdb_id=73871), "tv")["TVDB2"] == "series/73871"
    assert _build_tags_from_ids(ExternalIds(tvdb_id=113), "movie")["TVDB2"] == "movies/113"


def test_provider_order_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "metadata_providers", [])
    assert [c.NAME for c in providers.provider_order()] == list(providers.DEFAULT_ORDER)


def test_provider_order_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "metadata_providers", ["TVDB", "tmdb", "nope", "tvdb"])
    # unknown entries dropped, duplicates collapsed, case ignored, order kept
    assert [c.NAME for c in providers.provider_order()] == ["tvdb", "tmdb"]


def test_enrichment_authority_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    # search order must not demote TMDB below SIMKL for cross-validation tie-breaks
    monkeypatch.setattr(config, "metadata_providers", [])
    assert providers._enrichment_providers() == ["tmdb", "simkl", "tvdb"]
    # `metadata_providers` filters the set but does not re-rank the trust order
    monkeypatch.setattr(config, "metadata_providers", ["tvdb", "simkl"])
    assert providers._enrichment_providers() == ["simkl", "tvdb"]


def test_cached_tvdb_result_keeps_enriched_ids() -> None:
    cached = {
        "tvdb_id": "73871",
        "name": "Futurama",
        "year": "1999",
        "_enriched_ids": {"imdb_id": "tt0149460", "tmdb_id": 615},
    }
    result = providers._cached_to_result(cached, "tvdb", "tv")
    assert result is not None
    assert result.external_ids.tvdb_id == 73871
    assert result.external_ids.imdb_id == "tt0149460"
    assert result.external_ids.tmdb_id == 615
    # remote_ids stay authoritative over the enriched copies
    cached["remote_ids"] = REMOTE_IDS
    cached["_enriched_ids"] = {"imdb_id": "tt9999999", "tmdb_id": 1}
    result = providers._cached_to_result(cached, "tvdb", "tv")
    assert result.external_ids.imdb_id == "tt0149460"
    assert result.external_ids.tmdb_id == 615


def test_get_episodes_fails_closed_on_a_truncated_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    p = TVDBProvider()
    pages: dict[int, object] = {0: {"episodes": [{"id": n} for n in range(500)]}, 1: None}
    monkeypatch.setattr(p, "_get", lambda _path, params: pages[params["page"]])
    # page 1 fails mid-listing: a partial list would renumber wrongly
    assert p.get_episodes(73871, "official") == []
    assert p._episodes == {}


def test_get_episodes_does_not_cache_an_empty_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    p = TVDBProvider()
    monkeypatch.setattr(p, "_get", lambda _path, params: None)
    assert p.get_episodes(73871, "official") == []
    assert p._episodes == {}
