"""An API key's own cdm and service_config entries, and the identity cache a server device keeps per key."""

import asyncio
import base64
import json
import zlib
from types import SimpleNamespace

import pytest

from unshackle.core.api import handlers, key_policy
from unshackle.core.api.errors import APIError
from unshackle.core.api.key_policy import KeyPolicy
from unshackle.core.api.session_store import get_session_store
from unshackle.core.config import config
from unshackle.core.remote_protocol import AuthStatus


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "cdm", {"EXAMPLE": {"widevine": "shared_wv", "playready": "shared_pr"}, "OTHER": "x"})
    monkeypatch.setattr(config.directories, "cache", tmp_path)
    monkeypatch.setattr(handlers.Services, "get_tag", staticmethod(lambda tag: tag.upper()))
    return tmp_path


def test_a_key_entry_replaces_the_whole_server_entry(server):
    user = KeyPolicy({"cdm": {"example": "own_pr"}})
    assert user.cdm_config().cdm == {"OTHER": "x", "example": "own_pr"}
    assert handlers.resolve_device_name(user, "widevine", "EXAMPLE") == "own_pr"
    assert KeyPolicy({}).cdm_config() is config


def test_licence_device_comes_from_the_key_entry(server):
    assert handlers.resolve_device_name(KeyPolicy({"cdm": {"EXAMPLE": "own"}}), "playready", "EXAMPLE") == "own"
    assert handlers.resolve_device_name(KeyPolicy({}), "playready", "EXAMPLE") == "shared_pr"


def test_a_session_keeps_the_policy_it_was_created_with(server, monkeypatch):
    pinned, reloaded = KeyPolicy({"cdm": {"EXAMPLE": "pinned"}}), KeyPolicy({"cdm": {"EXAMPLE": "reloaded"}})
    monkeypatch.setattr(handlers, "request_policy", lambda request: reloaded)
    assert handlers.session_policy(SimpleNamespace(policy=pinned), None) is pinned
    assert handlers.session_policy(SimpleNamespace(), None) is reloaded


def test_the_key_block_beats_the_server_config_and_profile_sections(server, monkeypatch):
    base = {"device_id": "server", "device_key": "server", "p1": {"device_id": "p1", "other": 1}, "urls": {"a": "b"}}
    monkeypatch.setattr(key_policy, "load_service_config", lambda service: json.loads(json.dumps(base)))
    user = KeyPolicy({"service_config": {"example": {"device_id": "key", "device_key": "key"}}})

    merged = user.service_yaml("EXAMPLE")

    assert merged["device_id"] == merged["device_key"] == "key"
    assert merged["p1"] == {"device_id": "key", "other": 1}
    assert merged["urls"] == {"a": "b"}
    assert KeyPolicy({}).service_yaml("EXAMPLE")["device_id"] == "server"
    assert user.owns_identity("EXAMPLE") and not user.owns_identity("OTHER")


class _Service:
    IDENTITY_CACHE = ("keys/",)


def test_identity_keys_live_in_the_api_key_directory_and_never_reach_the_client(server):
    cache = handlers.keyed_cache("_s", _Service(), "EXAMPLE", "k", KeyPolicy(), temporary=True)
    cache.get("keys/device1").set("keys")
    cache.get("tokens").set("login")

    assert set(handlers.collect_cache_files("_s")) == {"tokens"}
    assert list((server / "_identity").rglob("device1.json"))
    assert handlers.keyed_cache("_t", _Service(), "EXAMPLE", "k", KeyPolicy(), True).get("keys/device1").data == "keys"

    text = base64.b64encode(zlib.compress(b'{"data": "planted"}')).decode("ascii")
    handlers.write_client_cache({"keys/device1": text}, "_s")
    assert cache.get("keys/device1").data == "keys"


def test_a_lasting_cache_keeps_identity_keys_apart_only_for_a_key_with_its_own_identity(server):
    own = KeyPolicy({"service_config": {"EXAMPLE": {"device_id": "key"}}})
    assert handlers.keyed_cache("_accounts/EXAMPLE/p", _Service(), "EXAMPLE", "k", KeyPolicy()).route is None
    assert handlers.keyed_cache("_accounts/EXAMPLE/p", _Service(), "EXAMPLE", "k", own).route is not None
    assert handlers.keyed_cache("_s", object(), "EXAMPLE", "k", own, temporary=True).route is None


def test_a_job_is_refused_for_a_key_with_its_own_identity(server, monkeypatch):
    monkeypatch.setattr(
        handlers, "request_policy", lambda request: KeyPolicy({"server_cdm": True, "cdm": {"EXAMPLE": "own"}})
    )
    with pytest.raises(APIError, match="its own"):
        handlers.enforce_download_gates({"service": "EXAMPLE"})


