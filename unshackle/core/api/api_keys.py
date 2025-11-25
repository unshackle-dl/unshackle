"""API key tier management for remote services."""

import logging
from typing import Any, Dict, List, Optional

from aiohttp import web

log = logging.getLogger("api.keys")


def get_api_key_from_request(request: web.Request) -> Optional[str]:
    """
    Extract API key from request headers.

    Args:
        request: aiohttp request object

    Returns:
        API key string or None
    """
    return request.headers.get("X-API-Key") or request.headers.get("Authorization", "").replace("Bearer ", "")


def get_api_key_config(app: web.Application, api_key: str) -> Optional[Dict[str, Any]]:
    """
    Get configuration for a specific API key.

    Args:
        app: aiohttp application
        api_key: API key to look up

    Returns:
        API key configuration dict or None if not found
    """
    config = app.get("config", {})

    # Check new-style tiered API keys
    api_keys = config.get("api_keys", [])
    for key_config in api_keys:
        if isinstance(key_config, dict) and key_config.get("key") == api_key:
            return key_config

    # Check legacy users list (backward compatibility)
    users = config.get("users", [])
    if api_key in users:
        return {
            "key": api_key,
            "tier": "basic",
            "allowed_cdms": []
        }

    return None


def is_premium_user(app: web.Application, api_key: str) -> bool:
    """
    Check if an API key belongs to a premium user.

    Premium users can use server-side CDM for decryption.

    Args:
        app: aiohttp application
        api_key: API key to check

    Returns:
        True if premium user, False otherwise
    """
    key_config = get_api_key_config(app, api_key)
    if not key_config:
        return False

    tier = key_config.get("tier", "basic")
    return tier == "premium"


def get_allowed_cdms(app: web.Application, api_key: str) -> List[str]:
    """
    Get list of CDMs that an API key is allowed to use.

    Args:
        app: aiohttp application
        api_key: API key to check

    Returns:
        List of allowed CDM names, or empty list if not premium
    """
    key_config = get_api_key_config(app, api_key)
    if not key_config:
        return []

    allowed_cdms = key_config.get("allowed_cdms", [])

    # Handle wildcard
    if allowed_cdms == "*" or allowed_cdms == ["*"]:
        return ["*"]

    return allowed_cdms if isinstance(allowed_cdms, list) else []


def get_default_cdm(app: web.Application, api_key: str) -> Optional[str]:
    """
    Get default CDM for an API key.

    Args:
        app: aiohttp application
        api_key: API key to check

    Returns:
        Default CDM name or None
    """
    key_config = get_api_key_config(app, api_key)
    if not key_config:
        return None

    return key_config.get("default_cdm")


def can_use_cdm(app: web.Application, api_key: str, cdm_name: str) -> bool:
    """
    Check if an API key can use a specific CDM.

    Args:
        app: aiohttp application
        api_key: API key to check
        cdm_name: CDM name to check access for

    Returns:
        True if allowed, False otherwise
    """
    allowed_cdms = get_allowed_cdms(app, api_key)

    # Wildcard access
    if "*" in allowed_cdms:
        return True

    # Specific CDM access
    return cdm_name in allowed_cdms
