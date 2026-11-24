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


def test_login_bound_providers_fetch_once_across_builds(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from types import SimpleNamespace

    from unshackle.core.proxies.expressvpn import ExpressVPN
    from unshackle.core.proxies.proton import ProtonVPN

    requests_made: list[str] = []

    def express_request(self: ExpressVPN, method: str, url: str, **_: object) -> SimpleNamespace:
        requests_made.append("express")
        return SimpleNamespace(json=lambda: {"locations": [{"country_code": "ca"}]})

    def proton_api(self: ProtonVPN, method: str, path: str) -> SimpleNamespace:
        requests_made.append(path)
        body = {"LogicalServers": [{"ExitCountry": "CA"}]} if path.endswith("logicals") else {"VPN": {"MaxTier": 0}}
        return SimpleNamespace(json=lambda: body)

    monkeypatch.setattr(ExpressVPN, "get_srt", lambda self: "srt")
    monkeypatch.setattr(ExpressVPN, "request", express_request)
    monkeypatch.setattr(ProtonVPN, "api", proton_api)

    for _ in range(2):
        assert ExpressVPN(cache_path=str(tmp_path / "e.json")).get_locations() == [{"country_code": "ca"}]
        proton = ProtonVPN(cookie_path=str(tmp_path / "c.txt"), cache_path=str(tmp_path / "p.json"))
        assert proton.get_logicals() == [{"ExitCountry": "CA"}]
        assert proton.get_max_tier() == 0

    assert sorted(requests_made) == ["/vpn", "/vpn/v1/logicals", "express"]
