import pytest

from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError


@pytest.fixture
def accounts(monkeypatch):
    monkeypatch.setattr(
        handlers.config, "credentials", {"EX": {"ca_main": "a:b", "eu_box": "c:d", "shared": "e:f"}, "SINGLE": "u:p"}
    )
    monkeypatch.setattr(
        handlers.config,
        "serve",
        {
            "server_accounts": {
                "EX": {"ca_main": "ca", "eu_box": ["gb", "fr", "de"], "shared": "global"},
                "SINGLE": True,
            }
        },
    )
    monkeypatch.setattr(handlers.Services, "get_tag", staticmethod(lambda s: s))
    handlers._account_counters.clear()


def test_pool_filters_by_region(accounts):
    assert handlers.server_account_pool("EX", "gb") == ["eu_box", "shared"]
    assert handlers.server_account_pool("EX", "CA") == ["ca_main", "shared"]
    assert handlers.server_account_pool("EX", "jp") == ["shared"]
    assert handlers.server_account_pool("SINGLE", None) == [None]
    assert handlers.server_account_pool("OTHER", "gb") is None


def test_round_robin_and_empty_pool(accounts):
    assert [handlers.next_server_profile("EX", "gb") for _ in range(3)] == ["eu_box", "shared", "eu_box"]
    handlers.config.serve["server_accounts"]["EX"].pop("shared")
    with pytest.raises(APIError):
        handlers.next_server_profile("EX", "jp")


def test_advertised_regions(accounts):
    assert handlers.server_account_regions("EX") == {"regions": ["ca", "de", "fr", "gb"], "global": True}
    assert handlers.server_account_regions("SINGLE") == {"regions": [], "global": True}
    assert handlers.server_account_regions("OTHER") is None


def test_falsy_or_malformed_spec_is_off(accounts):
    handlers.config.serve["server_accounts"]["EX"] = False
    assert handlers.server_account_spec("EX") is None
    handlers.config.serve["server_accounts"]["EX"] = "ca"
    assert handlers.server_account_spec("EX") is None
    assert handlers.server_account_regions("EX") is None
    with pytest.raises(ValueError):
        handlers.validate_server_accounts()


def test_validate_rejects_unknown_profile(accounts, tmp_path, monkeypatch):
    monkeypatch.setattr(handlers.config.directories, "cookies", tmp_path)
    handlers.config.serve["server_accounts"]["EX"]["typo"] = "ca"
    with pytest.raises(ValueError, match="typo"):
        handlers.validate_server_accounts()
    (tmp_path / "EX").mkdir()
    (tmp_path / "EX" / "typo.txt").write_text("# Netscape HTTP Cookie File\n")
    handlers.validate_server_accounts()


def test_single_account_always_same(accounts):
    handlers.config.serve["server_accounts"]["EX"].pop("shared")
    assert [handlers.next_server_profile("EX", "ca") for _ in range(3)] == ["ca_main"] * 3
    assert handlers.next_server_profile("SINGLE", None) is None


def test_client_managed_gets_nothing(monkeypatch, accounts):
    monkeypatch.setattr(handlers, "load_service_yaml", lambda s: {})
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *a: None)
    monkeypatch.setattr(handlers.Services, "load", staticmethod(lambda s: object()))
    monkeypatch.setattr(handlers, "instantiate_service", lambda *a: "svc")
    svc, cookies, cred = handlers.create_service_instance("OTHER", "t", {}, None, [], None)
    assert (svc, cookies, cred) == ("svc", None, None)


def test_per_key_gate(accounts, monkeypatch):
    class Req:
        def __init__(self, key):
            self.headers = {"X-Secret-Key": key}

    monkeypatch.setattr(handlers, "request_secret_key", lambda r: r.headers["X-Secret-Key"])
    handlers.config.serve["users"] = {"k1": {}, "k2": {"server_accounts": True}, "k3": {"server_accounts": ["EX"]}}
    assert handlers.server_accounts_allowed(Req("k1"), "EX") is False
    assert handlers.server_accounts_allowed(Req("k2"), "EX") is True
    assert handlers.server_accounts_allowed(Req("k3"), "EX") is True
    assert handlers.server_accounts_allowed(Req("k3"), "SINGLE") is False
    assert handlers.server_accounts_allowed(Req("admin"), "EX") is True
    assert handlers.server_account_for(Req("k1"), "EX") is False
    assert handlers.server_account_for(Req("k2"), "OTHER") is False


def test_rotation_per_region_pool(accounts):
    picks = [handlers.next_server_profile("EX", r) for r in ["ca", "gb", "ca", "gb"]]
    assert picks == ["ca_main", "eu_box", "shared", "shared"]


def test_server_account_ignores_client_credentials(monkeypatch, accounts):
    monkeypatch.setattr(handlers, "load_service_yaml", lambda s: {})
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *a: None)
    monkeypatch.setattr(handlers.Services, "load", staticmethod(lambda s: object()))
    monkeypatch.setattr(handlers, "instantiate_service", lambda *a: "svc")
    monkeypatch.setattr(handlers, "server_account_cookies", lambda s, p: "server-jar")
    data = {"credentials": {"username": "old", "password": "client"}, "cookies": "AAAA", "cache": {"x": "y"}}
    svc, cookies, cred = handlers.create_service_instance("EX", "t", data, None, [], "ca_main", server_account=True)
    assert cookies == "server-jar"
    assert cred.username == "a" and cred.password == "b"


def test_validate_rejects_bad_region_values(accounts):
    handlers.config.serve["server_accounts"]["EX"]["ca_main"] = ["gb", False]
    with pytest.raises(ValueError, match="quote"):
        handlers.validate_server_accounts()
    handlers.config.serve["server_accounts"]["EX"]["ca_main"] = "canada"
    with pytest.raises(ValueError):
        handlers.validate_server_accounts()
