"""Session utilities for creating HTTP sessions with different backends."""

from __future__ import annotations

import logging
import random
import time
import warnings
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from curl_cffi.requests import Response, Session, exceptions

from unshackle.core.config import config

# Globally suppress curl_cffi HTTPS proxy warnings since some proxy providers
# (like NordVPN) require HTTPS URLs but curl_cffi expects HTTP format
warnings.filterwarnings(
    "ignore", message="Make sure you are using https over https proxy.*", category=RuntimeWarning, module="curl_cffi.*"
)

FINGERPRINT_PRESETS = {
    "okhttp4": {
        "ja3": (
            "771,"  # TLS 1.2
            "4865-4866-4867-49195-49196-52393-49199-49200-52392-49171-49172-156-157-47-53,"  # Ciphers
            "0-23-65281-10-11-35-16-5-13-51-45-43-21,"  # Extensions
            "29-23-24,"  # Named groups (x25519, secp256r1, secp384r1)
            "0"  # EC point formats
        ),
        "akamai": "4:16777216|16711681|0|m,p,a,s",
        "description": "OkHttp 3.x/4.x (BoringSSL TLS stack)",
    },
    "okhttp5": {
        "ja3": (
            "771,"  # TLS 1.2
            "4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,"  # Ciphers
            "0-23-65281-10-11-35-16-5-13-51-45-43-21,"  # Extensions
            "29-23-24,"  # Named groups (x25519, secp256r1, secp384r1)
            "0"  # EC point formats
        ),
        "akamai": "4:16777216|16711681|0|m,p,a,s",
        "description": "OkHttp 5.x (BoringSSL TLS stack)",
    },
}


class MaxRetriesError(exceptions.RequestException):
    def __init__(self, message, cause=None):
        super().__init__(message)
        self.__cause__ = cause


class CurlSession(Session):
    def __init__(
        self,
        max_retries: int = 10,
        backoff_factor: float = 0.2,
        max_backoff: float = 60.0,
        status_forcelist: list[int] | None = None,
        allowed_methods: set[str] | None = None,
        catch_exceptions: tuple[type[Exception], ...] | None = None,
        **session_kwargs: Any,
    ):
        super().__init__(**session_kwargs)

        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.max_backoff = max_backoff
        self.status_forcelist = status_forcelist or [429, 500, 502, 503, 504]
        self.allowed_methods = allowed_methods or {"GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"}
        self.catch_exceptions = catch_exceptions or (
            exceptions.ConnectionError,
            exceptions.ProxyError,
            exceptions.SSLError,
            exceptions.Timeout,
        )
        self.log = logging.getLogger(self.__class__.__name__)

    def _get_sleep_time(self, response: Response | None, attempt: int) -> float | None:
        if response:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return float(retry_after)
                except ValueError:
                    if retry_date := parsedate_to_datetime(retry_after):
                        return (retry_date - datetime.now(timezone.utc)).total_seconds()

        if attempt == 0:
            return 0.0

        backoff_value = self.backoff_factor * (2 ** (attempt - 1))
        jitter = backoff_value * 0.1
        sleep_time = backoff_value + random.uniform(-jitter, jitter)
        return min(sleep_time, self.max_backoff)

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        if method.upper() not in self.allowed_methods:
            return super().request(method, url, **kwargs)

        last_exception = None
        response = None

        for attempt in range(self.max_retries + 1):
            try:
                response = super().request(method, url, **kwargs)
                if response.status_code not in self.status_forcelist:
                    return response
                last_exception = exceptions.HTTPError(f"Received status code: {response.status_code}")
                self.log.warning(
                    f"{response.status_code} {response.reason}({urlparse(url).path}). Retrying... "
                    f"({attempt + 1}/{self.max_retries})"
                )

            except self.catch_exceptions as e:
                last_exception = e
                response = None
                self.log.warning(
                    f"{e.__class__.__name__}({urlparse(url).path}). Retrying... ({attempt + 1}/{self.max_retries})"
                )

            if attempt < self.max_retries:
                if sleep_duration := self._get_sleep_time(response, attempt + 1):
                    if sleep_duration > 0:
                        time.sleep(sleep_duration)
            else:
                break

        raise MaxRetriesError(f"Max retries exceeded for {method} {url}", cause=last_exception)


