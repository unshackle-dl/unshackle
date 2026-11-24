"""Unit tests for the shared proxy provider loader: a proxy provider that fails to load is skipped
by a bare query and reported by a query that names it."""

from __future__ import annotations

import logging

import pytest
import requests

from unshackle.core.config import config
from unshackle.core.proxies import NordVPN, SurfsharkVPN
from unshackle.core.proxies.resolve import Unavailable, find_provider, load_proxy_providers, resolve_proxy

pytestmark = pytest.mark.unit

CREDENTIALS = {"username": "a" * 24, "password": "b" * 24}
OTHERS = ("basic", "expressvpn", "protonvpn", "windscribevpn", "gluetun", "hola", "controld")


@pytest.fixture(autouse=True)
def nordvpn_down_surfshark_up(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> list[dict]:
        raise requests.ConnectionError("no route to api.nordvpn.com")

    clusters = [{"countryCode": "US", "connectionName": "us-dal.prod.surfshark.com", "location": "Dallas"}]
    monkeypatch.setattr(config, "proxy_providers", {"nordvpn": CREDENTIALS, "surfsharkvpn": CREDENTIALS})
    monkeypatch.setattr(NordVPN, "get_countries", staticmethod(refuse))
    monkeypatch.setattr(SurfsharkVPN, "get_countries", staticmethod(lambda: clusters))


def test_failed_provider_is_kept_as_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        nordvpn, surfshark = load_proxy_providers(OTHERS)
    assert isinstance(nordvpn, Unavailable) and nordvpn.name == "NordVPN"
    assert isinstance(surfshark, SurfsharkVPN)
    assert "NordVPN proxy provider could not load" in caplog.text


def test_bare_query_skips_the_failed_provider() -> None:
    uri = resolve_proxy("us", load_proxy_providers(OTHERS))
    assert uri.endswith("@us-dal.prod.surfshark.com:443")


def test_query_naming_another_provider_still_works() -> None:
    uri = resolve_proxy("surfsharkvpn:us", load_proxy_providers(OTHERS))
    assert uri.endswith("@us-dal.prod.surfshark.com:443")


@pytest.mark.parametrize("name", ["nordvpn", "NordVPN"])
def test_query_naming_the_failed_provider_reports_why(name: str) -> None:
    with pytest.raises(ValueError, match="NordVPN could not load: no route to api.nordvpn.com"):
        resolve_proxy(f"{name}:us", load_proxy_providers(OTHERS))


def test_query_naming_an_unknown_provider_lists_the_loaded_ones() -> None:
    with pytest.raises(ValueError, match=r"'mullvad' was not found. Available: \['SurfsharkVPN'\]"):
        find_provider(load_proxy_providers(OTHERS), "mullvad")


def test_raise_errors_raises_the_load_failure() -> None:
    with pytest.raises(requests.ConnectionError):
        load_proxy_providers(OTHERS, raise_errors=True)


def test_rest_path_loads_windscribe_but_not_gluetun(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.proxies import Gluetun, WindscribeVPN
    from unshackle.core.proxies.resolve import initialize_proxy_providers

    countries = [
        {"country_code": "US", "groups": [{"city": "Dallas", "hosts": [{"hostname": "us-central-1.example"}]}]}
    ]
    monkeypatch.setattr(config, "proxy_providers", {"windscribevpn": CREDENTIALS, "gluetun": {"windscribe": {}}})
    monkeypatch.setattr(WindscribeVPN, "get_countries", staticmethod(lambda: countries))

    providers = initialize_proxy_providers(quiet=True)
    assert any(isinstance(p, WindscribeVPN) for p in providers)
    assert not any(isinstance(p, Gluetun) or getattr(p, "name", None) == "Gluetun" for p in providers)
    assert resolve_proxy("windscribevpn:us", providers).endswith("@us-central-1.example:443")
