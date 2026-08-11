"""Precedence of user-supplied external IDs in providers.resolve_by_ids."""

from __future__ import annotations

from typing import Optional, Union

import pytest

from unshackle.core import providers
from unshackle.core.config import config
from unshackle.core.providers._base import ExternalIds, MetadataProvider, MetadataResult


class FakeProvider(MetadataProvider):
    """Stands in for a real provider; records the ID it was asked for."""

    def __init__(self, name: str, result: Optional[MetadataResult] = None, raises: bool = False) -> None:
        self.NAME = name
        self._result = result
        self._raises = raises
        self.asked: list[Union[int, str]] = []
        super().__init__()

    def is_available(self) -> bool:
        return True

    def search(self, title: str, year: Optional[int], kind: str) -> Optional[MetadataResult]:
        raise AssertionError("resolve_by_ids must not search when an ID was supplied")

    def get_by_id(self, provider_id: Union[int, str], kind: str) -> Optional[MetadataResult]:
        self.asked.append(provider_id)
        if self._raises:
            raise ValueError("provider is down")
        return self._result

    def get_external_ids(self, provider_id: Union[int, str], kind: str) -> ExternalIds:
        return ExternalIds()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the network and on the default provider order."""
    monkeypatch.setattr(config, "metadata_providers", [])
    monkeypatch.setattr(providers, "fetch_external_ids", lambda *a, **kw: ExternalIds())
    monkeypatch.setattr(providers, "enrich_ids", lambda result: None)
    monkeypatch.setattr(
        providers,
        "search_metadata",
        lambda *a, **kw: pytest.fail("search_metadata must not run when an ID was supplied"),
    )


def _patch_providers(monkeypatch: pytest.MonkeyPatch, available: dict[str, FakeProvider]) -> None:
    monkeypatch.setattr(providers, "get_provider", lambda name: available.get(name))


def test_supplied_ids_survive_a_providers_own_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    # tmdb answers with a different imdb id; the one the user gave must win
    tmdb = FakeProvider(
        "tmdb",
        MetadataResult(title="Inception", year=2010, kind="movie", external_ids=ExternalIds(imdb_id="tt9999999")),
    )
    _patch_providers(monkeypatch, {"tmdb": tmdb})

    result = providers.resolve_by_ids(27205, "tt1375666", kind="movie")

    assert result is not None
    assert result.external_ids.tmdb_id == 27205
    assert result.external_ids.tmdb_kind == "movie"
    assert result.external_ids.imdb_id == "tt1375666"


def test_imdb_falls_through_to_omdb(monkeypatch: pytest.MonkeyPatch) -> None:
    imdb = FakeProvider("imdb", raises=True)
    omdb = FakeProvider("omdb", MetadataResult(title="Inception", year=2010, kind="movie"))
    _patch_providers(monkeypatch, {"imdb": imdb, "omdb": omdb})

    result = providers.resolve_by_ids(imdb_id="tt1375666", kind="movie")

    assert result is not None
    assert result.title == "Inception"
    assert imdb.asked == ["tt1375666"]
    assert omdb.asked == ["tt1375666"]


def test_all_providers_down_synthesizes_a_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_providers(monkeypatch, {})

    result = providers.resolve_by_ids(tvdb_id=73871, title="Futurama", year=1999, kind="tv")

    assert result is not None
    assert result.title == "Futurama"
    assert result.year == 1999
    assert result.external_ids.tvdb_id == 73871


def test_no_ids_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(providers, "search_metadata", lambda *a, **kw: calls.append(a) or None)
    _patch_providers(monkeypatch, {})

    providers.resolve_by_ids(title="Futurama", year=1999, kind="tv")

    assert calls and calls[0][:3] == ("Futurama", 1999, "tv")


def test_no_ids_and_no_title_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_providers(monkeypatch, {})
    assert providers.resolve_by_ids(kind="tv") is None


def test_per_kind_order_puts_tvdb_first_for_tv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "metadata_providers", {"tv": ["tvdb", "tmdb", "imdb", "omdb"]})
    tvdb = FakeProvider("tvdb", MetadataResult(title="Futurama", year=1999, kind="tv"))
    tmdb = FakeProvider("tmdb", MetadataResult(title="Wrong Show", year=2020, kind="tv"))
    _patch_providers(monkeypatch, {"tvdb": tvdb, "tmdb": tmdb})

    result = providers.resolve_by_ids(615, tvdb_id=73871, kind="tv")

    assert result is not None
    assert result.title == "Futurama"
    assert tmdb.asked == []
    # movies keep the default order, so tmdb is consulted before tvdb there
    assert [cls.NAME for cls in providers.provider_order("movie")] == list(providers.DEFAULT_ORDER)


def test_flat_list_config_applies_to_both_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "metadata_providers", ["tvdb", "tmdb"])
    assert [cls.NAME for cls in providers.provider_order("tv")] == ["tvdb", "tmdb"]
    assert [cls.NAME for cls in providers.provider_order("movie")] == ["tvdb", "tmdb"]
    assert [cls.NAME for cls in providers.provider_order()] == ["tvdb", "tmdb"]
