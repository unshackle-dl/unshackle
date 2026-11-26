"""What an API key may do, answered by its KeyPolicy from a plain serve.users entry."""

from pathlib import Path

import pytest

from unshackle.core.api.key_policy import KeyPolicy, api_keys, named_services
from unshackle.core.services import Services

pytestmark = pytest.mark.unit

GRANTS = ("server_cdm", "server_vault", "server_accounts")


@pytest.fixture(autouse=True)
def tags(monkeypatch):
    monkeypatch.setattr(Services, "get_tag", staticmethod(lambda tag: tag.upper()))


def test_a_key_with_no_entry_holds_every_grant_but_the_server_proxy():
    absent = KeyPolicy.of("api-secret", {"users": {"other": {}}})
    assert absent == KeyPolicy.of(None, {"users": {"other": {}}}) == KeyPolicy(None, {"users": {"other": {}}})
    assert not absent.configured and absent.admin
    assert all(absent.allows(name, "EXAMPLE") and absent.grant(name) is True for name in GRANTS)
    assert absent.allows("server_proxy") is absent.grant("server_proxy") is False
    assert absent.max_height("EXAMPLE") is None and absent.rate_limit is None


def test_a_call_with_no_request_gets_the_policy_of_a_key_with_no_entry():
    from unshackle.core.api.handlers import request_policy

    assert request_policy(None) == KeyPolicy.of(None)
    assert request_policy(None).entry is None


def test_the_policy_repr_holds_no_other_api_key():
    assert "secret" not in repr(KeyPolicy({}, {"api_secret": "secret", "users": {"other-secret": {}}}))


def test_an_empty_entry_holds_no_grant():
    policy = KeyPolicy({})
    assert policy.configured and not policy.admin
    assert not any(policy.allows(name, "EXAMPLE") for name in (*GRANTS, "server_proxy"))
    assert policy.grant("server_cdm") is False


@pytest.mark.parametrize("value", ["true", 1, "yes"])
def test_only_a_real_true_grants_the_server_proxy(value):
    assert KeyPolicy({"server_proxy": True}).allows("server_proxy") is True
    assert KeyPolicy({"server_proxy": value}).allows("server_proxy") is False


@pytest.mark.parametrize("name", GRANTS)
@pytest.mark.parametrize(
    ("setting", "service", "allowed"),
    [
        (True, "EXAMPLE", True),
        (True, None, True),
        (["example"], "EXAMPLE", True),
        (["EXAMPLE"], "OTHER", False),
        (["EXAMPLE"], None, False),
        ("example", "EXAMPLE", True),
        ("EXAMPLE", "OTHER", False),
        (False, "EXAMPLE", False),
    ],
)
def test_a_grant_covers_every_service_or_the_listed_tags(name, setting, service, allowed):
    assert KeyPolicy({name: setting}).allows(name, service) is allowed


def test_the_server_cdm_grant_gives_the_server_vault():
    assert KeyPolicy({"server_cdm": ["EXAMPLE"]}).allows("server_vault", "EXAMPLE")
    assert not KeyPolicy({"server_cdm": ["EXAMPLE"]}).allows("server_vault", "OTHER")
    assert KeyPolicy({"server_vault": ["OTHER"]}).allows("server_vault", "OTHER")


def test_the_dashboard_view_shows_the_grant_enforcement_reads():
    policy = KeyPolicy({"server_cdm": "example", "server_accounts": 1, "server_proxy": "true"})
    assert policy.grant("server_cdm") == ["example"]
    assert policy.grant("server_accounts") is True
    assert policy.grant("server_proxy") is False


def test_admin_needs_a_real_true():
    assert KeyPolicy({"admin": True}).admin
    assert not KeyPolicy({"admin": "true"}).admin


@pytest.mark.parametrize(
    ("serve", "entry", "services"),
    [
        ({}, {}, None),
        ({"services": ["example", "OTHER"]}, {}, {"EXAMPLE", "OTHER"}),
        ({}, {"services": ["example"]}, {"EXAMPLE"}),
        ({"services": ["EXAMPLE", "OTHER"]}, {"services": ["other", "THIRD"]}, {"OTHER"}),
    ],
)
def test_the_services_are_the_global_list_and_the_keys_list_together(serve, entry, services):
    allowed = KeyPolicy(entry, serve).services
    assert (set(allowed) if allowed is not None else None) == services


