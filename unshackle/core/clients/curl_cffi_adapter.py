"""
curl_cffi adapter integrated into unified HttpClient framework.
"""

from __future__ import annotations

import warnings
from typing import Any

from curl_cffi.requests import Session

from .base import BaseHttpClient, register

# Suppress noisy HTTPS proxy warning from curl_cffi
warnings.filterwarnings(
    "ignore",
    message="Make sure you are using https over https proxy.*",
    category=RuntimeWarning,
    module="curl_cffi.*",
)


# --------------------
# Fingerprint presets
# --------------------

FINGERPRINT_PRESETS = {
    "okhttp4": {
        "ja3": (
            "771,"
            "4865-4866-4867-49195-49196-52393-49199-49200-52392-49171-49172-156-157-47-53,"
            "0-23-65281-10-11-35-16-5-13-51-45-43,"
            "29-23-24,"
            "0"
        ),
        "akamai": "4:16777216|16711681|0|m,p,a,s",
    },
    "okhttp5": {
        "ja3": (
            "771,"
            "4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,"
            "0-23-65281-10-11-35-16-5-13-51-45-43,"
            "29-23-24,"
            "0"
        ),
        "akamai": "4:16777216|16711681|0|m,p,a,s",
    },
}


# --------------------
# Adapter
# --------------------

@register("curl_cffi")
class CurlCffiAdapter(BaseHttpClient):
    """
    curl_cffi backend adapter.

    Supports:
    - Browser impersonation
    - JA3 / Akamai fingerprinting
    - Extra fingerprint parameters
    - All unified retry logic from BaseHttpClient
    """

    def _build_client(self, config):
        options = dict(config.args)  # copy

        # Handle fingerprint preset
        browser = options.pop("browser", None)
        ja3 = options.pop("ja3", None)
        akamai = options.pop("akamai", None)
        extra_fp = options.pop("extra_fp", None)

        if browser and browser in FINGERPRINT_PRESETS:
            preset = FINGERPRINT_PRESETS[browser]
            ja3 = ja3 or preset.get("ja3")
            akamai = akamai or preset.get("akamai")
            browser = None

        session_kwargs: dict[str, Any] = {}

        if browser:
            session_kwargs["impersonate"] = browser

        if ja3:
            session_kwargs["ja3"] = ja3

        if akamai:
            session_kwargs["akamai"] = akamai

        if extra_fp:
            session_kwargs["extra_fp"] = extra_fp

        # Pass remaining adapter-specific options directly
        session_kwargs.update(options)

        session = Session(**session_kwargs)

        # Apply shared headers from HttpClientConfig
        session.headers.update(config.headers)

        return session
