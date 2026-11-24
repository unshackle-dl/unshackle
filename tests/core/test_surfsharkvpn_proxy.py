"""Unit tests for the SurfsharkVPN proxy provider against the current cluster API shape."""

from __future__ import annotations

import logging

import pytest

from unshackle.core.proxies.surfsharkvpn import SurfsharkVPN

pytestmark = pytest.mark.unit

USERNAME = "a" * 24
PASSWORD = "b" * 24
CLUSTERS = [
    {"id": "4c333aa2", "countryCode": "US", "connectionName": "us-sea.prod.surfshark.com", "location": "Seattle"},
    {"id": "f8aa8d00", "countryCode": "US", "connectionName": "us-dal.prod.surfshark.com", "location": "Dallas"},
    {
        "id": "1b2c3d4e",
        "countryCode": "US",
        "connectionName": "us-nyc-st002.prod.surfshark.com",
        "location": "New York",
    },
    {"id": "9794c127", "countryCode": "GB", "connectionName": "uk-lon.prod.surfshark.com", "location": "London"},
    {
        "id": "5e6f7a8b",
        "countryCode": "DE",
        "connectionName": "de-fra.prod.surfshark.com",
        "location": "Frankfurt am Main",
    },
]
US_HOSTS = {"us-sea.prod.surfshark.com:443", "us-dal.prod.surfshark.com:443", "us-nyc-st002.prod.surfshark.com:443"}


@pytest.fixture(autouse=True)
def fake_clusters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SurfsharkVPN, "get_countries", staticmethod(lambda: CLUSTERS))


def host(uri: str | None) -> str:
    assert uri is not None
    return uri.rsplit("@", 1)[1]


def test_country_picks_a_server_name_from_the_api() -> None:
    uri = SurfsharkVPN(USERNAME, PASSWORD).get_proxy("us")
    assert host(uri) in US_HOSTS
    assert uri.startswith(f"https://{USERNAME}:{PASSWORD}@")  # type: ignore[union-attr]


@pytest.mark.parametrize("query", ["gb", "uk"])
def test_gb_and_uk_pick_uk_hosts(query: str) -> None:
    assert host(SurfsharkVPN(USERNAME, PASSWORD).get_proxy(query)) == "uk-lon.prod.surfshark.com:443"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("us:dallas", "us-dal"),
        ("us:new-york", "us-nyc-st002"),
        ("us:newyork", "us-nyc-st002"),
        ("de:frankfurt", "de-fra"),
    ],
)
def test_city_filters_by_location(query: str, expected: str) -> None:
    assert host(SurfsharkVPN(USERNAME, PASSWORD).get_proxy(query)) == f"{expected}.prod.surfshark.com:443"


def test_unknown_city_raises() -> None:
    with pytest.raises(ValueError, match="No servers found in city"):
        SurfsharkVPN(USERNAME, PASSWORD).get_proxy("us:boston")


@pytest.mark.parametrize("server", ["us-dal", "us-nyc-st002", "US-DAL\n"])
def test_specific_server_query(server: str) -> None:
    uri = SurfsharkVPN(USERNAME, PASSWORD).get_proxy(server)
    assert host(uri) == f"{server.strip().lower()}.prod.surfshark.com:443"


@pytest.mark.parametrize("query", ["us-central-150", "us150", "zz", "us-dal:seattle", "https"])
def test_unrecognised_query_returns_none_for_the_next_provider(query: str) -> None:
    assert SurfsharkVPN(USERNAME, PASSWORD).get_proxy(query) is None


@pytest.mark.parametrize("server", ["us-dal", "us-dal.prod.surfshark.com", " us-dal "])
def test_server_map_takes_a_server_name(server: str) -> None:
    proxy = SurfsharkVPN(USERNAME, PASSWORD, server_map={"us": server})
    assert host(proxy.get_proxy("us")) == "us-dal.prod.surfshark.com:443"


def test_server_map_city_key() -> None:
    proxy = SurfsharkVPN(USERNAME, PASSWORD, server_map={"us:seattle": "us-sea"})
    assert host(proxy.get_proxy("us:seattle")) == "us-sea.prod.surfshark.com:443"


def test_server_map_city_query_does_not_fall_back_to_the_country_entry() -> None:
    proxy = SurfsharkVPN(USERNAME, PASSWORD, server_map={"us": "us-sea"})
    assert host(proxy.get_proxy("us:dallas")) == "us-dal.prod.surfshark.com:443"


def test_server_map_alias_needs_no_api_entry() -> None:
    proxy = SurfsharkVPN(USERNAME, PASSWORD, server_map={"nl": "nl-ams"})
    assert host(proxy.get_proxy("nl")) == "nl-ams.prod.surfshark.com:443"


@pytest.mark.parametrize("server", [3844, None])
def test_unusable_server_map_entry_is_ignored_with_a_warning(server: object, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        proxy = SurfsharkVPN(USERNAME, PASSWORD, server_map={"us": server})  # type: ignore[dict-item]
    assert "ignoring server_map entry" in caplog.text
    assert host(proxy.get_proxy("us")) in US_HOSTS


@pytest.mark.parametrize("key", ["gb", "uk", "GB"])
@pytest.mark.parametrize("query", ["gb", "uk"])
def test_server_map_uk_and_gb_are_the_same_region(key: str, query: str) -> None:
    proxy = SurfsharkVPN(USERNAME, PASSWORD, server_map={key: "uk-lon"})
    assert host(proxy.get_proxy(query)) == "uk-lon.prod.surfshark.com:443"
