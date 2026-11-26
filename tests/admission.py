"""Test helper for the parameter check of download job admission."""

from typing import Any

from unshackle.core.api.errors import APIError
from unshackle.core.api.job_admission import check_parameters, check_plan
from unshackle.core.remote_protocol import APIErrorCode


def refusal(data: dict[str, Any]) -> str | None:
    """The message of the INVALID_PARAMETERS refusal that the parameter and plan checks give ``data``, else None."""
    try:
        check_plan(check_parameters(data))
    except APIError as e:
        assert e.error_code is APIErrorCode.INVALID_PARAMETERS
        return e.message
    return None
