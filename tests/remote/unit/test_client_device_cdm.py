"""A remote client's own CDM behind the server's stand-in: the relay, the key policy and the config overlay."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import click
import pytest
from pywidevine import DeviceTypes
from pywidevine.pssh import PSSH

from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError
from unshackle.core.api.input_bridge import AuthStatus, InputBridge
from unshackle.core.cdm.client_relay import ClientDeviceCdm, answer_cdm_call

pytestmark = pytest.mark.unit

MSL_PSSH = PSSH("AAAANHBzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAABQIARIQAAAAAAPSZ0kAAAAAAAAAAA==")
SESSION_KID = UUID("00000000-03d2-6749-0000-000000000000")
CONTENT_KID = UUID("11111111-2222-3333-4444-555555555555")


class FakeWidevineCdm:
    """Records the calls a relayed exchange makes on the client's device."""

    is_playready = False

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.closed: list[bytes] = []

    def open(self) -> bytes:
        return b"local-session"

    def close(self, session_id: bytes) -> None:
        self.closed.append(session_id)

    def set_service_certificate(self, session_id: bytes, certificate: str) -> None:
        self.calls.append(("certificate", certificate))

    def get_license_challenge(self, session_id: bytes, pssh: PSSH, license_type: str, privacy_mode: bool) -> bytes:
        self.calls.append(("challenge", pssh.dumps(), license_type, privacy_mode))
        return b"challenge-bytes"

    def parse_license(self, session_id: bytes, license_message: bytes) -> None:
        self.calls.append(("license", license_message))

    def get_keys(self, session_id: bytes) -> list[Any]:
        return [
            SimpleNamespace(kid=SESSION_KID, type="OPERATOR_SESSION", key=b"\x01" * 16, permissions=["allow_encrypt"]),
            SimpleNamespace(kid=CONTENT_KID, type="CONTENT", key=b"\x02" * 16, permissions=[]),
        ]


class DirectBridge:
    """Answers each call at once on the client's CDM, as the client poller would."""

    def __init__(self, cdm: Any) -> None:
        self.cdm = cdm
        self.sessions: dict[str, bytes] = {}

    def request_cdm(self, call: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(answer_cdm_call(self.cdm, json.loads(json.dumps(call)), self.sessions)))


def test_a_key_exchange_through_the_stand_in_returns_only_session_keys() -> None:
    device = FakeWidevineCdm()
    cdm = ClientDeviceCdm(DirectBridge(device), is_playready=False, security_level=1, system_id=4464)

    session_id = cdm.open()
    cdm.set_service_certificate(session_id, "Q0VSVA==")
    challenge = cdm.get_license_challenge(session_id, MSL_PSSH, "OFFLINE", True)
    cdm.parse_license(session_id, b"licence")
    keys = cdm.get_keys(session_id)

    assert challenge == b"challenge-bytes"
    assert device.calls == [
        ("certificate", "Q0VSVA=="),
        ("challenge", MSL_PSSH.dumps(), "OFFLINE", True),
        ("license", b"licence"),
    ]
    assert [(k.kid, k.type, k.key, k.permissions) for k in keys] == [
        (SESSION_KID, "OPERATOR_SESSION", b"\x01" * 16, ["allow_encrypt"])
    ]


def test_the_client_refuses_to_hand_over_playready_keys() -> None:
    device = SimpleNamespace(is_playready=True)
    answer = answer_cdm_call(device, {"op": "keys", "session": "x", "license": "bGljZW5jZQ=="}, {})
    assert "stay on this machine" in answer["error"]


def test_the_client_refuses_a_challenge_for_the_other_drm_system() -> None:
    answer = answer_cdm_call(FakeWidevineCdm(), {"op": "challenge", "drm": "playready", "init_data": "<x/>"}, {})
    assert "not a playready device" in answer["error"]


def test_a_cdm_call_after_authentication_leaves_the_status_and_client_auth_alone() -> None:
    bridge = InputBridge()
    bridge.status = AuthStatus.AUTHENTICATED
    result: list[dict] = []
    worker = threading.Thread(target=lambda: result.append(bridge.request_cdm({"op": "challenge"}, timeout=5)))
    worker.start()
    for _ in range(100):
        if bridge.get_pending_cdm_call():
            break
        time.sleep(0.01)
    assert bridge.get_pending_cdm_call() == {"op": "challenge"}

    assert bridge.submit_response(json.dumps({"session": "s", "challenge": "Yw=="}))
    worker.join(timeout=2)

    assert result == [{"session": "s", "challenge": "Yw=="}]
    assert bridge.status is AuthStatus.AUTHENTICATED
    assert bridge.answered is False
    assert bridge.get_pending_cdm_call() is None


