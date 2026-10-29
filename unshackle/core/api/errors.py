"""
API Error Handling System

Provides structured error responses with error codes, categorization,
and optional debug information for the unshackle REST API.
"""

from __future__ import annotations

import secrets
import traceback
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from aiohttp import web

from unshackle.core.utils.redact import redact_secrets


class APIErrorCode(str, Enum):
    """Standard API error codes for programmatic error handling."""

    INVALID_INPUT = "INVALID_INPUT"
    INVALID_SERVICE = "INVALID_SERVICE"
    INVALID_PROXY = "INVALID_PROXY"
    INVALID_PARAMETERS = "INVALID_PARAMETERS"

    AUTH_FAILED = "AUTH_FAILED"
    FORBIDDEN = "FORBIDDEN"
    SERVER_CDM_CAPPED = "SERVER_CDM_CAPPED"
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
    SERVER_DRAINING = "SERVER_DRAINING"
    WORKER_ERROR = "WORKER_ERROR"


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
    ):
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
            debug_info["traceback"] = redact_secrets(traceback.format_exc())

        if extra_debug_info:
            debug_info.update(extra_debug_info)

        response_data["debug_info"] = debug_info

    response = web.json_response(response_data, status=http_status)
    response["error"] = {"code": error_code, "ref": ref, "message": message}
    return response


def categorize_exception(
    exc: BaseException,
    context: dict[str, Any] | None = None,
) -> APIError:
    """
    Categorize a generic exception into a structured APIError.

    This function attempts to identify the type of error based on the exception
    type, message patterns, and optional context information.

    Args:
        exc: The exception to categorize
        context: Optional context such as the service tag and the operation type

    Returns:
        APIError with appropriate error code and details
    """
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
    exc_str = f"{exc} {root}".lower()
    exc_type = type(root).__name__

    if any(keyword in exc_str for keyword in ["geofence", "geoblock", "region", "not available in", "territory"]):
        return APIError(
            error_code=APIErrorCode.GEOFENCE,
            message=f"Content not available in your region: {redact_secrets(str(exc))}",
            details={**context, "reason": "geofence_restriction"},
            retryable=False,
        )

    if "proxy" in str(root).lower():
        return APIError(
            error_code=APIErrorCode.INVALID_PROXY,
            message=f"The proxy for this session failed: {redact_secrets(str(root))}",
            details={**context, "reason": "proxy_error"},
            retryable=False,
        )

    if any(keyword in exc_str for keyword in ["auth", "login", "credential", "unauthorized", "forbidden", "token"]):
        return APIError(
            error_code=APIErrorCode.AUTH_FAILED,
            message=f"Authentication failed: {redact_secrets(str(exc))}",
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
            message=f"Network error occurred: {redact_secrets(str(exc))}",
            details={**context, "reason": "network_connectivity"},
            retryable=True,
        )

    if any(keyword in exc_str for keyword in ["not found", "404", "does not exist", "invalid id"]):
        return APIError(
            error_code=APIErrorCode.NOT_FOUND,
            message=f"Resource not found: {redact_secrets(str(exc))}",
            details={**context, "reason": "not_found"},
            retryable=False,
        )

    if any(keyword in exc_str for keyword in ["rate limit", "too many requests", "429", "throttle"]):
        return APIError(
            error_code=APIErrorCode.RATE_LIMITED,
            message=f"Rate limit exceeded: {redact_secrets(str(exc))}",
            details={**context, "reason": "rate_limited"},
            retryable=True,
            http_status=429,
        )

    if any(keyword in exc_str for keyword in ["drm", "license", "widevine", "playready", "decrypt"]):
        return APIError(
            error_code=APIErrorCode.DRM_ERROR,
            message=f"DRM error: {redact_secrets(str(exc))}",
            details={**context, "reason": "drm_failure"},
            retryable=False,
        )

    if any(keyword in exc_str for keyword in ["service unavailable", "503", "maintenance", "temporarily unavailable"]):
        return APIError(
            error_code=APIErrorCode.SERVICE_UNAVAILABLE,
            message=f"Service temporarily unavailable: {redact_secrets(str(exc))}",
            details={**context, "reason": "service_unavailable"},
            retryable=True,
        )

    if any(keyword in exc_str for keyword in ["invalid", "malformed", "validation"]) or exc_type in [
        "ValueError",
        "ValidationError",
    ]:
        return APIError(
            error_code=APIErrorCode.INVALID_INPUT,
            message=f"Invalid input: {redact_secrets(str(exc))}",
            details={**context, "reason": "validation_failed"},
            retryable=False,
        )

    return APIError(
        error_code=APIErrorCode.INTERNAL_ERROR,
        message=f"An unexpected error occurred: {redact_secrets(str(exc))}",
        details={**context, "exception_type": exc_type},
        retryable=False,
    )


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
    if isinstance(exc, APIError):
        api_error = exc
    else:
        api_error = categorize_exception(exc, context)

    return build_error_response(api_error, debug_mode, extra_debug_info)
