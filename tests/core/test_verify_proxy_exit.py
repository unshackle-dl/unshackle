from types import SimpleNamespace

import pytest

from unshackle.core.utils import ip_info

SERVER = {"ip": "1.1.1.1", "country": "gb"}


def patch_lookups(monkeypatch, proxied, server=SERVER, live_server=None, proxy_errors=0):
    """Answer the proxied lookup, the cached server lookup and the live server lookup."""
    live_server = live_server if live_server is not None else server

    def fake(session=None, cached=False, errors=None):
        if cached:
            return server
        if session is None:
            return live_server
        if errors is not None:
            errors.extend(OSError("refused") for _ in range(proxy_errors))
        return proxied

    monkeypatch.setattr(ip_info, "get_ip_info", fake)


def test_dead_proxy_raises(monkeypatch):
    patch_lookups(monkeypatch, None, proxy_errors=len(ip_info.build_providers()))
    with pytest.raises(ConnectionError, match=r"no IP lookup got through the proxy \(OSError\)"):
        ip_info.verify_proxy_exit(object())


def test_rate_limited_providers_do_not_reject(monkeypatch):
    patch_lookups(monkeypatch, None, proxy_errors=len(ip_info.build_providers()) - 1)
    assert ip_info.verify_proxy_exit(object()) == {}


def test_exit_ip_equal_to_server_raises(monkeypatch):
    patch_lookups(monkeypatch, {"ip": "1.1.1.1", "country": "gb"})
    with pytest.raises(ConnectionError, match="proxy exit IP is the IP of this machine"):
        ip_info.verify_proxy_exit(object())


def test_stale_cached_server_ip_does_not_reject(monkeypatch):
    patch_lookups(monkeypatch, {"ip": "1.1.1.1", "country": "gb"}, live_server={"ip": "3.3.3.3", "country": "gb"})
    assert ip_info.verify_proxy_exit(object())["country"] == "gb"


def test_distinct_exit_ip_passes(monkeypatch):
    patch_lookups(monkeypatch, {"ip": "2.2.2.2", "country": "us"})
    assert ip_info.verify_proxy_exit(object())["country"] == "us"


def test_unknown_server_ip_skips_comparison(monkeypatch):
    patch_lookups(monkeypatch, {"ip": "1.1.1.1", "country": "us"}, server=None)
    assert ip_info.verify_proxy_exit(object())["country"] == "us"


def test_proxied_lookup_ignores_env_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    sess = ip_info.lookup_session(SimpleNamespace(proxies={"all": "http://127.0.0.1:2"}))
    settings = sess.merge_environment_settings("https://ipinfo.io/json", {}, None, None, None)
    assert settings["proxies"]["https"] == "http://127.0.0.1:2"


def test_local_lookup_keeps_env_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    sess = ip_info.lookup_session(None)
    settings = sess.merge_environment_settings("https://ipinfo.io/json", {}, None, None, None)
    assert settings["proxies"]["https"] == "http://127.0.0.1:1"


def test_get_ip_info_collects_connection_errors_only(monkeypatch):
    def refused(session):
        raise OSError("refused")

    def no_usable_data(session):
        return None

    monkeypatch.setattr(ip_info, "build_providers", lambda: [("a", refused), ("b", no_usable_data)])
    errors: list[Exception] = []
    assert ip_info.get_ip_info(SimpleNamespace(proxies={"all": "http://127.0.0.1:9"}), errors=errors) is None
    assert [str(e) for e in errors] == ["refused"]
