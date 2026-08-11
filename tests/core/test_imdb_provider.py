"""IMDb GraphQL provider: persisted-query flow, field mapping, and search constraints."""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from unshackle.core.providers.imdb import IMDBProvider, escape_graphql


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _node(
    imdb_id: str = "tt3581920",
    text: str = "The Last of Us",
    year: Optional[int] = 2023,
    title_type: str = "tvSeries",
    languages: Any = ({"id": "en", "text": "English"}, {"id": "id", "text": "Indonesian"}),
) -> dict:
    return {
        "id": imdb_id,
        "titleText": {"text": text},
        "originalTitleText": {"text": text},
        "releaseYear": {"year": year} if year else None,
        "titleType": {"id": title_type},
        "spokenLanguages": {"spokenLanguages": list(languages)} if languages is not None else None,
    }


class _FakeSession:
    """Records GET/POST calls and replays queued payloads."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = responses
        self.gets: list[dict] = []
        self.posts: list[dict] = []

    def get(self, url: str, params: dict, headers: dict, timeout: int) -> _Response:
        self.gets.append(params)
        return _Response(self._responses.pop(0))

    def post(self, url: str, json: dict, headers: dict, timeout: int) -> _Response:  # noqa: A002
        self.posts.append(json)
        return _Response(self._responses.pop(0))


def _provider(monkeypatch: pytest.MonkeyPatch, responses: list[dict]) -> tuple[IMDBProvider, _FakeSession]:
    p = IMDBProvider()
    session = _FakeSession(responses)
    monkeypatch.setattr(type(p), "session", property(lambda _self: session))
    return p, session


NOT_FOUND = {"errors": [{"message": "PersistedQueryNotFound", "extensions": {"code": "PERSISTED_QUERY_NOT_FOUND"}}]}


def test_is_available_without_any_key() -> None:
    assert IMDBProvider().is_available() is True


def test_cache_hit_uses_one_get(monkeypatch: pytest.MonkeyPatch) -> None:
    p, session = _provider(monkeypatch, [{"data": {"title": _node()}}])
    result = p.get_by_id("tt3581920", "tv")
    assert result is not None and result.title == "The Last of Us"
    assert len(session.gets) == 1 and session.posts == []


def test_persisted_query_miss_falls_back_to_one_post(monkeypatch: pytest.MonkeyPatch) -> None:
    p, session = _provider(monkeypatch, [NOT_FOUND, {"data": {"title": _node()}}])
    result = p.get_by_id("tt3581920", "tv")
    assert result is not None and result.external_ids.imdb_id == "tt3581920"
    assert len(session.gets) == 1 and len(session.posts) == 1
    # the POST registers the query by sending its text, keyed by the same hash the GET sent
    assert "query UnshackleTitle" in session.posts[0]["query"]
    sent_hash = json.loads(session.gets[0]["extensions"])["persistedQuery"]["sha256Hash"]
    assert session.posts[0]["extensions"]["persistedQuery"]["sha256Hash"] == sent_hash


def test_get_params_carry_no_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """IMDb's GraphQL gateway decodes "+" literally, so spaced JSON would come back as a 400."""
    p, session = _provider(monkeypatch, [{"data": {"title": _node()}}])
    p.get_by_id("tt3581920", "tv")
    assert " " not in session.gets[0]["extensions"]
    assert " " not in session.gets[0]["variables"]


def test_get_by_id_maps_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    p, _ = _provider(monkeypatch, [{"data": {"title": _node()}}])
    result = p.get_by_id("tt3581920", "tv")
    assert result is not None
    assert (result.title, result.year, result.kind) == ("The Last of Us", 2023, "tv")
    assert result.original_language == "en"
    assert result.source == "imdb"
    assert result.external_ids.imdb_id == "tt3581920"


