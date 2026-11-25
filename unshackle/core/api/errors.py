"""
API Error Handling System

Provides structured error responses with error codes, categorization,
and optional debug information for the unshackle REST API.
"""

from __future__ import annotations

import secrets
import traceback
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from unshackle.core.remote_protocol import APIErrorCode
from unshackle.core.utils.redact import redact_secrets


class APIError(Exception):
    """
    Structured API error with error code, message, and details.

    Attributes:
        error_code: Standardized error code from APIErrorCode enum
        message: User-friendly error message
        details: Additional structured error information
        retryable: Whether the caller can do the operation again
        http_status: HTTP status code to return (default based on error_code)
    """

    def __init__(
        self,
        error_code: APIErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = details or {}
        self.retryable = retryable
        self.http_status = http_status or self.default_http_status(error_code)

    @staticmethod
    def default_http_status(error_code: APIErrorCode) -> int:
        """Map error codes to default HTTP status codes.

        No code maps to a 5xx: a CDN in front of the server can replace a 5xx body with its own page.
        Server-side failures are 422, and the ones worth a retry are 424, which the client retries.
        """
        status_map = {
            APIErrorCode.INVALID_INPUT: 400,
            APIErrorCode.INVALID_SERVICE: 400,
            APIErrorCode.INVALID_PROXY: 400,
            APIErrorCode.INVALID_PARAMETERS: 400,
            APIErrorCode.AUTH_FAILED: 401,
            APIErrorCode.FORBIDDEN: 403,
            APIErrorCode.SERVER_CDM_CAPPED: 403,
            APIErrorCode.GEOFENCE: 403,
            APIErrorCode.NOT_FOUND: 404,
            APIErrorCode.NO_CONTENT: 404,
            APIErrorCode.JOB_NOT_FOUND: 404,
            APIErrorCode.SESSION_NOT_FOUND: 404,
            APIErrorCode.TRACK_NOT_FOUND: 404,
            APIErrorCode.CONFLICT: 409,
            APIErrorCode.SERVER_DRAINING: 409,
            APIErrorCode.SERVICE_ERROR: 422,
            APIErrorCode.DRM_ERROR: 422,
            APIErrorCode.INTERNAL_ERROR: 422,
            APIErrorCode.DOWNLOAD_ERROR: 422,
            APIErrorCode.WORKER_ERROR: 422,
            APIErrorCode.NETWORK_ERROR: 424,
            APIErrorCode.SERVICE_UNAVAILABLE: 424,
            APIErrorCode.RATE_LIMITED: 429,
        }
        return status_map.get(error_code, 422)


def build_error_response(
    error: APIError | Exception,
    debug_mode: bool = False,
    extra_debug_info: dict[str, Any] | None = None,
) -> web.Response:
    """
    Assemble a structured JSON error response.

    Each error gets a short ``ref``, also appended to ``message`` so that every client prints it.
    The stats middleware logs the ref with the request, so a reported ref finds the server-side record.

    Args:
        error: APIError or generic Exception to convert to response
        debug_mode: Whether to include technical debug information
        extra_debug_info: Additional debug info such as stderr and stdout

    Returns:
        aiohttp JSON response with structured error data
    """
    if isinstance(error, APIError):
        error_code = error.error_code.value
        message = error.message
        details = error.details
        http_status = error.http_status
        retryable = error.retryable
    else:
        error_code = APIErrorCode.INTERNAL_ERROR.value
        message = str(error) or "An unexpected error occurred"
        details = {}
        http_status = APIError.default_http_status(APIErrorCode.INTERNAL_ERROR)
        retryable = False

    ref = secrets.token_hex(3)
    response_data: dict[str, Any] = {
        "status": "error",
        "error_code": error_code,
        "message": f"{message} (ref {ref})",
        "ref": ref,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    if details:
        response_data["details"] = details

    if retryable:
        response_data["retryable"] = True

    if debug_mode:
        debug_info: dict[str, Any] = {
            "exception_type": type(error).__name__,
        }

        if isinstance(error, Exception):
            debug_info["traceback"] = redact_secrets(traceback.format_exc())

        if extra_debug_info:
            debug_info.update(extra_debug_info)

        response_data["debug_info"] = debug_info

    response = web.json_response(response_data, status=http_status)
    response["error"] = {"code": error_code, "ref": ref, "message": message}
    return response


CATEGORIES: dict[APIErrorCode, tuple[str, str]] = {
    APIErrorCode.GEOFENCE: ("Content not available in your region", "geofence_restriction"),
    APIErrorCode.INVALID_PROXY: ("The proxy for this session failed", "proxy_error"),
    APIErrorCode.AUTH_FAILED: ("Authentication failed", "authentication_error"),
    APIErrorCode.NETWORK_ERROR: ("Network error occurred", "network_connectivity"),
    APIErrorCode.NOT_FOUND: ("Resource not found", "not_found"),
    APIErrorCode.RATE_LIMITED: ("Rate limit exceeded", "rate_limited"),
    APIErrorCode.DRM_ERROR: ("DRM error", "drm_failure"),
    APIErrorCode.SERVICE_UNAVAILABLE: ("Service temporarily unavailable", "service_unavailable"),
    APIErrorCode.INVALID_INPUT: ("Invalid input", "validation_failed"),
    APIErrorCode.INTERNAL_ERROR: ("An unexpected error occurred", ""),
}
"""The message prefix and ``details.reason`` of each code that ``categorize_exception`` gives."""

RETRYABLE = {APIErrorCode.NETWORK_ERROR, APIErrorCode.RATE_LIMITED, APIErrorCode.SERVICE_UNAVAILABLE}


def categorized(code: APIErrorCode, text: str, context: dict[str, Any]) -> APIError:
    """The APIError of ``code`` for an exception with message ``text``."""
    prefix, reason = CATEGORIES.get(code, ("", ""))
    text = redact_secrets(text) or ""
    return APIError(
        error_code=code,
        message=f"{prefix}: {text}" if prefix else text,
        details={**context, "reason": reason} if reason else context,
        retryable=code in RETRYABLE,
    )


def keyword_code(exc_str: str, root: BaseException) -> APIErrorCode:
    """Guess the code of an exception that names none, from the words of its message and its type.

    The fallback for exceptions from out-of-repo services; a core raiser names its code with ``coded``.
    """
    exc_type = type(root).__name__
    if any(
        keyword in exc_str
        for keyword in ["geofence", "geoblock", "geo-block", "geolocation", "region", "not available in", "territory"]
    ):
        return APIErrorCode.GEOFENCE
    if "proxy" in str(root).lower():
        return APIErrorCode.INVALID_PROXY
    if any(keyword in exc_str for keyword in ["auth", "login", "credential", "unauthorized", "forbidden", "token"]):
        return APIErrorCode.AUTH_FAILED
    if any(
        keyword in exc_str
        for keyword in [
            "connection",
            "timeout",
            "network",
            "unreachable",
            "socket",
            "dns",
            "resolve",
        ]
    ) or exc_type in ["ConnectionError", "TimeoutError", "URLError", "SSLError"]:
        return APIErrorCode.NETWORK_ERROR
    if any(keyword in exc_str for keyword in ["not found", "404", "does not exist", "invalid id"]):
        return APIErrorCode.NOT_FOUND
    if any(keyword in exc_str for keyword in ["rate limit", "too many requests", "429", "throttle"]):
        return APIErrorCode.RATE_LIMITED
    if any(keyword in exc_str for keyword in ["drm", "license", "widevine", "playready", "decrypt"]):
        return APIErrorCode.DRM_ERROR
    if any(keyword in exc_str for keyword in ["service unavailable", "503", "maintenance", "temporarily unavailable"]):
        return APIErrorCode.SERVICE_UNAVAILABLE
    if any(keyword in exc_str for keyword in ["invalid", "malformed", "validation"]) or exc_type in [
        "ValueError",
        "ValidationError",
    ]:
        return APIErrorCode.INVALID_INPUT
    return APIErrorCode.INTERNAL_ERROR


def categorize_exception(
    exc: BaseException,
    context: dict[str, Any] | None = None,
) -> APIError:
    """
    Categorize a generic exception into a structured APIError; an APIError is returned as it is.

    An exception that names its code (``coded``, or its cause's) gets that code. Any other gets the code
    that ``keyword_code`` guesses from its message.

    Args:
        exc: The exception to categorize
        context: Optional context such as the service tag and the operation type

    Returns:
        APIError with appropriate error code and details
    """
    if isinstance(exc, APIError):
        return exc
    context = context or {}
    if isinstance(exc, SystemExit):
        exited = f"Service {context['service']} exited" if context.get("service") else "Service exited"
        return APIError(
            error_code=APIErrorCode.SERVICE_ERROR,
            message=redact_secrets(getattr(exc, "message", None)) or exited,
            details={**context, "reason": "service_exit"},
            retryable=False,
        )
    root = exc.__cause__ or exc
    for carrier in (exc, root):
        if isinstance(code := getattr(carrier, "error_code", None), APIErrorCode):
            return categorized(code, str(carrier), context)
    code = keyword_code(f"{exc} {root}".lower(), root)
    return categorized(code, str(root if code == APIErrorCode.INVALID_PROXY else exc), context)


def handle_api_exception(
    exc: BaseException,
    context: dict[str, Any] | None = None,
    debug_mode: bool = False,
    extra_debug_info: dict[str, Any] | None = None,
) -> web.Response:
    """
    Convenience function to categorize an exception and assemble an error response.

    Args:
        exc: The exception to categorize
        context: Optional context information
        debug_mode: Whether to include debug information
        extra_debug_info: Additional debug info

    Returns:
        Structured JSON error response
    """
    return build_error_response(categorize_exception(exc, context), debug_mode, extra_debug_info)