def session(
    browser: str | None = None,
    ja3: str | None = None,
    akamai: str | None = None,
    extra_fp: dict | None = None,
    **kwargs,
) -> CurlSession:
    """
    Create a curl_cffi session that impersonates a browser or custom TLS/HTTP fingerprint.

    This is a full replacement for requests.Session with browser impersonation
    and anti-bot capabilities. The session uses curl-impersonate under the hood
    to mimic real browser behavior.

    Args:
        browser: Browser to impersonate (e.g. "chrome124", "firefox", "safari") OR
                 fingerprint preset name (e.g. "okhttp4").
                 Uses the configured default from curl_impersonate.browser if not specified.
                 Available presets: okhttp4
                 See https://github.com/lexiforest/curl_cffi#sessions for browser options.
        ja3: Custom JA3 TLS fingerprint string (format: "SSLVersion,Ciphers,Extensions,Curves,PointFormats").
             When provided, curl_cffi will use this exact TLS fingerprint instead of the browser's default.
             See https://curl-cffi.readthedocs.io/en/latest/impersonate/customize.html
        akamai: Custom Akamai HTTP/2 fingerprint string (format: "SETTINGS|WINDOW_UPDATE|PRIORITY|PSEUDO_HEADERS").
                When provided, curl_cffi will use this exact HTTP/2 fingerprint instead of the browser's default.
                See https://curl-cffi.readthedocs.io/en/latest/impersonate/customize.html
        extra_fp: Additional fingerprint parameters dict for advanced customization.
                  See https://curl-cffi.readthedocs.io/en/latest/impersonate/customize.html
        **kwargs: Additional arguments passed to CurlSession constructor:
                  - headers: Additional headers (dict)
                  - cookies: Cookie jar or dict
                  - auth: HTTP basic auth tuple (username, password)
                  - proxies: Proxy configuration dict
                  - verify: SSL certificate verification (bool, default True)
                  - timeout: Request timeout in seconds (float or tuple)
                  - allow_redirects: Follow redirects (bool, default True)
                  - max_redirects: Maximum redirect count (int)
                  - cert: Client certificate (str or tuple)

                  Extra arguments for retry handler:
                  - max_retries: Maximum number of retries (int, default 10)
                  - backoff_factor: Backoff factor (float, default 0.2)
                  - max_backoff: Maximum backoff time (float, default 60.0)
                  - status_forcelist: List of status codes to force retry (list, default [429, 500, 502, 503, 504])
                  - allowed_methods: List of allowed HTTP methods (set, default {"GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})
                  - catch_exceptions: List of exceptions to catch (tuple, default (exceptions.ConnectionError, exceptions.ProxyError, exceptions.SSLError, exceptions.Timeout))

    Returns:
        curl_cffi.requests.Session configured with browser impersonation or custom fingerprints,
        common headers, and equivalent retry behavior to requests.Session.

    Examples:
        # Standard browser impersonation
        from unshackle.core.session import session

        class MyService(Service):
            @staticmethod
            def get_session():
                return session()  # Uses config default browser

        # Use OkHttp 4.x preset for Android TV
        class AndroidService(Service):
            @staticmethod
            def get_session():
                return session("okhttp4")

        # Custom fingerprint (manual)
        class CustomService(Service):
            @staticmethod
            def get_session():
                return session(
                    ja3="771,4865-4866-4867-49195...",
                    akamai="1:65536;2:0;4:6291456;6:262144|15663105|0|m,a,s,p",
                )

        # With retry configuration
        class MyService(Service):
            @staticmethod
            def get_session():
                return session(
                    "okhttp4",
                    max_retries=5,
                    status_forcelist=[429, 500],
                    allowed_methods={"GET", "HEAD", "OPTIONS"},
                )
    """

    if browser and browser in FINGERPRINT_PRESETS:
        preset = FINGERPRINT_PRESETS[browser]
        if ja3 is None:
            ja3 = preset.get("ja3")
        if akamai is None:
            akamai = preset.get("akamai")
        if extra_fp is None:
            extra_fp = preset.get("extra_fp")
        browser = None

    if browser is None and ja3 is None and akamai is None:
        browser = config.curl_impersonate.get("browser", "chrome")

    session_config = {}
    if browser:
        session_config["impersonate"] = browser

    if ja3:
        session_config["ja3"] = ja3
    if akamai:
        session_config["akamai"] = akamai
    if extra_fp:
        session_config["extra_fp"] = extra_fp

    session_config.update(kwargs)

    session_obj = CurlSession(**session_config)
    session_obj.headers.update(config.headers)
    return session_obj
