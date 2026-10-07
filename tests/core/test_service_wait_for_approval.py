import logging
from types import SimpleNamespace

import pytest

from unshackle.core.api.input_bridge import InputBridge
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
