"""Only keys that opt in may have the server run the CDM licensing."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError, APIErrorCode

pytestmark = pytest.mark.unit

USERS = {
    "tier1key": {"username": "tier1"},
    "tier3key": {"username": "tier3", "server_cdm": True},
    "tier2key": {"username": "tier2", "server_cdm": ["example1"]},
    "tier2strkey": {"username": "tier2str", "server_cdm": "example1"},
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


def request(key):
    return SimpleNamespace(headers={"X-Secret-Key": key} if key else {})


@pytest.mark.parametrize(("key", "allowed"), ENTITLEMENTS)
def test_server_cdm_allowed(serve_users, key, allowed):
    assert handlers.server_cdm_allowed(request(key)) is allowed


@pytest.mark.parametrize(("service", "allowed"), [("EXAMPLE1", True), ("EXAMPLE2", False), (None, False)])
def test_server_cdm_service_list(serve_users, service, allowed):
    assert handlers.server_cdm_allowed(request("tier2key"), service) is allowed


@pytest.mark.parametrize(("service", "allowed"), [("EXAMPLE1", True), ("EXAMPLE2", False)])
def test_server_cdm_bare_tag(serve_users, service, allowed):
    # A bare tag gates one service. It must not read as a truthy value that opens every service.
    assert handlers.server_cdm_allowed(request("tier2strkey"), service) is allowed


def test_download_gate_uses_job_service(serve_users):
    handlers.enforce_download_gates({"service": "EXAMPLE1"}, request("tier2key"))
    with pytest.raises(APIError):
        handlers.enforce_download_gates({"service": "EXAMPLE2"}, request("tier2key"))


@pytest.mark.parametrize(("service", "allowed"), [("EXAMPLE1", True), ("EXAMPLE2", False)])
async def test_retry_gate_uses_job_service(serve_users, monkeypatch, service, allowed):
    # The job keeps its tag outside `parameters`, so the retry gate must add it back.
    from unshackle.core.api import download_manager as dm

    job = SimpleNamespace(service=service, title_id="t1", parameters={}, status=dm.JobStatus.FAILED)
    new_job = SimpleNamespace(job_id="new", status=dm.JobStatus.QUEUED, created_time=datetime.now())

    async def start_workers():
        return None

    manager = SimpleNamespace(
        get_job=lambda job_id: job,
        start_workers=start_workers,
        create_job=lambda *args, **kwargs: new_job,
    )
    monkeypatch.setattr(dm, "get_download_manager", lambda: manager)
    monkeypatch.setattr(handlers, "owns_job", lambda job, request: True)
    monkeypatch.setattr(handlers, "validate_service", lambda tag, request=None: tag)

    if allowed:
        response = await handlers.retry_download_job_handler("job1", request("tier2key"))
        assert response.status == 202
    else:
        with pytest.raises(APIError) as ei:
            await handlers.retry_download_job_handler("job1", request("tier2key"))
        assert ei.value.error_code == APIErrorCode.FORBIDDEN


def test_no_request_allows(serve_users):
    assert handlers.server_cdm_allowed(None) is True


@pytest.mark.parametrize(("key", "allowed"), ENTITLEMENTS)
def test_download_gate_follows_entitlement(serve_users, key, allowed):
    # A download job licenses with the server CDM, so submission obeys the same gate.
    if allowed:
        handlers.enforce_download_gates({}, request(key))
    else:
        with pytest.raises(APIError) as ei:
            handlers.enforce_download_gates({}, request(key))
        assert ei.value.error_code == APIErrorCode.FORBIDDEN


def test_download_gate_without_request(serve_users):
    handlers.enforce_download_gates({})