@pytest.mark.parametrize("title_type", ["movie", "tvSeries", "videoGame"])
def test_caller_kind_wins_over_the_response_title_type(monkeypatch: pytest.MonkeyPatch, title_type: str) -> None:
    """kind stays the caller's, so it cannot desync from the tmdb_kind resolve_by_ids seeds."""
    p, _ = _provider(monkeypatch, [{"data": {"title": _node(title_type=title_type)}}])
    result = p.get_by_id("tt1375666", "tv")
    assert result is not None and result.kind == "tv"


@pytest.mark.parametrize("payload", [{"data": {"title": None}}, {"errors": [{"message": "boom"}]}, {"data": {}}])
def test_get_by_id_returns_none_on_no_answer(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    p, _ = _provider(monkeypatch, [payload])
    assert p.get_by_id("tt3581920", "tv") is None


def _edges(*nodes: dict) -> dict:
    return {"data": {"advancedTitleSearch": {"edges": [{"node": {"title": n}} for n in nodes]}}}


def test_search_uses_its_own_operation_and_one_get_on_a_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    p, session = _provider(monkeypatch, [_edges(_node())])
    result = p.search("The Last of Us", 2023, "tv")
    assert result is not None and result.external_ids.imdb_id == "tt3581920"
    assert session.gets[0]["operationName"] == "UnshackleTitleSearch"
    assert session.posts == []


def test_search_query_text_carries_the_constraints(monkeypatch: pytest.MonkeyPatch) -> None:
    p, session = _provider(monkeypatch, [NOT_FOUND, _edges(_node())])
    p.search("The Last of Us", 2023, "tv")
    query = session.posts[0]["query"]
    assert 'searchTerm: "The Last of Us"' in query
    assert 'start: "2023-01-01", end: "2023-12-31"' in query
    assert '["tvSeries", "tvMiniSeries"]' in query or '["tvSeries","tvMiniSeries"]' in query


def test_search_omits_the_year_range_when_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    p, session = _provider(monkeypatch, [NOT_FOUND, _edges(_node(title_type="movie"))])
    p.search("Inception", None, "movie")
    query = session.posts[0]["query"]
    assert "releaseDateConstraint" not in query
    assert '["movie", "tvMovie"]' in query or '["movie","tvMovie"]' in query


def test_search_picks_the_closest_title_not_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    noise = _node(imdb_id="tt27478132", text="Making of: The Last of Us")
    exact = _node()
    p, _ = _provider(monkeypatch, [_edges(noise, exact)])
    result = p.search("The Last of Us", 2023, "tv")
    assert result is not None and result.external_ids.imdb_id == "tt3581920"


def test_search_rejects_a_title_that_is_not_a_close_match(monkeypatch: pytest.MonkeyPatch) -> None:
    p, _ = _provider(monkeypatch, [_edges(_node(imdb_id="tt999", text="Something Else Entirely"))])
    assert p.search("The Last of Us", 2023, "tv") is None


def test_search_survives_a_null_language_block(monkeypatch: pytest.MonkeyPatch) -> None:
    p, _ = _provider(monkeypatch, [_edges(_node(languages=None))])
    result = p.search("The Last of Us", 2023, "tv")
    assert result is not None and result.original_language is None


def test_search_returns_none_when_nothing_comes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    p, _ = _provider(monkeypatch, [_edges()])
    assert p.search("The Last of Us", 2023, "tv") is None


def test_escape_graphql_closes_the_string_injection() -> None:
    assert escape_graphql('a"b\\c') == 'a\\"b\\\\c'


def test_search_term_with_a_quote_stays_inside_the_string(monkeypatch: pytest.MonkeyPatch) -> None:
    p, session = _provider(monkeypatch, [NOT_FOUND, _edges(_node(text='The "Best" Show'))])
    p.search('The "Best" Show', None, "tv")
    assert 'searchTerm: "The \\"Best\\" Show"' in session.posts[0]["query"]


def test_get_external_ids_echoes_the_id() -> None:
    assert IMDBProvider().get_external_ids("tt1375666", "movie").imdb_id == "tt1375666"
