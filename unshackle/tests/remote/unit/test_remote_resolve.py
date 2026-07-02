"""Unit tests for resolve_server / _resolve_proxy in unshackle.core.remote_service."""

from __future__ import annotations

import click
import pytest

from unshackle.core.remote_service import _resolve_proxy, resolve_server

pytestmark = pytest.mark.unit


@pytest.fixture
def empty_remote_services(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core import remote_service as rs

    monkeypatch.setattr(rs.config, "remote_services", {})


@pytest.fixture
def single_remote_service(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core import remote_service as rs

    monkeypatch.setattr(
        rs.config,
        "remote_services",
        {
            "primary": {
                "url": "https://primary:8080",
                "api_key": "key-abc",
                "services": {"ATV": True, "NF": True},
                "server_cdm": True,
            }
        },
    )


@pytest.fixture
def multi_remote_services(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core import remote_service as rs

    monkeypatch.setattr(
        rs.config,
        "remote_services",
        {
            "a": {"url": "https://a:8080", "api_key": "ka"},
            "b": {"url": "https://b:8080", "api_key": "kb"},
        },
    )


def test_resolve_server_no_config_raises_click(empty_remote_services) -> None:
    with pytest.raises(click.ClickException) as exc:
        resolve_server(None)
    assert "remote_services" in str(exc.value.message)


def test_resolve_server_single_picks_only_entry(single_remote_service) -> None:
    url, key, services = resolve_server(None)
    assert url == "https://primary:8080"
    assert key == "key-abc"
    assert services["_server_cdm"] is True
    assert services.get("ATV") is True


def test_resolve_server_explicit_name(single_remote_service) -> None:
    url, key, services = resolve_server("primary")
    assert url == "https://primary:8080"
    assert services["_server_cdm"] is True


def test_resolve_server_unknown_name_raises(single_remote_service) -> None:
    with pytest.raises(click.ClickException) as exc:
        resolve_server("bogus")
    assert "bogus" in str(exc.value.message)


def test_resolve_server_multi_requires_explicit(multi_remote_services) -> None:
    with pytest.raises(click.ClickException) as exc:
        resolve_server(None)
    assert "--server" in str(exc.value.message)


def test_resolve_server_multi_with_name(multi_remote_services) -> None:
    url, key, services = resolve_server("b")
    assert url == "https://b:8080"
    assert key == "kb"
    assert services["_server_cdm"] is False


def test_resolve_proxy_none_returns_none() -> None:
    assert _resolve_proxy(None) is None
    assert _resolve_proxy("") is None


def test_resolve_proxy_passes_through_value(monkeypatch: pytest.MonkeyPatch) -> None:
    import unshackle.core.proxies.resolve as resolve_mod

    monkeypatch.setattr(resolve_mod, "initialize_proxy_providers", lambda: [])
    monkeypatch.setattr(resolve_mod, "resolve_proxy", lambda arg, providers: f"http://proxy/{arg}")

    assert _resolve_proxy("us") == "http://proxy/us"


def test_resolve_proxy_value_error_becomes_click(monkeypatch: pytest.MonkeyPatch) -> None:
    import unshackle.core.proxies.resolve as resolve_mod

    monkeypatch.setattr(resolve_mod, "initialize_proxy_providers", lambda: [])

    def boom(*_):
        raise ValueError("no such country")

    monkeypatch.setattr(resolve_mod, "resolve_proxy", boom)

    with pytest.raises(click.ClickException) as exc:
        _resolve_proxy("xx")
    assert "no such country" in str(exc.value.message)
