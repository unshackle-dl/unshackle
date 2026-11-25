"""Unit tests for unshackle.core.api.input_bridge.InputBridge."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from unshackle.core.api.input_bridge import InputBridge
from unshackle.core.remote_protocol import AuthStatus

pytestmark = pytest.mark.unit


def test_initial_status_is_authenticating() -> None:
    bridge = InputBridge()
    assert bridge.status is AuthStatus.AUTHENTICATING
    assert bridge.get_pending_prompt() is None


def test_submit_response_returns_false_when_no_prompt_pending() -> None:
    bridge = InputBridge()
    assert bridge.submit_response("foo") is False


def test_request_input_blocks_until_submit() -> None:
    bridge = InputBridge()
    result: list[str] = []

    def worker() -> None:
        result.append(bridge.request_input("OTP?", timeout=5))

    t = threading.Thread(target=worker)
    t.start()
    for _ in range(50):
        if bridge.get_pending_prompt() == "OTP?":
            break
        time.sleep(0.02)
    assert bridge.status is AuthStatus.PENDING_INPUT
    assert bridge.get_pending_prompt() == "OTP?"

    assert bridge.submit_response("123456") is True
    t.join(timeout=2)
    assert result == ["123456"]
    assert bridge.status is AuthStatus.AUTHENTICATING
    assert bridge.get_pending_prompt() is None


def test_request_input_times_out() -> None:
    bridge = InputBridge()
    with pytest.raises(TimeoutError, match="No client response"):
        bridge.request_input("hello", timeout=0.6)
    assert bridge.status is AuthStatus.AUTHENTICATING


def test_a_prompt_after_a_timed_out_one_takes_its_answer() -> None:
    """A login that catches the timeout and asks again gets the answer through the prompt routes."""
    bridge = InputBridge()
    with pytest.raises(TimeoutError):
        bridge.request_input("Code?", timeout=0.01)
    offered = threading.Event()
    bridge.channel = SimpleNamespace(offer=lambda ask: offered.set() or False, attached=False)  # type: ignore[assignment]
    answers: list[str] = []
    worker = threading.Thread(target=lambda: answers.append(bridge.request_input("Code again?", timeout=5)))
    worker.start()
    assert offered.wait(5)
    assert bridge.status is AuthStatus.PENDING_INPUT
    assert bridge.submit_response("1234")
    worker.join(5)
    assert answers == ["1234"]


def test_cancel_before_request_raises_immediately() -> None:
    bridge = InputBridge()
    bridge.cancel()
    with pytest.raises(RuntimeError, match="cancelled"):
        bridge.request_input("hello", timeout=5)
    assert bridge.status is AuthStatus.AUTHENTICATING  # ending a session leaves its login state as it was


def test_cancel_unblocks_pending_request() -> None:
    bridge = InputBridge()
    exc: list[Exception] = []

    def worker() -> None:
        try:
            bridge.request_input("OTP?", timeout=5)
        except RuntimeError as e:
            exc.append(e)

    t = threading.Thread(target=worker)
    t.start()
    for _ in range(50):
        if bridge.status is AuthStatus.PENDING_INPUT:
            break
        time.sleep(0.02)

    bridge.cancel()
    t.join(timeout=2)
    assert exc and isinstance(exc[0], RuntimeError)
    assert bridge.status is AuthStatus.AUTHENTICATING


def test_get_pending_prompt_returns_none_outside_pending_state() -> None:
    bridge = InputBridge()
    bridge.login_status = AuthStatus.AUTHENTICATED
    assert bridge.get_pending_prompt() is None


def test_login_status_setter() -> None:
    bridge = InputBridge()
    bridge.login_status = AuthStatus.AUTHENTICATED
    assert bridge.status is AuthStatus.AUTHENTICATED


def test_request_input_after_authentication_fails_fast() -> None:
    """The client stops polling for prompts once authenticated, so a later prompt must not wait for an answer."""
    bridge = InputBridge()
    bridge.login_status = AuthStatus.AUTHENTICATED
    with pytest.raises(RuntimeError, match="after authentication"):
        bridge.request_input("Pick a profile", timeout=5)
    assert bridge.status is AuthStatus.AUTHENTICATED