def test_the_named_services_are_the_global_list_else_every_keys_list():
    users = {"a": {"services": ["example"]}, "b": {"services": ["OTHER", 7]}, "c": {}, "d": "bad"}
    assert sorted(named_services({"users": users})) == ["EXAMPLE", "OTHER"]
    assert named_services({"services": ["third"], "users": users}) == ["THIRD"]
    assert named_services({"users": ["not", "a", "mapping"]}) == []
    assert api_keys({"users": users}) == ["a", "b", "c", "d"]


@pytest.mark.parametrize(
    ("setting", "service", "cap"),
    [
        (1080, "EXAMPLE1", 1080),
        ({"example1": 720, "default": 1080}, "EXAMPLE1", 720),
        ({"example1": 720, "default": 1080}, "EXAMPLE2", 1080),
        ({"EXAMPLE1": 1080}, "EXAMPLE2", None),
        ({"EXAMPLE1": None, "default": 1080}, "EXAMPLE1", 1080),
        (None, "EXAMPLE1", None),
        (True, "EXAMPLE1", None),
    ],
)
def test_the_height_cap_per_service(setting, service, cap):
    assert KeyPolicy({"server_cdm_max_height": setting}).max_height(service) == cap


def test_a_key_with_no_entry_gets_every_server_device():
    serve = {"users": {"k1": {"devices": ["mine"]}}, "devices": [Path("/x/dev.wvd")], "playready_devices": []}
    assert KeyPolicy.of("k1", serve).devices("devices") == ["mine"]
    assert KeyPolicy.of("k1", serve).devices("playready_devices") == []
    assert KeyPolicy.of("admin-secret", serve).devices("devices") == ["dev"]


def test_a_malformed_entry_grants_nothing():
    policy = KeyPolicy.of("k", {"users": {"k": ["not", "a", "mapping"]}})
    assert policy.configured and not policy.admin and not policy.allows("server_cdm", "EXAMPLE")


def test_the_rate_limit_is_the_keys_own_else_its_tiers():
    tiers = {"bot": {"rate_limit": 600}}
    assert KeyPolicy({"tier": "bot"}, {"tiers": tiers}).rate_limit == 600
    assert KeyPolicy({"tier": "bot", "rate_limit": 5}, {"tiers": tiers}).rate_limit == 5
    assert KeyPolicy({"tier": "bot"}, {"tiers": {"bot": {"rate_limit": "600"}}}).rate_limit is None
    assert KeyPolicy({"tier": "bot"}).tier == "bot"


def test_shared_and_username():
    assert KeyPolicy({"shared": True, "username": "tier1"}).shared
    assert not KeyPolicy({"shared": "true"}).shared
    assert KeyPolicy({"username": "tier1"}).username == "tier1"
    assert KeyPolicy({}).username is None


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ({"tier": "gone"}, "serve.users.u.tier: no such tier 'gone'"),
        ({"rate_limit": -1}, "serve.users.u.rate_limit"),
        ({"server_cdm_max_height": "1080"}, "serve.users.u.server_cdm_max_height must be"),
        ({"server_cdm_max_height": {"EXAMPLE": -1}}, "server_cdm_max_height values"),
        ({"services": {"EXAMPLE": {}}}, "list of service tags"),
    ],
)
def test_the_entry_check_names_the_first_problem(entry, problem):
    assert problem in KeyPolicy(entry, {"tiers": {"bot": {}}}).entry_error("serve.users.u")


@pytest.mark.parametrize("value", [None, 1080, {"EXAMPLE1": 720, "default": None}])
def test_the_entry_check_accepts_a_good_height_cap(value):
    assert KeyPolicy({"server_cdm_max_height": value, "tier": "bot"}, {"tiers": {"bot": {}}}).entry_error("u") is None
