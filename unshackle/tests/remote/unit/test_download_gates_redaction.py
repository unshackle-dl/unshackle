"""Unit tests for the /api/download security gates (per-request CDM + credential overrides)
and the secret redaction applied to job parameters and error/stderr fields."""

from __future__ import annotations

from datetime import datetime

import pytest
from aiohttp import web

from unshackle.core.api import handlers
from unshackle.core.api.download_manager import (DownloadJob, JobStatus, _redact_parameters, _redact_text,
                                                 _secret_values)
from unshackle.core.api.errors import APIError, APIErrorCode

pytestmark = pytest.mark.unit


# ---------- redaction ----------


def test_redact_parameters_masks_secrets_and_proxy_userinfo():
    params = {
        "service": "ATV",
        "credential": "user:hunter2",
        "password": "pw",
        "token": "tok",
        "api_key": "ak",
        "proxy": "http://bob:secret@proxy.example:8080",
        "quality": "1080p",
    }
    red = _redact_parameters(params)
    assert red["credential"] == "***"
    assert red["password"] == "***"
    assert red["token"] == "***"
    assert red["api_key"] == "***"
    assert red["proxy"] == "http://***@proxy.example:8080"
    assert red["quality"] == "1080p"  # non-secret left intact
    assert params["credential"] == "user:hunter2"  # original dict not mutated


def test_redact_parameters_masks_credentials_dict():
    assert _redact_parameters({"credentials": {"default": "u:p"}})["credentials"] == "***"


def test_secret_values_includes_password_half_and_dict_values():
    secrets = _secret_values({"credential": "user:hunter2", "credentials": {"d": "alice:wonder"}})
    assert "user:hunter2" in secrets  # full credential
    assert "hunter2" in secrets  # password half of user:pass
    assert "alice:wonder" in secrets  # value from the credentials map


def test_redact_text_scrubs_credential_and_proxy_from_free_text():
    params = {"credential": "user:hunter2", "proxy": "http://bob:secret@p:1"}
    out = _redact_text("auth failed for user:hunter2 via http://bob:secret@p:1", params)
    assert "hunter2" not in out
    assert "bob:secret@" not in out
    assert "***" in out


def test_redact_text_passthrough_without_secrets():
    assert _redact_text("plain error", {}) == "plain error"
    assert _redact_text(None, {}) is None


def test_to_dict_full_details_redacts_error_fields_and_parameters():
    job = DownloadJob(
        job_id="j1",
        status=JobStatus.FAILED,
        created_time=datetime(2026, 1, 1),
        service="ATV",
        title_id="t",
        parameters={"credential": "user:hunter2"},
    )
    job.error_message = "login failed for user:hunter2"
    job.worker_stderr = "Traceback ... user:hunter2 ..."
    d = job.to_dict(include_full_details=True)
    assert "hunter2" not in d["error_message"]
    assert "hunter2" not in d["worker_stderr"]
    assert d["parameters"]["credential"] == "***"


# ---------- gates ----------


class _PastGate(Exception):
    """Raised by the stubbed Services.load to prove a request got past the gate into the try block."""


@pytest.fixture
def stub_handler(monkeypatch):
    """Make the service valid and make the first call after the gate (Services.load) explode, so a
    forbidden request raises APIError *before* the try block and an allowed one is caught inside it."""
    monkeypatch.setattr(handlers, "validate_service", lambda tag, request=None: tag)

    def _boom(*_args, **_kwargs):
        raise _PastGate()

    monkeypatch.setattr(handlers.Services, "load", _boom)
    return monkeypatch


async def test_cdm_override_forbidden_by_default(stub_handler):
    stub_handler.setattr(handlers.config, "serve", {})
    with pytest.raises(APIError) as ei:
        await handlers.download_handler({"service": "ATV", "title_id": "t", "cdm": "dev"})
    assert ei.value.error_code == APIErrorCode.FORBIDDEN


async def test_cdm_override_allowed_when_enabled(stub_handler):
    stub_handler.setattr(handlers.config, "serve", {"cdm_overrides": True})
    # passing the gate reaches the stubbed Services.load, whose error is caught and returned as a response
    resp = await handlers.download_handler({"service": "ATV", "title_id": "t", "cdm": "dev"})
    assert isinstance(resp, web.Response)


async def test_cdm_override_allowlist_permits_only_named_device(stub_handler):
    stub_handler.setattr(handlers.config, "serve", {"cdm_overrides": ["good"]})
    assert isinstance(
        await handlers.download_handler({"service": "ATV", "title_id": "t", "cdm": "good"}), web.Response
    )
    with pytest.raises(APIError) as ei:
        await handlers.download_handler({"service": "ATV", "title_id": "t", "cdm": "other"})
    assert ei.value.error_code == APIErrorCode.FORBIDDEN


async def test_credential_forbidden_by_default(stub_handler):
    stub_handler.setattr(handlers.config, "serve", {})
    with pytest.raises(APIError) as ei:
        await handlers.download_handler({"service": "ATV", "title_id": "t", "credential": "u:p"})
    assert ei.value.error_code == APIErrorCode.FORBIDDEN


async def test_credential_allowed_when_enabled(stub_handler):
    stub_handler.setattr(handlers.config, "serve", {"allow_job_credentials": True})
    resp = await handlers.download_handler({"service": "ATV", "title_id": "t", "credential": "u:p"})
    assert isinstance(resp, web.Response)


# ---------- range validation ----------


def test_range_validation_accepts_hdr10p_and_alias():
    # canonical "HDR10P" and back-compat "HDR10+" both pass; mixed casing too
    assert handlers.validate_download_parameters({"range": ["HDR10P", "DV", "SDR"]}) is None
    assert handlers.validate_download_parameters({"range": ["hdr10+"]}) is None
    assert handlers.validate_download_parameters({"range": "HYBRID"}) is None


def test_range_validation_rejects_unknown_and_lists_hdr10p():
    err = handlers.validate_download_parameters({"range": ["HDR99"]})
    assert err and "HDR10P" in err and "HDR99" in err
