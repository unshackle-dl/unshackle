"""Unit tests for unshackle.core.api.input_bridge.InputBridge."""

from __future__ import annotations

import threading
import time

import pytest

from unshackle.core.api.input_bridge import AuthStatus, BridgeCancelledError, InputBridge

pytestmark = pytest.mark.unit


def test_initial_status_is_authenticating() -> None:
    bridge = InputBridge()
    assert bridge.status is AuthStatus.AUTHENTICATING
    assert bridge.get_pending_prompt() is None
    assert bridge.error is None


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
    with pytest.raises(TimeoutError):
        bridge.request_input("hello", timeout=0.6)
    assert bridge.status is AuthStatus.FAILED
    assert "timed out" in (bridge.error or "")


def test_cancel_before_request_raises_immediately() -> None:
    bridge = InputBridge()
    bridge.cancel()
    with pytest.raises(BridgeCancelledError):
        bridge.request_input("hello", timeout=5)
    assert bridge.status is AuthStatus.FAILED


def test_cancel_unblocks_pending_request() -> None:
    bridge = InputBridge()
    exc: list[Exception] = []

    def worker() -> None:
        try:
            bridge.request_input("OTP?", timeout=5)
        except BridgeCancelledError as e:
            exc.append(e)

    t = threading.Thread(target=worker)
    t.start()
    for _ in range(50):
        if bridge.status is AuthStatus.PENDING_INPUT:
            break
        time.sleep(0.02)

    bridge.cancel()
    t.join(timeout=2)
    assert exc and isinstance(exc[0], BridgeCancelledError)
    assert bridge.status is AuthStatus.FAILED


def test_get_pending_prompt_returns_none_outside_pending_state() -> None:
    bridge = InputBridge()
    bridge.status = AuthStatus.AUTHENTICATED
    assert bridge.get_pending_prompt() is None


def test_status_and_error_setters() -> None:
    bridge = InputBridge()
    bridge.status = AuthStatus.AUTHENTICATED
    bridge.error = "boom"
    assert bridge.status is AuthStatus.AUTHENTICATED
    assert bridge.error == "boom"
