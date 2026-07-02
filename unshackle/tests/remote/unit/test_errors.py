"""Unit tests for unshackle.core.api.errors."""

from __future__ import annotations

import json

import pytest

from unshackle.core.api.errors import (APIError, APIErrorCode, build_error_response, categorize_exception,
                                       handle_api_exception)

pytestmark = pytest.mark.unit


def _body(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


def test_api_error_default_http_status_per_code() -> None:
    # Every live APIErrorCode and the status a client observes for it. This is the
    # status half of the error contract PRD 05 must preserve through the collapse.
    cases = {
        APIErrorCode.INVALID_INPUT: 400,
        APIErrorCode.INVALID_SERVICE: 400,
        APIErrorCode.INVALID_PROXY: 400,
        APIErrorCode.INVALID_PARAMETERS: 400,
        APIErrorCode.AUTH_FAILED: 401,
        APIErrorCode.FORBIDDEN: 403,
        APIErrorCode.GEOFENCE: 403,
        APIErrorCode.NOT_FOUND: 404,
        APIErrorCode.NO_CONTENT: 404,
        APIErrorCode.JOB_NOT_FOUND: 404,
        APIErrorCode.SESSION_NOT_FOUND: 404,
        APIErrorCode.TRACK_NOT_FOUND: 404,
        APIErrorCode.RATE_LIMITED: 429,
        APIErrorCode.INTERNAL_ERROR: 500,
        APIErrorCode.DOWNLOAD_ERROR: 500,
        APIErrorCode.WORKER_ERROR: 500,
        APIErrorCode.SERVICE_ERROR: 502,
        APIErrorCode.DRM_ERROR: 502,
        APIErrorCode.NETWORK_ERROR: 503,
        APIErrorCode.SERVICE_UNAVAILABLE: 503,
    }
    # Lock that the cases above are the *complete* live set — a new/removed code trips this.
    assert {c.name for c in cases} == {c.name for c in APIErrorCode}
    for code, expected in cases.items():
        assert APIError(code, "x").http_status == expected, code


def test_api_error_explicit_http_status_overrides_default() -> None:
    err = APIError(APIErrorCode.INVALID_INPUT, "x", http_status=418)
    assert err.http_status == 418


def test_build_error_response_from_api_error() -> None:
    err = APIError(
        APIErrorCode.SESSION_NOT_FOUND,
        "no such session",
        details={"session_id": "abc"},
        retryable=False,
    )
    resp = build_error_response(err)
    assert resp.status == 404
    body = _body(resp)
    assert body["status"] == "error"
    assert body["error_code"] == "SESSION_NOT_FOUND"
    assert body["message"] == "no such session"
    assert body["details"] == {"session_id": "abc"}
    assert "retryable" not in body
    assert "debug_info" not in body
    assert "timestamp" in body


def test_build_error_response_retryable_flag() -> None:
    err = APIError(APIErrorCode.NETWORK_ERROR, "boom", retryable=True)
    body = _body(build_error_response(err))
    assert body["retryable"] is True


def test_build_error_response_from_generic_exception() -> None:
    resp = build_error_response(RuntimeError("oops"))
    assert resp.status == 500
    body = _body(resp)
    assert body["error_code"] == "INTERNAL_ERROR"
    assert body["message"] == "oops"


def test_build_error_response_debug_mode_includes_traceback() -> None:
    try:
        raise ValueError("kaboom")
    except ValueError as e:
        resp = build_error_response(e, debug_mode=True, extra_debug_info={"foo": "bar"})
    body = _body(resp)
    assert body["debug_info"]["exception_type"] == "ValueError"
    assert "traceback" in body["debug_info"]
    assert body["debug_info"]["foo"] == "bar"


@pytest.mark.parametrize(
    "exc, expected_code",
    [
        (Exception("Invalid credentials provided"), APIErrorCode.AUTH_FAILED),
        (Exception("Connection refused"), APIErrorCode.NETWORK_ERROR),
        (TimeoutError("read timeout"), APIErrorCode.NETWORK_ERROR),
        (Exception("Not available in your region"), APIErrorCode.GEOFENCE),
        (Exception("Title not found"), APIErrorCode.NOT_FOUND),
        (Exception("HTTP 429 too many requests"), APIErrorCode.RATE_LIMITED),
        (Exception("DRM license fetch failed"), APIErrorCode.DRM_ERROR),
        (Exception("503 service unavailable"), APIErrorCode.SERVICE_UNAVAILABLE),
        (ValueError("malformed body"), APIErrorCode.INVALID_INPUT),
        (RuntimeError("totally novel failure xyz"), APIErrorCode.INTERNAL_ERROR),
    ],
)
def test_categorize_exception(exc: Exception, expected_code: APIErrorCode) -> None:
    api_err = categorize_exception(exc, context={"service": "ATV"})
    assert api_err.error_code == expected_code
    assert api_err.details.get("service") == "ATV"


def test_categorize_preserves_context() -> None:
    api_err = categorize_exception(ValueError("bad"), context={"op": "search"})
    assert api_err.details["op"] == "search"


def test_handle_api_exception_with_api_error_preserves_code() -> None:
    err = APIError(APIErrorCode.TRACK_NOT_FOUND, "no track")
    resp = handle_api_exception(err)
    body = _body(resp)
    assert body["error_code"] == "TRACK_NOT_FOUND"
    assert resp.status == 404


def test_handle_api_exception_categorizes_generic() -> None:
    resp = handle_api_exception(ConnectionError("oops"))
    body = _body(resp)
    assert body["error_code"] == "NETWORK_ERROR"


# --- Error contract harness (PRD 05 gate) ---------------------------------
# Locks the FULL observable response — (http status, error_code, retryable) — that
# a client sees for a generic (non-APIError) exception flowing through the
# handle_api_exception funnel that every route's `except Exception` path calls.
# This is the equivalence reference: after the categorize_exception collapse,
# any tuple that changes here is a client-visible behaviour change, not a refactor.
@pytest.mark.parametrize(
    "exc, status, code, retryable",
    [
        (Exception("Invalid credentials provided"), 401, "AUTH_FAILED", False),
        (Exception("Connection refused"), 503, "NETWORK_ERROR", True),
        (TimeoutError("read timeout"), 503, "NETWORK_ERROR", True),
        (Exception("Not available in your region"), 403, "GEOFENCE", False),
        (Exception("Title not found"), 404, "NOT_FOUND", False),
        (Exception("HTTP 429 too many requests"), 429, "RATE_LIMITED", True),
        (Exception("DRM license fetch failed"), 502, "DRM_ERROR", False),
        (Exception("503 service unavailable"), 503, "SERVICE_UNAVAILABLE", True),
        (ValueError("malformed body"), 400, "INVALID_INPUT", False),
        (RuntimeError("totally novel failure xyz"), 500, "INTERNAL_ERROR", False),
    ],
)
def test_error_contract_generic_exception(exc: Exception, status: int, code: str, retryable: bool) -> None:
    resp = handle_api_exception(exc)
    assert resp.status == status
    body = _body(resp)
    assert body["error_code"] == code
    # retryable is only serialized when True (build_error_response omits it otherwise).
    assert body.get("retryable", False) is retryable


def test_error_contract_apierror_passthrough_full_shape() -> None:
    # APIError carriers keep status + code + message + details + retryable verbatim.
    err = APIError(APIErrorCode.RATE_LIMITED, "slow down", details={"retry_after": 5}, retryable=True)
    resp = handle_api_exception(err)
    assert resp.status == 429
    body = _body(resp)
    assert body["error_code"] == "RATE_LIMITED"
    assert body["message"] == "slow down"
    assert body["details"] == {"retry_after": 5}
    assert body["retryable"] is True
