import asyncio
import json
import time

import pytest

from unshackle.core import remote_service
from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError, APIErrorCode
from unshackle.core.config import config

SERVICES = [
    {
        "tag": "AMZN",
        "aliases": ["amazon"],
        "cli_params": [{"kind": "option", "name": "extras"}, {"kind": "argument", "name": "title"}],
    }
]
INFO = SERVICES[0]


class FakeRemote:
    service_tag = "AMZN"

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def pool(monkeypatch):
    logins = []

    def fake_setup(data, server, profile, title_id, request=None):
        time.sleep(0.05)  # long enough that every request queues on the lease lock
        logins.append(title_id)
        return FakeRemote()

    monkeypatch.setattr(handlers, "setup_remote_service", fake_setup)
    monkeypatch.setattr(handlers, "_remote_pool", {})
    return logins


def lease(data, **extra):
    return handlers.lease_remote_service({**data, **extra}, "srv", INFO, None, "show")


DATA = {"service": "AMZN", "title_id": "show", "remote": "srv"}


async def test_concurrent_leases_share_one_session(pool):
    leases = await asyncio.gather(*(lease(DATA, wanted=f"S01E0{i}") for i in range(1, 7)))

    assert pool == ["show"]
    assert len({id(x.service) for x in leases}) == 1
    for x in leases:
        await handlers.release_remote_service(x)
    assert not leases[0].service.closed and handlers._remote_pool


async def test_a_broken_session_leaves_the_pool_at_once_but_closes_after_its_last_holder(pool):
    first = await lease(DATA)
    second = await lease(DATA)
    first.dead = True

    await handlers.release_remote_service(first)
    assert not handlers._remote_pool and not first.service.closed

    await handlers.release_remote_service(second)
    assert first.service.closed
    assert (await lease(DATA)).service is not first.service


async def test_only_options_that_reach_the_remote_split_the_pool(pool):
    a = await lease(DATA, extras="a")
    b = await lease(DATA, extras="b")
    c = await lease(DATA, extras="a", tvdb_id="1", client_name="x")
    assert a.service is not b.service
    assert a.service is c.service


@pytest.fixture
def remote_config(monkeypatch):
    monkeypatch.setattr(config, "remote_services", {"srv": {"url": "https://srv", "api_key": "k"}})
    monkeypatch.setattr(remote_service, "remote_service_list", lambda name: SERVICES)
    monkeypatch.setattr(handlers, "get_allowed_services", lambda request=None: None)


def test_remote_server_resolves_case_and_aliases(remote_config):
    assert handlers.remote_server({"remote": "srv", "service": "amzn"}) == ("srv", INFO)
    assert handlers.remote_server({"remote": "srv", "service": "Amazon"}) == ("srv", INFO)
    assert handlers.remote_server({"service": "AMZN"}) is None


@pytest.mark.parametrize(
    "data, code",
    [
        ({"remote": "nope", "service": "AMZN"}, APIErrorCode.INVALID_PARAMETERS),
        ({"remote": ["srv"], "service": "AMZN"}, APIErrorCode.INVALID_PARAMETERS),
        ({"remote": "srv", "service": "NF"}, APIErrorCode.INVALID_SERVICE),
    ],
)
def test_remote_server_rejects(remote_config, data, code):
    with pytest.raises(APIError) as e:
        handlers.remote_server(data)
    assert e.value.error_code == code


def test_remote_server_applies_the_allowlist_and_reports_an_unreachable_server(remote_config, monkeypatch):
    monkeypatch.setattr(handlers, "get_allowed_services", lambda request=None: ["NF"])
    with pytest.raises(APIError) as e:
        handlers.remote_server({"remote": "srv", "service": "AMZN"})
    assert e.value.error_code == APIErrorCode.INVALID_SERVICE

    monkeypatch.setattr(remote_service, "remote_service_list", lambda name: None)
    with pytest.raises(APIError) as e:
        handlers.remote_server({"remote": "srv", "service": "AMZN"})
    assert e.value.error_code == APIErrorCode.SERVICE_UNAVAILABLE


def test_make_remote_service_passes_only_cli_options(remote_config, monkeypatch):
    built = {}
    monkeypatch.setattr(remote_service, "resolve_server", lambda name: ("https://srv", "k", {"AMZN": {"x": 1}}))
    monkeypatch.setattr(remote_service, "RemoteService", lambda *a, **kw: built.update(args=a, **kw))

    remote_service.make_remote_service(
        None, "srv", "AMZN", "id", {"service_params": {"extras": "y", "service": "DSNP", "proxy": "x"}}, False
    )
    assert built["service_params"] == {"extras": "y"}
    assert "AMZN" not in built["args"][5]  # apply_overrides=False keeps the remote's overrides out

    remote_service.make_remote_service(None, "srv", "AMZN", "id", {"extras": "z", "service": "DSNP", "quality": 1})
    assert built["service_params"] == {"extras": "z"}
    assert built["args"][5]["AMZN"] == {"x": 1}


