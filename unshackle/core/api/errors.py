"""
API Error Handling System

Provides structured error responses with error codes, categorization,
and optional debug information for the unshackle REST API.
"""

from __future__ import annotations

import traceback
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from aiohttp import web


class APIErrorCode(str, Enum):
    """Standard API error codes for programmatic error handling."""

    INVALID_INPUT = "INVALID_INPUT"
    INVALID_SERVICE = "INVALID_SERVICE"
    INVALID_PROXY = "INVALID_PROXY"
    INVALID_PARAMETERS = "INVALID_PARAMETERS"

    AUTH_FAILED = "AUTH_FAILED"
    FORBIDDEN = "FORBIDDEN"
    GEOFENCE = "GEOFENCE"

    NOT_FOUND = "NOT_FOUND"
    NO_CONTENT = "NO_CONTENT"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    TRACK_NOT_FOUND = "TRACK_NOT_FOUND"

    CONFLICT = "CONFLICT"

    RATE_LIMITED = "RATE_LIMITED"

    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_ERROR = "SERVICE_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    DRM_ERROR = "DRM_ERROR"
    DOWNLOAD_ERROR = "DOWNLOAD_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    WORKER_ERROR = "WORKER_ERROR"


class APIError(Exception):
    """
    Structured API error with error code, message, and details.

    Attributes:
        error_code: Standardized error code from APIErrorCode enum
        message: User-friendly error message
        details: Additional structured error information
        retryable: Whether the operation can be retried
        http_status: HTTP status code to return (default based on error_code)
    """

    def __init__(
        self,
        error_code: APIErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = details or {}
        self.retryable = retryable
        self.http_status = http_status or self.default_http_status(error_code)

    @staticmethod
    def default_http_status(error_code: APIErrorCode) -> int:
        """Map error codes to default HTTP status codes."""
        status_map = {
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
            APIErrorCode.CONFLICT: 409,
            APIErrorCode.RATE_LIMITED: 429,
            APIErrorCode.INTERNAL_ERROR: 500,
            APIErrorCode.SERVICE_ERROR: 502,
            APIErrorCode.DRM_ERROR: 502,
            APIErrorCode.NETWORK_ERROR: 503,
            APIErrorCode.SERVICE_UNAVAILABLE: 503,
            APIErrorCode.DOWNLOAD_ERROR: 500,
            APIErrorCode.WORKER_ERROR: 500,
        }
        return status_map.get(error_code, 500)


def build_error_response(
    error: APIError | Exception,
    debug_mode: bool = False,
    extra_debug_info: dict[str, Any] | None = None,
) -> web.Response:
    """
    Build a structured JSON error response.

    Args:
        error: APIError or generic Exception to convert to response
        debug_mode: Whether to include technical debug information
        extra_debug_info: Additional debug info (stderr, stdout, etc.)

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
        http_status = 500
        retryable = False

    response_data: dict[str, Any] = {
        "status": "error",
        "error_code": error_code,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
            debug_info["traceback"] = traceback.format_exc()

        if extra_debug_info:
            debug_info.update(extra_debug_info)

        response_data["debug_info"] = debug_info

    return web.json_response(response_data, status=http_status)


def categorize_exception(
    exc: Exception,
    context: dict[str, Any] | None = None,
) -> APIError:
    """
    Categorize a generic exception into a structured APIError.

    This function attempts to identify the type of error based on the exception
    type, message patterns, and optional context information.

    Args:
        exc: The exception to categorize
        context: Optional context (service name, operation type, etc.)

    Returns:
        APIError with appropriate error code and details
    """
    context = context or {}
    exc_str = str(exc).lower()
    exc_type = type(exc).__name__

    if any(keyword in exc_str for keyword in ["auth", "login", "credential", "unauthorized", "forbidden", "token"]):
        return APIError(
            error_code=APIErrorCode.AUTH_FAILED,
            message=f"Authentication failed: {exc}",
            details={**context, "reason": "authentication_error"},
            retryable=False,
        )

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
        return APIError(
            error_code=APIErrorCode.NETWORK_ERROR,
            message=f"Network error occurred: {exc}",
            details={**context, "reason": "network_connectivity"},
            retryable=True,
            http_status=503,
        )

    if any(keyword in exc_str for keyword in ["geofence", "region", "not available in", "territory"]):
        return APIError(
            error_code=APIErrorCode.GEOFENCE,
            message=f"Content not available in your region: {exc}",
            details={**context, "reason": "geofence_restriction"},
            retryable=False,
        )

    if any(keyword in exc_str for keyword in ["not found", "404", "does not exist", "invalid id"]):
        return APIError(
            error_code=APIErrorCode.NOT_FOUND,
            message=f"Resource not found: {exc}",
            details={**context, "reason": "not_found"},
            retryable=False,
        )

    if any(keyword in exc_str for keyword in ["rate limit", "too many requests", "429", "throttle"]):
        return APIError(
            error_code=APIErrorCode.RATE_LIMITED,
            message=f"Rate limit exceeded: {exc}",
            details={**context, "reason": "rate_limited"},
            retryable=True,
            http_status=429,
        )

    if any(keyword in exc_str for keyword in ["drm", "license", "widevine", "playready", "decrypt"]):
        return APIError(
            error_code=APIErrorCode.DRM_ERROR,
            message=f"DRM error: {exc}",
            details={**context, "reason": "drm_failure"},
            retryable=False,
        )

    if any(keyword in exc_str for keyword in ["service unavailable", "503", "maintenance", "temporarily unavailable"]):
        return APIError(
            error_code=APIErrorCode.SERVICE_UNAVAILABLE,
            message=f"Service temporarily unavailable: {exc}",
            details={**context, "reason": "service_unavailable"},
            retryable=True,
            http_status=503,
        )

    if any(keyword in exc_str for keyword in ["invalid", "malformed", "validation"]) or exc_type in [
        "ValueError",
        "ValidationError",
    ]:
        return APIError(
            error_code=APIErrorCode.INVALID_INPUT,
            message=f"Invalid input: {exc}",
            details={**context, "reason": "validation_failed"},
            retryable=False,
        )

    return APIError(
        error_code=APIErrorCode.INTERNAL_ERROR,
        message=f"An unexpected error occurred: {exc}",
        details={**context, "exception_type": exc_type},
        retryable=False,
    )


def handle_api_exception(
    exc: Exception,
    context: dict[str, Any] | None = None,
    debug_mode: bool = False,
    extra_debug_info: dict[str, Any] | None = None,
) -> web.Response:
    """
    Convenience function to categorize an exception and build an error response.

    Args:
        exc: The exception to handle
        context: Optional context information
        debug_mode: Whether to include debug information
        extra_debug_info: Additional debug info

    Returns:
        Structured JSON error response
    """
    if isinstance(exc, APIError):
        api_error = exc
    else:
        api_error = categorize_exception(exc, context)

    return build_error_response(api_error, debug_mode, extra_debug_info)
