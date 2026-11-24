"""Unit tests for the shared proxy provider loader: a proxy provider that fails to load is skipped
by a bare query and reported by a query that names it."""

from __future__ import annotations

import logging
from typing import Any

import pytest
import requests

from unshackle.core.config import config
from unshackle.core.proxies import NordVPN, SurfsharkVPN
from unshackle.core.proxies.resolve import Unavailable, find_provider, load_proxy_providers, resolve_proxy

pytestmark = pytest.mark.unit

CREDENTIALS = {"username": "a" * 24, "password": "b" * 24}
LOGIN_CREDENTIALS = {"username": "someone@example.com", "password": "hunter2"}
OTHERS = ("basic", "expressvpn", "protonvpn", "windscribevpn", "gluetun", "hola", "controld")


@pytest.fixture(autouse=True)
def nordvpn_misconfigured_surfshark_up(monkeypatch: pytest.MonkeyPatch) -> None:
    clusters = [{"countryCode": "US", "connectionName": "us-dal.prod.surfshark.com", "location": "Dallas"}]
    monkeypatch.setattr(config, "proxy_providers", {"nordvpn": LOGIN_CREDENTIALS, "surfsharkvpn": CREDENTIALS})
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
    with pytest.raises(ValueError, match="NordVPN could not load: NordVPN needs its Service Credentials"):
        resolve_proxy(f"{name}:us", load_proxy_providers(OTHERS))


def test_query_naming_an_unknown_provider_lists_the_loaded_ones() -> None:
    with pytest.raises(ValueError, match=r"'mullvad' was not found. Available: \['SurfsharkVPN'\]"):
        find_provider(load_proxy_providers(OTHERS), "mullvad")


def test_raise_errors_raises_the_load_failure() -> None:
    with pytest.raises(ValueError, match="Service Credentials"):
        load_proxy_providers(OTHERS, raise_errors=True)


def test_catalogue_is_fetched_on_first_use_and_a_failed_fetch_fails_only_its_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def refuse() -> list[dict]:
        calls.append(1)
        raise requests.ConnectionError("no route to api.nordvpn.com")

    monkeypatch.setattr(config, "proxy_providers", {"nordvpn": CREDENTIALS, "surfsharkvpn": CREDENTIALS})
    monkeypatch.setattr(NordVPN, "get_countries", staticmethod(refuse))
    nordvpn, surfshark = load_proxy_providers(OTHERS)
    assert isinstance(nordvpn, NordVPN) and not calls

    assert resolve_proxy("us", [nordvpn, surfshark]).endswith("@us-dal.prod.surfshark.com:443")
    with pytest.raises(ValueError, match="nordvpn failed for us: no route to api.nordvpn.com"):
        resolve_proxy("nordvpn:us", [nordvpn, surfshark])


def test_bare_query_order_follows_config_key_order_with_controld_last(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.proxies import ControlD

    order = {"controld": {"token": "t"}, "surfsharkvpn": CREDENTIALS, "nordvpn": CREDENTIALS, "basic": {"us": "x"}}
    monkeypatch.setattr(config, "proxy_providers", order)
    monkeypatch.setattr(ControlD, "get_locations", lambda self: [])
    loaded = load_proxy_providers(("expressvpn", "protonvpn", "hola"), quiet=True)
    names = [getattr(p, "name", p.__class__.__name__) for p in loaded]
    assert names == ["Basic", "SurfsharkVPN", "NordVPN", "ControlD"]


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


def build_controld() -> Any:
    from unshackle.core.proxies import ControlD

    return ControlD(token="t")


def build_hola(monkeypatch: pytest.MonkeyPatch) -> Any:
    from unshackle.core import binaries
    from unshackle.core.proxies import Hola

    monkeypatch.setattr(binaries, "HolaProxy", "hola-proxy")
    return Hola()


@pytest.mark.parametrize(
    ("name", "fetch", "attr"),
    [
        ("NordVPN", "get_countries", "countries"),
        ("WindscribeVPN", "get_countries", "countries"),
        ("SurfsharkVPN", "get_countries", "countries"),
        ("ControlD", "get_locations", "catalogue"),
        ("Hola", "get_countries", "countries"),
    ],
)
def test_construction_makes_no_request_and_the_first_read_fetches_once(
    name: str, fetch: str, attr: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unshackle.core import proxies

    cls = getattr(proxies, name)
    calls: list[int] = []

    def counted(*args: Any) -> list[dict]:
        calls.append(1)
        return [{"country": "US"}]

    is_static = isinstance(vars(cls)[fetch], staticmethod)
    monkeypatch.setattr(cls, fetch, staticmethod(counted) if is_static else counted)
    if name == "ControlD":
        provider = build_controld()
    elif name == "Hola":
        provider = build_hola(monkeypatch)
    else:
        provider = cls(**CREDENTIALS)
    assert not calls

    assert getattr(provider, attr) == [{"country": "US"}]
    getattr(provider, attr)
    assert calls == [1]


def test_health_probe_reads_each_proxy_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.api.handlers import run_health_checks

    calls: list[int] = []

    def countries() -> list[dict]:
        calls.append(1)
        return [{"serverCount": 3}]

    monkeypatch.setattr(config, "key_vaults", [])
    monkeypatch.setattr(config, "proxy_providers", {"nordvpn": CREDENTIALS})
    monkeypatch.setattr(NordVPN, "get_countries", staticmethod(countries))
    detail = {c["id"]: c for c in run_health_checks()}["proxies"]["detail"]
    assert calls == [1]
    assert "NordVPN (1 Country (3 Servers))" in detail
