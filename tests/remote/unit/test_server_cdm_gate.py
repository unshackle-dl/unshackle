"""Only keys that opt in may have the server run the CDM licensing."""

from types import SimpleNamespace

import pytest

from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError, APIErrorCode

pytestmark = pytest.mark.unit

USERS = {
    "tier1key": {"username": "tier1"},
    "tier3key": {"username": "tier3", "server_cdm": True},
}

ENTITLEMENTS = [
    ("tier3key", True),
    ("tier1key", False),
    ("adminsecret", True),  # no users entry, e.g. api_secret
    (None, True),
]


@pytest.fixture
def serve_users(monkeypatch):
    monkeypatch.setattr(handlers.config, "serve", {"users": USERS}, raising=False)
    monkeypatch.setattr(handlers, "request_secret_key", lambda request: request.headers.get("X-Secret-Key"))


def _request(key):
    return SimpleNamespace(headers={"X-Secret-Key": key} if key else {})


@pytest.mark.parametrize(("key", "allowed"), ENTITLEMENTS)
def test_server_cdm_allowed(serve_users, key, allowed):
    assert handlers.server_cdm_allowed(_request(key)) is allowed


def test_no_request_allows(serve_users):
    assert handlers.server_cdm_allowed(None) is True


@pytest.mark.parametrize(("key", "allowed"), ENTITLEMENTS)
def test_download_gate_follows_entitlement(serve_users, key, allowed):
    # A download job licenses with the server CDM, so submission obeys the same gate.
    if allowed:
        handlers.enforce_download_gates({}, _request(key))
    else:
        with pytest.raises(APIError) as ei:
            handlers.enforce_download_gates({}, _request(key))
        assert ei.value.error_code == APIErrorCode.FORBIDDEN


def test_download_gate_without_request(serve_users):
    handlers.enforce_download_gates({})