def test_remote_error_carries_its_reason_and_exits_a_cli_run():
    err = remote_service.RemoteError("Server error [SESSION_NOT_FOUND]: gone", "SESSION_NOT_FOUND")
    assert isinstance(err, SystemExit) and err.code == 1 and err.kind == "SESSION_NOT_FOUND"
    assert err.session_broke
    assert not remote_service.RemoteError("Server error [NOT_FOUND]: no title", "NOT_FOUND").session_broke


def test_a_geofenced_remote_service_gets_its_first_allowed_region_as_proxy(monkeypatch):
    monkeypatch.setattr(handlers, "server_proxy_allowed", lambda request=None: True)
    fenced = {"tag": "PCOK", "geofence": ["US", "ca"], "geoblock": ["us"]}
    assert handlers.geofence_proxy({}, {"tag": "PCOK", "geofence": ["US", "ca"]}) == {"proxy": "us"}
    assert handlers.geofence_proxy({}, fenced) == {"proxy": "ca"}
    assert handlers.geofence_proxy({"proxy": "gb"}, fenced) == {}
    assert handlers.geofence_proxy({"no_proxy": True}, fenced) == {}
    assert handlers.geofence_proxy({}, INFO) == {}

    monkeypatch.setattr(handlers, "server_proxy_allowed", lambda request=None: False)
    assert handlers.geofence_proxy({}, fenced) == {}


def test_a_quiet_remote_login_shows_its_last_server_lines_as_a_notice(monkeypatch):
    import logging
    from threading import Lock

    shown = []
    monkeypatch.setattr(remote_service, "show_notice", shown.append)
    monkeypatch.setattr(remote_service.time, "sleep", lambda s: None)
    logs = iter([["+ IP Region: CA"], ["+ Open URL: https://disneyplus.com/start\n+ Enter pair code: 9835-2503"], []])
    statuses = iter(["authenticating", "authenticating", "authenticating", "authenticated"])

    class Client:
        def get(self, endpoint):
            return {"status": next(statuses)}

        def get_optional(self, endpoint):
            batch = next(logs, [])
            remote.seq += len(batch)
            return {"logs": [{"level": "INFO", "message": m} for m in batch], "last_seq": remote.seq}

    remote = object.__new__(remote_service.RemoteService)
    remote.seq = 0
    remote.client, remote._session_id, remote._log_seq = Client(), "sid", 0
    remote._recent_logs, remote.auth_notice, remote._log_drain_lock = [], None, Lock()
    remote.log, remote._cdm_sessions = logging.getLogger("test"), {}
    monkeypatch.setattr(remote_service.RemoteService, "local_cdm", None)

    remote.poll_auth_completion(poll_interval=0)
    # silent while the remote was still logging; the pair code once it went quiet; cleared when done
    assert shown == ["+ IP Region: CA\n+ Open URL: https://disneyplus.com/start\n+ Enter pair code: 9835-2503", None]
    assert remote.auth_notice is None


async def test_a_remote_login_prompt_waits_for_its_owners_answer(monkeypatch):
    import threading as th

    class Remote:
        auth_notice = None

    login = handlers.RemoteLogin("owner", "srv", "DSNP", "show", Remote())
    monkeypatch.setattr(handlers, "_remote_logins", {login.id: login})
    monkeypatch.setattr(handlers, "is_admin", lambda request: False)
    caller = {"key": "owner"}
    monkeypatch.setattr(handlers, "caller_key", lambda request: caller["key"])
    got = []
    asker = th.Thread(target=lambda: got.append(login.bridge.request_input("Enter the code sent to your email")))
    asker.start()
    while login.bridge.get_pending_prompt() is None:
        await asyncio.sleep(0.01)

    listed = json.loads((await handlers.remote_logins_handler(None)).text)["logins"]
    assert listed[0]["prompt"] == "Enter the code sent to your email"

    caller["key"] = "someone-else"
    assert json.loads((await handlers.remote_logins_handler(None)).text)["logins"] == []
    with pytest.raises(APIError) as e:
        await handlers.remote_login_input_handler(login.id, {"response": "123456"}, None)
    assert e.value.error_code == APIErrorCode.NOT_FOUND

    caller["key"] = "owner"
    await handlers.remote_login_input_handler(login.id, {"response": "123456"}, None)
    asker.join(2)
    assert got == ["123456"] and login.bridge.get_pending_prompt() is None
    with pytest.raises(APIError) as e:
        await handlers.remote_login_input_handler(login.id, {"response": "again"}, None)
    assert e.value.error_code == APIErrorCode.CONFLICT
