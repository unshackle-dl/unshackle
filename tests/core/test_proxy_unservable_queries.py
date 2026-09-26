"""Every proxy provider returns None for a query it cannot serve, so a bare --proxy query moves on
to the next proxy provider instead of stopping at the first one that does not know the form."""

from __future__ import annotations

import subprocess

import pytest

from unshackle.core.proxies import Gluetun, Hola, NordVPN, WindscribeVPN
from unshackle.core.proxies import hola as hola_module

pytestmark = pytest.mark.unit

CREDENTIALS = {"username": "a" * 24, "password": "b" * 24}


@pytest.fixture
def nordvpn(monkeypatch: pytest.MonkeyPatch) -> NordVPN:
    countries = [{"id": 228, "code": "US", "name": "United States", "serverCount": 1}]
    monkeypatch.setattr(NordVPN, "get_countries", staticmethod(lambda: countries))
    monkeypatch.setattr(NordVPN, "get_recommended_servers", staticmethod(lambda country_id, city_id=None: []))
    return NordVPN(**CREDENTIALS)


@pytest.fixture
def windscribe(monkeypatch: pytest.MonkeyPatch) -> WindscribeVPN:
    countries = [
        {"country_code": "US", "groups": [{"city": "Dallas", "hosts": [{"hostname": "us-central-150.x.com"}]}]}
    ]
    monkeypatch.setattr(WindscribeVPN, "get_countries", staticmethod(lambda: countries))
    return WindscribeVPN(**CREDENTIALS)


@pytest.mark.parametrize("query", ["us-bos", "us_yyz", "zz", "us"])
def test_nordvpn(nordvpn: NordVPN, query: str) -> None:
    # "us" has no recommended servers in the fixture
    assert nordvpn.get_proxy(query) is None


@pytest.mark.parametrize("query", ["us999", "us-bos", "us_yyz", "zz"])
def test_windscribe(windscribe: WindscribeVPN, query: str) -> None:
    assert windscribe.get_proxy(query) is None


def test_windscribe_still_finds_a_listed_server(windscribe: WindscribeVPN) -> None:
    assert windscribe.get_proxy("us150") == f"https://{'a' * 24}:{'b' * 24}@us-central-150.x.com:443"


def test_gluetun_skips_a_bare_region() -> None:
    assert Gluetun.__new__(Gluetun).get_proxy("us") is None


def hola_with_output(monkeypatch: pytest.MonkeyPatch, stdout: str, stderr: str = "") -> Hola:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(hola_module.subprocess, "run", run)
    hola = Hola.__new__(Hola)
    hola.binary = "hola-proxy"
    hola.countries = [{"us": "United States"}, {"gb": "United Kingdom"}]
    return hola


HOLA_STDOUT = (
    "Login: user-uuid-1\nPassword: secret\nProxy-Authorization: basic abc\n\n"
    "host,ip_address,direct,peer,hola,trial,trial_peer,vendor\n"
    "zagent1.hola.org,203.0.113.7,22222,22223,22224,22225,22226,digitalocean\n"
)


def test_hola_parses_stdout_while_the_log_goes_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    hola = hola_with_output(monkeypatch, HOLA_STDOUT, 'MAIN: INFO Action "list proxies" succeeded\n')
    assert hola.get_proxy("us") == "http://user-uuid-1:secret@203.0.113.7:22223"


@pytest.mark.parametrize(("query", "stdout"), [("us-bos", ""), ("zz", HOLA_STDOUT), ("us", "no login"), ("us", "")])
def test_hola(monkeypatch: pytest.MonkeyPatch, query: str, stdout: str) -> None:
    assert hola_with_output(monkeypatch, stdout).get_proxy(query) is None


def test_hola_ban_while_hola_proxy_keeps_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("hola-proxy", 30, stderr=b"Transaction error: temporary ban detected. Retrying")

    monkeypatch.setattr(hola_module.subprocess, "run", run)
    hola = Hola.__new__(Hola)
    hola.binary = "hola-proxy"
    hola.countries = [{"us": "United States"}]
    with pytest.raises(ConnectionError, match="banned"):
        hola.get_proxy("us")


@pytest.fixture
def nordvpn_with_cities(monkeypatch: pytest.MonkeyPatch) -> NordVPN:
    countries = [
        {
            "id": 228,
            "code": "US",
            "name": "United States",
            "serverCount": 2,
            "cities": [
                {"id": 1, "name": "Seattle", "dns_name": "seattle"},
                {"id": 2, "name": "New York", "dns_name": "new-york"},
            ],
        },
        {"id": 227, "code": "GB", "name": "United Kingdom", "serverCount": 1, "cities": []},
    ]
    by_city = {None: "us100.nordvpn.com", 1: "us1.nordvpn.com", 2: "us2.nordvpn.com"}

    def recommended(country_id: int, city_id: int | None = None) -> list[dict]:
        return [{"hostname": "uk6018.nordvpn.com" if country_id == 227 else by_city[city_id]}]

    monkeypatch.setattr(NordVPN, "get_countries", staticmethod(lambda: countries))
    monkeypatch.setattr(NordVPN, "get_recommended_servers", staticmethod(recommended))
    return NordVPN(**CREDENTIALS)


@pytest.mark.parametrize(
    ("query", "host"),
    [
        ("us", "us100"),
        ("us:new-york", "us2"),
        ("us:new york", "us2"),
        ("us:seattle", "us1"),
        ("uk", "uk6018"),
        ("gb", "uk6018"),
        ("gb6018", "uk6018"),
    ],
)
def test_nordvpn_city_and_uk(nordvpn_with_cities: NordVPN, query: str, host: str) -> None:
    uri = nordvpn_with_cities.get_proxy(query)
    assert uri is not None and uri.endswith(f"@{host}.proxy.nordvpn.com:89")


def test_nordvpn_unknown_city_raises(nordvpn_with_cities: NordVPN) -> None:
    with pytest.raises(ValueError, match="No servers found in city 'boston'"):
        nordvpn_with_cities.get_proxy("us:boston")
