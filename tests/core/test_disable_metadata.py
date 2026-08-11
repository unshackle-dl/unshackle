"""The `disable_metadata` config switch stops automatic lookups but keeps supplied IDs working."""

from __future__ import annotations

from typing import Optional, Union

import pytest

from unshackle.core import providers
from unshackle.core.config import config
from unshackle.core.providers._base import ExternalIds, MetadataProvider, MetadataResult


class FakeProvider(MetadataProvider):
    """Stands in for a real provider; fails the test if it is searched."""

    def __init__(self, name: str, result: Optional[MetadataResult] = None) -> None:
        self.NAME = name
        self._result = result
        self.asked: list[Union[int, str]] = []
        super().__init__()

    def is_available(self) -> bool:
        return True

    def search(self, title: str, year: Optional[int], kind: str) -> Optional[MetadataResult]:
        raise AssertionError("no provider may be searched while disable_metadata is set")

    def get_by_id(self, provider_id: Union[int, str], kind: str) -> Optional[MetadataResult]:
        self.asked.append(provider_id)
        return self._result

    def get_external_ids(self, provider_id: Union[int, str], kind: str) -> ExternalIds:
        return ExternalIds()


@pytest.fixture(autouse=True)
def _disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn metadata off and keep every test off the network."""
    monkeypatch.setattr(config, "disable_metadata", True)
    monkeypatch.setattr(config, "metadata_providers", [])
    monkeypatch.setattr(providers, "fetch_external_ids", lambda *a, **kw: ExternalIds())
    monkeypatch.setattr(providers, "enrich_ids", lambda result: None)


def test_search_metadata_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "get_provider", lambda name: FakeProvider(name))

    assert providers.search_metadata("Futurama", 1999, "tv") is None


def test_supplied_id_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    tmdb = FakeProvider("tmdb", MetadataResult(title="Inception", year=2010, kind="movie"))
    monkeypatch.setattr(providers, "get_provider", lambda name: {"tmdb": tmdb}.get(name))

    result = providers.resolve_by_ids(27205, kind="movie")

    assert result is not None
    assert result.title == "Inception"
    assert result.external_ids.tmdb_id == 27205
    assert tmdb.asked == [27205]


def test_no_ids_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "get_provider", lambda name: FakeProvider(name))

    assert providers.resolve_by_ids(title="Futurama", year=1999, kind="tv") is None
