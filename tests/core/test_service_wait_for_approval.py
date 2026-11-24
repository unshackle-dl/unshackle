import logging
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from unshackle.core import console
from unshackle.core.api.input_bridge import NO_USER_MESSAGE, InputBridge, NoUserBridge
from unshackle.core.service import Service


def _service(bridge=None):
    return SimpleNamespace(log=logging.getLogger("test"), _input_bridge=bridge)


def test_returns_the_first_value_check_gives():
    answers = iter([None, None, "token"])
    assert Service.wait_for_approval(_service(), "enter code", lambda: next(answers), timeout=5, interval=0) == "token"


def test_times_out_when_check_never_gives_a_value():
    with pytest.raises(TimeoutError):
        Service.wait_for_approval(_service(), "enter code", lambda: None, timeout=0.05, interval=0.01)


def test_stops_when_the_remote_session_is_cancelled():
    bridge = InputBridge()
    calls = []

    def check():
        calls.append(1)
        bridge.cancel()

    with pytest.raises(RuntimeError):
        Service.wait_for_approval(_service(bridge), "enter code", check, timeout=5, interval=0)
    assert len(calls) == 1


def _raise_value_error():
    raise ValueError("denied")


def test_logs_the_message_for_a_remote_session(caplog):
    """A remote session reads the message only from the service log."""
    with caplog.at_level(logging.INFO, logger="test"):
        Service.wait_for_approval(_service(), "enter code", lambda: "token", timeout=5, interval=0)
    assert "enter code" in caplog.messages


def test_a_request_with_no_user_fails_at_once():
    calls = []
    with pytest.raises(RuntimeError, match="needs the user"):
        Service.wait_for_approval(_service(NoUserBridge()), "enter code", lambda: calls.append(1), timeout=5)
    assert not calls
    with pytest.raises(RuntimeError, match="needs the user"):
        Service.request_input(_service(NoUserBridge()), "Enter OTP")


def test_no_user_message_is_an_authentication_error():
    from unshackle.core.api.errors import APIErrorCode, categorize_exception

    assert categorize_exception(RuntimeError(NO_USER_MESSAGE)).error_code == APIErrorCode.AUTH_FAILED


@pytest.mark.parametrize(
    "check, expectation",
    [
        (lambda: "token", nullcontext()),
        (lambda: None, pytest.raises(TimeoutError)),
        (_raise_value_error, pytest.raises(ValueError)),
    ],
)
def test_shows_the_message_as_a_notice_and_clears_it(monkeypatch, check, expectation):
    notices = []
    monkeypatch.setattr(console, "_notice_handler", notices.append)
    with expectation:
        Service.wait_for_approval(_service(), "enter code", check, timeout=0.05, interval=0.01)
    assert notices == ["enter code", None]