@pytest.mark.parametrize(
    ("answer", "message"),
    [("not json", "update unshackle"), (json.dumps({"error": "no local CDM is loaded"}), "no local CDM")],
)
def test_a_bad_or_refused_answer_stops_the_service(answer: str, message: str) -> None:
    bridge = InputBridge()
    threading.Timer(0.1, lambda: bridge.submit_response(answer)).start()
    with pytest.raises(RuntimeError, match=message):
        bridge.request_cdm({"op": "keys"}, timeout=5)


def test_the_overlay_keeps_only_the_client_identity() -> None:
    server = {"esn": "SERVER", "Kpe": "server-kpe", "esn_map": {7000: "server-template"}, "endpoints": {"a": "b"}}
    merged, identity = handlers.client_config_overlay(
        server,
        ("esn", "Kpe", "Kph", "esn_map"),
        {"esn": "CLIENT", "esn_map": {"4464": "client-template"}, "endpoints": {"a": "evil"}},
    )
    assert identity is True
    assert merged == {
        "esn": "CLIENT",
        "esn_map": {7000: "server-template", "4464": "client-template"},
        "endpoints": {"a": "b"},
    }


def test_the_overlay_keeps_the_server_identity_when_the_client_sends_none() -> None:
    server = {"esn": "SERVER", "Kph": "k", "esn_map": {7000: "t"}, "other": 1}
    merged, identity = handlers.client_config_overlay(server, ("esn", "Kph", "esn_map"), {"esn_map": {"1": "c"}})
    assert identity is False
    assert merged == {"esn": "SERVER", "Kph": "k", "esn_map": {7000: "t", "1": "c"}, "other": 1}


@pytest.mark.parametrize("supplied", [["esn"], {"esn": ["a"]}, {"esn": {"k": {"nested": 1}}}, {"esn": "x" * 20000}])
def test_the_overlay_rejects_ill_formed_client_config(supplied: Any) -> None:
    with pytest.raises(APIError):
        handlers.client_config_overlay({}, ("esn",), supplied)


def test_a_relaying_client_gets_a_stand_in_with_its_device_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict = {}

    class Declares:
        CLIENT_CONFIG = ("esn",)

    monkeypatch.setattr(handlers, "load_service_yaml", lambda tag: {"esn": "SERVER"})
    monkeypatch.setattr(
        handlers,
        "build_parent_ctx",
        lambda profile, cdm, *a, **k: built.update(cdm=cdm, config=a[3], params=k["extra_params"]),
    )
    monkeypatch.setattr(handlers.Services, "load", lambda tag: Declares)
    monkeypatch.setattr(handlers, "instantiate_service", lambda *a, **k: "service")

    data = {
        "cdm_type": "widevine",
        "cdm_security_level": 1,
        "cdm_relay": True,
        "cdm_system_id": 4464,
        "cdm_device_type": "ANDROID",
        "service_config": {"esn": "CLIENT"},
    }
    bridge = InputBridge()
    handlers.create_service_instance("EXAMPLE1", "t", data, None, [], None, client_device=True, input_bridge=bridge)

    cdm = built["cdm"]
    assert isinstance(cdm, ClientDeviceCdm)
    assert (cdm.is_playready, cdm.security_level, cdm.system_id, cdm.device_type) == (
        False,
        1,
        4464,
        DeviceTypes.ANDROID,
    )
    assert cdm.bridge is bridge
    assert built["config"] == {"esn": "CLIENT"}
    assert built["params"]["client_identity"] is True


def test_an_answered_call_is_no_longer_pending_before_the_service_wakes() -> None:
    bridge = InputBridge()
    release = threading.Event()
    real_wait = bridge._response_ready.wait

    def slow_wait(timeout: float) -> bool:
        release.wait(5)  # hold the service thread asleep after the answer arrives
        return real_wait(timeout)

    bridge._response_ready.wait = slow_wait  # type: ignore[method-assign]
    worker = threading.Thread(target=lambda: bridge.request_cdm({"op": "challenge"}, timeout=5))
    worker.start()
    for _ in range(100):
        if bridge.get_pending_cdm_call():
            break
        time.sleep(0.01)

    assert bridge.submit_response(json.dumps({"session": "s", "challenge": "Yw=="}))
    assert bridge.get_pending_cdm_call() is None
    assert bridge.submit_response("{}") is False
    release.set()
    worker.join(timeout=2)


