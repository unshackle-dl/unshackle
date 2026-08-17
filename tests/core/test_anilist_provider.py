"""AniList provider: search, ID lookup, title-variant config and derived fields."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from unshackle.core.config import config
from unshackle.core.providers.anilist import AniListProvider, parse_anilist_ref

ONE_PIECE = {
    "id": 21,
    "idMal": 21,
    "title": {"romaji": "ONE PIECE", "english": "One Piece", "native": "ONE PIECE"},
    "format": "TV",
    "startDate": {"year": 1999},
    "countryOfOrigin": "JP",
}

ONE_PIECE_MOVIE = {
    "id": 459,
    "idMal": 459,
    "title": {"romaji": "ONE PIECE (Movie)", "english": "One Piece: The Movie", "native": "ONE PIECE (Movie)"},
    "format": "MOVIE",
    "startDate": {"year": 2000},
    "countryOfOrigin": "JP",
}


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = body

    def json(self) -> dict:
        return self._body


def _provider(monkeypatch: pytest.MonkeyPatch, body: dict) -> tuple[AniListProvider, list[dict]]:
    """An AniListProvider whose every POST answers `body`; returns the sent payloads."""
    provider = AniListProvider()
    sent: list[dict] = []

    def fake_post(url: str, json: Optional[dict] = None, **kwargs: Any) -> FakeResponse:
        sent.append(json or {})
        return FakeResponse(body)

    monkeypatch.setattr(provider, "_session", type("S", (), {"post": staticmethod(fake_post)})())
    return provider, sent


@pytest.fixture(autouse=True)
def default_title_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "anilist_title_language", "english")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (21, ("id", 21)),
        ("21", ("id", 21)),
        (" 21 ", ("id", 21)),
        ("mal:21", ("mal", 21)),
        ("MAL:21", ("mal", 21)),
        ("mal:007", ("mal", 7)),
        ("tt1375666", None),
        ("anilist:21", None),
        ("mal:", None),
        ("", None),
        (0, None),
        ("0", None),
        ("mal:0", None),
        (-3, None),
        ("21.5", None),
        ("²", None),  # superscript two passes isdigit() but crashes int()
    ],
)
def test_parse_anilist_ref(value: Any, expected: Optional[tuple[str, int]]) -> None:
    assert parse_anilist_ref(value) == expected


def test_search_returns_the_best_match(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, sent = _provider(monkeypatch, {"data": {"Page": {"media": [ONE_PIECE, ONE_PIECE_MOVIE]}}})

    result = provider.search("one piece", None, "tv")

    assert result is not None
    assert result.title == "One Piece"
    assert result.year == 1999
    assert result.kind == "tv"
    assert result.original_language == "ja"
    assert result.external_ids.anilist_id == 21
    assert result.source == "anilist"
    assert sent[0]["variables"] == {"search": "one piece"}


def test_search_prefers_the_requested_year(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(monkeypatch, {"data": {"Page": {"media": [ONE_PIECE, ONE_PIECE_MOVIE]}}})

    result = provider.search("one piece", 2000, "movie")

    assert result is not None
    assert result.year == 2000
    assert result.kind == "movie"
    assert result.external_ids.anilist_id == 459


def test_search_prefers_the_requested_kind_without_a_year(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(monkeypatch, {"data": {"Page": {"media": [ONE_PIECE, ONE_PIECE_MOVIE]}}})

    result = provider.search("one piece", None, "movie")

    assert result is not None
    assert result.kind == "movie"
    assert result.external_ids.anilist_id == 459


def test_search_for_tv_is_not_shadowed_by_a_movie(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(monkeypatch, {"data": {"Page": {"media": [ONE_PIECE_MOVIE, ONE_PIECE]}}})

    result = provider.search("one piece", None, "tv")

    assert result is not None
    assert result.kind == "tv"
    assert result.external_ids.anilist_id == 21


def test_search_falls_back_when_no_node_matches_the_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(monkeypatch, {"data": {"Page": {"media": [ONE_PIECE]}}})

    result = provider.search("one piece", None, "movie")

    assert result is not None
    assert result.external_ids.anilist_id == 21


def test_search_of_a_non_anime_title_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(monkeypatch, {"data": {"Page": {"media": []}}})
    assert provider.search("The Walking Dead", None, "tv") is None


def test_graphql_errors_are_a_miss_not_a_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(monkeypatch, {"errors": [{"message": "Not Found."}], "data": {"Media": None}})
    assert provider.search("one piece", None, "tv") is None
    assert provider.get_by_id(21, "tv") is None


def test_get_by_id_queries_the_anilist_id(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, sent = _provider(monkeypatch, {"data": {"Media": ONE_PIECE}})

    result = provider.get_by_id(21, "tv")

    assert result is not None
    assert result.title == "One Piece"
    assert sent[0]["variables"] == {"id": 21}


def test_get_by_id_resolves_a_mal_reference_natively(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, sent = _provider(monkeypatch, {"data": {"Media": ONE_PIECE}})

    result = provider.get_by_id("mal:21", "tv")

    assert result is not None
    assert sent[0]["variables"] == {"idMal": 21}
    assert result.external_ids.anilist_id == 21


def test_get_by_id_rejects_a_foreign_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, sent = _provider(monkeypatch, {"data": {"Media": ONE_PIECE}})
    assert provider.get_by_id("tt1375666", "tv") is None
    assert sent == []


def test_get_external_ids_needs_no_lookup_for_a_bare_id(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, sent = _provider(monkeypatch, {"data": {"Media": ONE_PIECE}})

    assert provider.get_external_ids(21, "tv").anilist_id == 21
    assert sent == []
    assert provider.get_external_ids("mal:21", "tv").anilist_id == 21
    assert sent[0]["variables"] == {"idMal": 21}


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("english", "One Piece"), ("romaji", "ONE PIECE"), ("native", "ONE PIECE"), ("klingon", "One Piece")],
)
def test_title_language_config(monkeypatch: pytest.MonkeyPatch, configured: str, expected: str) -> None:
    monkeypatch.setattr(config, "anilist_title_language", configured)
    provider, _ = _provider(monkeypatch, {"data": {"Media": ONE_PIECE}})

    result = provider.get_by_id(21, "tv")

    assert result is not None
    assert result.title == expected


def test_title_language_falls_back_when_the_variant_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    node = {**ONE_PIECE, "title": {"romaji": "Koisuru ONE PIECE", "english": None, "native": "恋するワンピース"}}
    provider, _ = _provider(monkeypatch, {"data": {"Media": node}})

    result = provider.get_by_id(21, "tv")

    assert result is not None
    assert result.title == "Koisuru ONE PIECE"


def test_a_title_with_no_variant_at_all_is_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    node = {**ONE_PIECE, "title": {"romaji": None, "english": None, "native": None}}
    provider, _ = _provider(monkeypatch, {"data": {"Media": node}})
    assert provider.get_by_id(21, "tv") is None


@pytest.mark.parametrize(
    ("country", "language"),
    [("JP", "ja"), ("KR", "ko"), ("CN", "zh"), ("TW", "zh"), ("US", None), (None, None)],
)
def test_country_of_origin_maps_to_a_language(
    monkeypatch: pytest.MonkeyPatch, country: Optional[str], language: Optional[str]
) -> None:
    provider, _ = _provider(monkeypatch, {"data": {"Media": {**ONE_PIECE, "countryOfOrigin": country}}})

    result = provider.get_by_id(21, "tv")

    assert result is not None
    assert result.original_language == language


@pytest.mark.parametrize(
    ("format_", "kind"), [("MOVIE", "movie"), ("TV", "tv"), ("ONA", "tv"), ("SPECIAL", "tv"), (None, "tv")]
)
def test_format_maps_to_kind(monkeypatch: pytest.MonkeyPatch, format_: Optional[str], kind: str) -> None:
    provider, _ = _provider(monkeypatch, {"data": {"Media": {**ONE_PIECE, "format": format_}}})

    result = provider.get_by_id(21, "tv")

    assert result is not None
    assert result.kind == kind


def test_the_provider_needs_no_api_key() -> None:
    assert AniListProvider().is_available() is True
    assert AniListProvider.REQUIRES_KEY is False
    assert AniListProvider.ID_KIND == "anilist"


def test_a_cached_node_rebuilds_with_the_read_time_title_language(monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw node is cached; which title variant it yields follows the config at read time."""
    from unshackle.core.providers import cached_to_result

    node = {**ONE_PIECE, "title": {"romaji": "WAN PIISU", "english": "One Piece", "native": "ワンピース"}}

    monkeypatch.setattr(config, "anilist_title_language", "native")
    result = cached_to_result(node, "anilist", "tv")

    assert result is not None
    assert result.title == "ワンピース"
    assert result.year == 1999
    assert result.kind == "tv"
    assert result.external_ids.anilist_id == 21
    assert result.original_language == "ja"
