"""An API key's own cdm and service_config entries, and the identity cache a server device keeps per key."""

import asyncio
import base64
import json
import zlib
from types import SimpleNamespace

import pytest

from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError
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
    user = {"cdm": {"example": "own_pr"}}
    assert handlers.key_cdm_config(user).cdm == {"OTHER": "x", "example": "own_pr"}
    assert handlers.resolve_device_name(user, "widevine", "EXAMPLE") == "own_pr"
    assert handlers.key_cdm_config({}) is config


def test_licence_device_comes_from_the_key_entry(server):
    assert handlers.resolve_device_name({"cdm": {"EXAMPLE": "own"}}, "playready", "EXAMPLE") == "own"
    assert handlers.resolve_device_name({}, "playready", "EXAMPLE") == "shared_pr"


def test_a_session_keeps_the_user_config_it_was_created_with(server, monkeypatch):
    session = SimpleNamespace(user_config={"cdm": {"EXAMPLE": "pinned"}})
    monkeypatch.setattr(handlers, "serve_user_config", lambda key: {"cdm": {"EXAMPLE": "reloaded"}})
    assert handlers.session_user_config(session, None)["cdm"]["EXAMPLE"] == "pinned"
    assert handlers.session_user_config(SimpleNamespace(), None)["cdm"]["EXAMPLE"] == "reloaded"


def test_the_key_block_beats_the_server_config_and_profile_sections(server, monkeypatch):
    base = {"device_id": "server", "device_key": "server", "p1": {"device_id": "p1", "other": 1}, "urls": {"a": "b"}}
    monkeypatch.setattr(handlers, "load_service_yaml", lambda service: json.loads(json.dumps(base)))
    user = {"service_config": {"example": {"device_id": "key", "device_key": "key"}}}

    merged = handlers.key_service_yaml("EXAMPLE", user)

    assert merged["device_id"] == merged["device_key"] == "key"
    assert merged["p1"] == {"device_id": "key", "other": 1}
    assert merged["urls"] == {"a": "b"}
    assert handlers.key_service_yaml("EXAMPLE", {})["device_id"] == "server"
    assert handlers.key_owns_identity(user, "EXAMPLE") and not handlers.key_owns_identity(user, "OTHER")


class _Service:
    IDENTITY_CACHE = ("keys/",)


def test_identity_keys_live_in_the_api_key_directory_and_never_reach_the_client(server):
    cache = handlers.keyed_cache("_s", _Service(), "EXAMPLE", "k", {}, temporary=True)
    cache.get("keys/device1").set("keys")
    cache.get("tokens").set("login")

    assert set(handlers.collect_cache_files("_s")) == {"tokens"}
    assert list((server / "_identity").rglob("device1.json"))
    assert handlers.keyed_cache("_t", _Service(), "EXAMPLE", "k", {}, True).get("keys/device1").data == "keys"

    text = base64.b64encode(zlib.compress(b'{"data": "planted"}')).decode("ascii")
    handlers.write_client_cache({"keys/device1": text}, "_s")
    assert cache.get("keys/device1").data == "keys"


def test_a_lasting_cache_keeps_identity_keys_apart_only_for_a_key_with_its_own_identity(server):
    own = {"service_config": {"EXAMPLE": {"device_id": "key"}}}
    assert handlers.keyed_cache("_accounts/EXAMPLE/p", _Service(), "EXAMPLE", "k", {}).route is None
    assert handlers.keyed_cache("_accounts/EXAMPLE/p", _Service(), "EXAMPLE", "k", own).route is not None
    assert handlers.keyed_cache("_s", object(), "EXAMPLE", "k", own, temporary=True).route is None


def test_a_job_is_refused_for_a_key_with_its_own_identity(server, monkeypatch):
    monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request, service: True)
    monkeypatch.setattr(handlers, "server_cdm_max_height", lambda request, service: None)
    monkeypatch.setattr(handlers, "serve_user_config", lambda key: {"cdm": {"EXAMPLE": "own"}})
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
    assert problem in handlers.key_config_error("serve.users.u", user_cfg)


def test_a_known_device_passes_validation(server, monkeypatch):
    monkeypatch.setattr(handlers.Services, "get_tags", staticmethod(lambda: ["EXAMPLE"]))
    monkeypatch.setattr(handlers, "detect_cdm_type", lambda name, cfg: "playready")
    assert handlers.key_config_error("u", {"cdm": {"EXAMPLE": {"playready": "dev"}}, "service_config": {}}) is None
    assert handlers.key_config_error("u", {"cdm": {"EXAMPLE": {"default": "dev"}}, "service_config": None}) is None
    assert handlers.key_config_error("u", {"cdm": {"EXAMPLE": {"widevine": "dev"}}}) is not None


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

    user = {"service_config": {"EXAMPLE": {"device_id": "key"}}} if own else {}
    text = base64.b64encode(zlib.compress(b'{"data": "x"}')).decode("ascii")
    monkeypatch.setattr(handlers, "validate_service", lambda service, request=None: service)
    monkeypatch.setattr(handlers, "resolve_handler_proxy", lambda *args: (None, []))
    monkeypatch.setattr(handlers, "server_account_for", lambda *args: False)
    monkeypatch.setattr(handlers, "serve_user_config", lambda key: user)
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

    assert session.user_config is user
    assert sent_back == returned


def test_a_reload_checks_key_devices_against_the_new_config(server, monkeypatch):
    monkeypatch.setattr(handlers.Services, "get_tags", staticmethod(lambda: ["EXAMPLE"]))
    fresh = SimpleNamespace(remote_cdm=[{"name": "new_remote", "device_type": "PLAYREADY"}])
    user = {"cdm": {"EXAMPLE": "new_remote"}}
    assert handlers.key_config_error("u", user, fresh) is None
    assert handlers.key_config_error("u", user) is not None


def test_a_block_value_reaches_only_plain_values_of_other_sections(server, monkeypatch):
    base = {"p1": {"device_id": "p1", "headers": {"a": "b"}}, "device_key": "server"}
    monkeypatch.setattr(handlers, "load_service_yaml", lambda service: json.loads(json.dumps(base)))
    block = {"device_id": "key", "headers": {"c": "d"}, "device_key": {"nested": 1}}

    merged = handlers.key_service_yaml("EXAMPLE", {"service_config": {"EXAMPLE": block}})

    assert merged["p1"] == {"device_id": "key", "headers": {"a": "b"}}
    assert merged["device_key"] == {"nested": 1}


def test_a_removed_key_loses_its_identity_cache(server):
    handlers.keyed_cache("_s", _Service(), "EXAMPLE", "gone", {}, True).get("keys/device1").set("keys")
    handlers.keyed_cache("_s", _Service(), "EXAMPLE", "kept", {}, True).get("keys/device1").set("keys")
    handlers.drop_identity_cache({"gone"})
    assert not (server / "_identity" / handlers.key_namespace("gone")).exists()
    assert (server / "_identity" / handlers.key_namespace("kept")).exists()