def test_a_lent_server_device_marks_the_stand_in() -> None:
    server = object()
    cdm = ClientDeviceCdm(DirectBridge(FakeWidevineCdm()), is_playready=True, server_device=lambda: server)
    assert cdm.lent is False
    assert cdm.lend_server_device() is server
    assert cdm.lent is True


def test_a_lent_device_session_never_licenses_live() -> None:
    refusal = handlers.live_licence_refusal(SimpleNamespace(server_device=True, server_cdm=False), object())
    assert refusal is not None
    assert refusal.details == {"reason": "server_device"}


def test_a_vault_only_miss_raises_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "check_vaults", lambda kids, service: None)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda *a, **k: None)
    refusal = handlers.live_licence_refusal(SimpleNamespace(server_device=True), object())
    with pytest.raises(APIError) as caught:
        handlers.handle_single_server_cdm(
            SimpleNamespace(),
            None,
            SimpleNamespace(drm=[]),
            MSL_PSSH.dumps(),
            "widevine",
            None,
            refusal=refusal,
            vault_only=True,
        )
    assert caught.value is refusal


def test_a_vault_only_hit_needs_no_server_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handlers, "check_vaults", lambda kids, service: ({SESSION_KID.hex: "k"}, {SESSION_KID.hex: "v"})
    )
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda *a, **k: None)
    monkeypatch.setattr(
        handlers, "resolve_device_name", lambda *a, **k: pytest.fail("a vault-only lookup loads no device")
    )
    sources: dict = {}
    keys = handlers.handle_single_server_cdm(
        SimpleNamespace(), None, SimpleNamespace(drm=[]), MSL_PSSH.dumps(), "widevine", None, sources, vault_only=True
    )
    assert keys == {SESSION_KID.hex: "k"} and sources == {SESSION_KID.hex: "v"}


def test_the_client_stops_with_a_clear_message_when_no_vault_has_the_key() -> None:
    from unshackle.core.remote_service import RemoteService

    svc = object.__new__(RemoteService)
    with pytest.raises(click.ClickException, match="in no vault"):
        svc.license_locally(SimpleNamespace(id="vid"), {"reason": "server_device"})


class ClosingBridge(DirectBridge):
    """A client whose unshackle predates the ``close`` call, so it refuses it."""

    def request_cdm(self, call: dict[str, Any]) -> dict[str, Any]:
        if call["op"] == "close":
            raise RuntimeError("The client's CDM refused the call: unknown CDM call 'close'")
        return super().request_cdm(call)


def test_closing_the_stand_in_session_closes_the_client_session() -> None:
    device = FakeWidevineCdm()
    bridge = DirectBridge(device)
    cdm = ClientDeviceCdm(bridge, is_playready=False)

    session_id = cdm.open()
    cdm.get_license_challenge(session_id, MSL_PSSH)
    cdm.close(session_id)

    assert device.closed == [b"local-session"]
    assert bridge.sessions == {}


def test_a_client_that_refuses_close_does_not_stop_the_service() -> None:
    device = FakeWidevineCdm()
    cdm = ClientDeviceCdm(ClosingBridge(device), is_playready=False)

    session_id = cdm.open()
    cdm.get_license_challenge(session_id, MSL_PSSH)
    cdm.close(session_id)
    cdm.close(cdm.open())

    assert cdm.get_keys(session_id) == []


def test_a_lent_device_gives_the_service_the_server_config_back(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict = {}
    server_device = object()

    class Declares:
        CLIENT_CONFIG = ("esn", "esn_map")

    monkeypatch.setattr(
        handlers, "load_service_yaml", lambda tag: {"esn": "SERVER", "esn_map": {7000: "s"}, "other": 1}
    )
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *a: server_device)
    monkeypatch.setattr(handlers, "build_parent_ctx", lambda profile, cdm, *a, **k: built.update(cdm=cdm, config=a[3]))
    monkeypatch.setattr(handlers.Services, "load", lambda tag: Declares)
    monkeypatch.setattr(handlers, "instantiate_service", lambda *a, **k: "service")

    data = {"cdm_type": "widevine", "cdm_relay": True, "service_config": {"esn": "CLIENT", "esn_map": {"1": "c"}}}
    handlers.create_service_instance(
        "EXAMPLE1", "t", data, None, [], None, client_device=True, input_bridge=InputBridge()
    )
    service_config = built["config"]
    assert service_config["esn"] == "CLIENT"

    assert built["cdm"].lend_server_device() is server_device
    assert service_config == {"esn": "SERVER", "esn_map": {7000: "s"}, "other": 1}


