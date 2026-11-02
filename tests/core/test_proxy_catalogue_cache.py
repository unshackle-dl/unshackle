"""The proxy provider catalogue cache."""

from __future__ import annotations

import pytest

from unshackle.core.proxies import proxy
from unshackle.core.proxies.proxy import cached_catalogue


def test_catalogue_is_fetched_once_until_it_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    @cached_catalogue
    def fetch() -> list[dict]:
        calls.append(1)
        return [{"country": "CA"}]

    assert fetch() == fetch() == [{"country": "CA"}]
    assert len(calls) == 1

    monkeypatch.setattr(proxy, "CATALOGUE_TTL", 0.0)
    fetch()
    assert len(calls) == 2


def test_empty_catalogue_is_not_kept() -> None:
    calls: list[int] = []

    @cached_catalogue
    def fetch() -> list[dict]:
        calls.append(1)
        return []

    fetch()
    fetch()
    assert len(calls) == 2