@pytest.mark.parametrize(
    "user_cfg, problem",
    [
        ({"services": {"EXAMPLE": {}}}, "list of service tags"),
        ({"cdm": "dev"}, "mapping of service tag"),
        ({"cdm": {"default": "dev"}}, "a key has no default entry"),
        ({"service_config": {"DEFAULT": {"device_id": "x"}}}, "a key has no default entry"),
        ({"cdm": {"EXAMPLE": {"widevine": "dev", "tiers": "x"}}}, "device name or a widevine/playready/default"),
        ({"cdm": {"EXAMPLE": "missing"}}, "no CDM device named 'missing'"),
        ({"service_config": {"EXAMPLE": "device_id"}}, "mapping of config key"),
    ],
)
def test_bad_key_config_is_refused_at_load(server, monkeypatch, user_cfg, problem):
    monkeypatch.setattr(handlers.Services, "get_tags", staticmethod(lambda: ["EXAMPLE"]))
    assert problem in KeyPolicy(user_cfg).entry_error("serve.users.u")


def test_a_known_device_passes_validation(server, monkeypatch):
    monkeypatch.setattr(handlers.Services, "get_tags", staticmethod(lambda: ["EXAMPLE"]))
    monkeypatch.setattr(key_policy, "device_system", lambda name, cfg: "playready")
    assert KeyPolicy({"cdm": {"EXAMPLE": {"playready": "dev"}}, "service_config": {}}).entry_error("u") is None
    assert KeyPolicy({"cdm": {"EXAMPLE": {"default": "dev"}}, "service_config": None}).entry_error("u") is None
    assert KeyPolicy({"cdm": {"EXAMPLE": {"widevine": "dev"}}}).entry_error("u") is not None


@pytest.mark.parametrize(
    "server_cdm, sent, own, returned",
    [
        (True, {}, True, {"tokens", "keys/planted"}),
        (False, {}, True, {"tokens", "keys/planted"}),
        (False, {"cdm_type": "playready", "cdm_relay": True}, True, {"tokens", "keys/planted"}),
        (False, {"cdm_type": "playready", "cdm_relay": True}, False, {"tokens", "keys/device1", "keys/planted"}),
    ],
)
def test_a_session_keeps_identity_keys_from_the_client_unless_the_identity_is_the_clients(
    server, monkeypatch, server_cdm, sent, own, returned
):
    class Service(_Service):
        _input_bridge = log = cache = None

        def authenticate(self, cookies, credential):
            self.cache.get("keys/device1").set("identity")
            self.cache.get("tokens").set("login")

    user = KeyPolicy({"service_config": {"EXAMPLE": {"device_id": "key"}}} if own else {})
    text = base64.b64encode(zlib.compress(b'{"data": "x"}')).decode("ascii")
    monkeypatch.setattr(handlers, "validate_service", lambda service, request=None: service)
    monkeypatch.setattr(handlers, "resolve_handler_proxy", lambda *args: (None, []))
    monkeypatch.setattr(handlers, "server_account_for", lambda *args: False)
    monkeypatch.setattr(handlers, "request_policy", lambda request: user)
    monkeypatch.setattr(handlers, "choose_session_cdm", lambda *args: (server_cdm, None))
    monkeypatch.setattr(handlers.Services, "load", lambda service: Service)
    monkeypatch.setattr(handlers, "create_service_instance", lambda *args, **kwargs: (Service(), None, None))

    async def run():
        data = {"service": "EXAMPLE", "title_id": "t", "cookies": "x", "cache": {"keys/planted": text}, **sent}
        response = await handlers.session_create_handler(data)
        session = get_session_store().peek(json.loads(response.body)["session_id"])
        try:
            for _ in range(250):
                if session.auth_status != AuthStatus.AUTHENTICATING:
                    break
                await asyncio.sleep(0.02)
            return session, set(handlers.collect_cache_files(session.cache_tag))
        finally:
            await get_session_store().delete(session.session_id)

    session, sent_back = asyncio.run(run())

    assert session.policy is user
    assert sent_back == returned


def test_a_reload_checks_key_devices_against_the_new_config(server, monkeypatch):
    monkeypatch.setattr(handlers.Services, "get_tags", staticmethod(lambda: ["EXAMPLE"]))
    fresh = SimpleNamespace(remote_cdm=[{"name": "new_remote", "device_type": "PLAYREADY"}])
    user = KeyPolicy({"cdm": {"EXAMPLE": "new_remote"}})
    assert user.entry_error("u", fresh) is None
    assert user.entry_error("u") is not None


def test_a_block_value_reaches_only_plain_values_of_other_sections(server, monkeypatch):
    base = {"p1": {"device_id": "p1", "headers": {"a": "b"}}, "device_key": "server"}
    monkeypatch.setattr(key_policy, "load_service_config", lambda service: json.loads(json.dumps(base)))
    block = {"device_id": "key", "headers": {"c": "d"}, "device_key": {"nested": 1}}

    merged = KeyPolicy({"service_config": {"EXAMPLE": block}}).service_yaml("EXAMPLE")

    assert merged["p1"] == {"device_id": "key", "headers": {"a": "b"}}
    assert merged["device_key"] == {"nested": 1}


def test_a_removed_key_loses_its_identity_cache(server):
    handlers.keyed_cache("_s", _Service(), "EXAMPLE", "gone", KeyPolicy(), True).get("keys/device1").set("keys")
    handlers.keyed_cache("_s", _Service(), "EXAMPLE", "kept", KeyPolicy(), True).get("keys/device1").set("keys")
    handlers.drop_identity_cache({"gone"})
    assert not (server / "_identity" / handlers.key_namespace("gone")).exists()
    assert (server / "_identity" / handlers.key_namespace("kept")).exists()
