"""Unit tests for the Basic proxy provider: a query it cannot serve returns None so a bare query moves on."""

from __future__ import annotations

import pytest

from unshackle.core.proxies import Basic

pytestmark = pytest.mark.unit


@pytest.fixture
def basic() -> Basic:
    return Basic(us=["http://one.example:8080", "http://two.example:8080"], gb="http://gb.example:8080")


@pytest.mark.parametrize(
    ("query", "expected"), [("us1", "http://one.example:8080"), ("US2", "http://two.example:8080")]
)
def test_numbered_entry(basic: Basic, query: str, expected: str) -> None:
    assert basic.get_proxy(query) == expected


def test_single_entry_region(basic: Basic) -> None:
    assert basic.get_proxy("gb") == "http://gb.example:8080"


@pytest.mark.parametrize("query", ["us-bos", "yul", "us:seattle", "us150", "us0", "ca"])
def test_query_it_cannot_serve_returns_none(basic: Basic, query: str) -> None:
    assert basic.get_proxy(query) is None