def test_a_client_map_entry_replaces_the_server_entry_of_the_same_number() -> None:
    merged, _ = handlers.client_config_overlay(
        {"esn_map": {4464: "server-template", 7000: "other"}}, ("esn_map",), {"esn_map": {"4464": "client-template"}}
    )
    assert merged["esn_map"] == {7000: "other", "4464": "client-template"}


def lent_device_session() -> Any:
    return SimpleNamespace(
        server_device=True,
        client_auth=True,
        cache_tag="_sessions/abc/sid/EXAMPLE1",
        input_bridge=None,
        service_instance=SimpleNamespace(
            session=SimpleNamespace(
                headers={"Authorization": "Bearer server", "User-Agent": "ua"},
                cookies=[SimpleNamespace(name="account", value="client")],
            )
        ),
    )


def test_a_lent_device_session_returns_no_cache_at_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from unshackle.core.api import session_store

    session = lent_device_session()

    async def validated(session_id: str, request: Any) -> Any:
        return session

    class Store:
        async def delete(self, session_id: str) -> bool:
            return True

    monkeypatch.setattr(handlers, "get_validated_session", validated)
    monkeypatch.setattr(session_store, "get_session_store", lambda: Store())
    monkeypatch.setattr(handlers, "collect_cache_files", lambda tag: {"MSL/server-esn": "tokens"})

    response = asyncio.run(handlers.session_delete_handler("sid", None))

    assert "cache" not in json.loads(response.body)


def test_a_lent_device_session_keeps_its_auth_headers_and_track_secrets() -> None:
    session = lent_device_session()

    headers, cookies = handlers.client_session_auth(session)

    assert headers == {"User-Agent": "ua"}
    assert cookies == {"account": "client"}
    assert handlers.scrub_track_data({"token": "server", "id": 1}, session) == {"id": 1}


def test_a_lent_device_without_the_vault_grant_asks_the_server_for_no_key() -> None:
    import logging

    from unshackle.core.remote_service import RemoteService

    svc = object.__new__(RemoteService)
    svc.log = logging.getLogger("test")
    svc._session_id = "sid"
    svc._server_cdm = True
    svc._server_device = True
    svc._local_vaults_only = True
    svc.client_licensed = set()
    svc.client = SimpleNamespace(post=lambda *a, **k: pytest.fail("the server refuses every key request"))
    track = SimpleNamespace(id="vid", drm=None)

    svc.resolve_server_keys(SimpleNamespace(tracks=SimpleNamespace(videos=[track], audio=[])))
    with pytest.raises(click.ClickException, match="cannot read the server vault"):
        svc.proxy_license(b"challenge", track, "widevine")


@pytest.mark.parametrize("extra", [None, {"quality": None}])
def test_every_service_context_defaults_quality_to_a_list(extra: Any) -> None:
    parent = handlers.build_parent_ctx(None, None, None, False, [], {}, extra_params=extra)
    assert parent.params["quality"] == []


def test_a_call_no_client_fetches_fails_fast_and_restores_the_status() -> None:
    bridge = InputBridge()
    bridge.status = AuthStatus.AUTHENTICATED
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="none is listening"):
        bridge.request_cdm({"op": "challenge"}, timeout=30, pickup=0.2)
    assert time.monotonic() - started < 5
    assert bridge.status is AuthStatus.AUTHENTICATED
    assert bridge.get_pending_cdm_call() is None


def test_a_fetched_call_gets_the_full_answer_window() -> None:
    bridge = InputBridge()
    result: list[dict] = []
    worker = threading.Thread(target=lambda: result.append(bridge.request_cdm({"op": "keys"}, timeout=5, pickup=0.3)))
    worker.start()
    for _ in range(100):
        if bridge.get_pending_cdm_call():
            break
        time.sleep(0.01)
    time.sleep(0.6)  # past the pickup window: a fetched call must still wait for its answer
    assert bridge.submit_response(json.dumps({"keys": []}))
    worker.join(timeout=2)
    assert result == [{"keys": []}]


def test_cancel_wakes_a_call_waiting_for_a_client() -> None:
    bridge = InputBridge()
    threading.Timer(0.1, bridge.cancel).start()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="cancelled"):
        bridge.request_cdm({"op": "challenge"}, timeout=30, pickup=20)
    assert time.monotonic() - started < 5
