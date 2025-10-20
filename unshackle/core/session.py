"""Session utilities for creating HTTP sessions with different backends."""

from __future__ import annotations

import logging
import random
import time
import warnings
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, List, Optional, Set, Tuple
from urllib.parse import urlparse

from curl_cffi import Response, Session, exceptions

from unshackle.core.config import config

# Globally suppress curl_cffi HTTPS proxy warnings since some proxy providers
# (like NordVPN) require HTTPS URLs but curl_cffi expects HTTP format
warnings.filterwarnings(
    "ignore", message="Make sure you are using https over https proxy.*", category=RuntimeWarning, module="curl_cffi.*"
)


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
        status_forcelist: Optional[List[int]] = None,
        allowed_methods: Optional[Set[str]] = None,
        catch_exceptions: Optional[Tuple[type[Exception], ...]] = None,
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
                    f"{e.__class__.__name__}({urlparse(url).path}). Retrying... "
                    f"({attempt + 1}/{self.max_retries})"
                )

            if attempt < self.max_retries:
                if sleep_duration := self._get_sleep_time(response, attempt + 1):
                    if sleep_duration > 0:
                        time.sleep(sleep_duration)
            else:
                break

        raise MaxRetriesError(f"Max retries exceeded for {method} {url}", cause=last_exception)


def session(browser: str | None = None, **kwargs) -> CurlSession:
    """
    Create a curl_cffi session that impersonates a browser.

    This is a full replacement for requests.Session with browser impersonation
    and anti-bot capabilities. The session uses curl-impersonate under the hood
    to mimic real browser behavior.

    Args:
        browser: Browser to impersonate (e.g. "chrome124", "firefox", "safari").
                 Uses the configured default from curl_impersonate.browser if not specified.
                 See https://github.com/lexiforest/curl_cffi#sessions for available options.
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
                  - ja3: JA3 fingerprint (str)
                  - akamai: Akamai fingerprint (str)

                  Extra arguments for retry handler:
                  - max_retries: Maximum number of retries (int, default 10)
                  - backoff_factor: Backoff factor (float, default 0.2)
                  - max_backoff: Maximum backoff time (float, default 60.0)
                  - status_forcelist: List of status codes to force retry (list, default [429, 500, 502, 503, 504])
                  - allowed_methods: List of allowed HTTP methods (set, default {"GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})
                  - catch_exceptions: List of exceptions to catch (tuple, default (exceptions.ConnectionError, exceptions.SSLError, exceptions.Timeout))

    Returns:
        curl_cffi.requests.Session configured with browser impersonation, common headers,
        and equivalent retry behavior to requests.Session.

    Example:
        from unshackle.core.session import session as CurlSession

        class MyService(Service):
            @staticmethod
            def get_session() -> CurlSession:
                session = CurlSession(
                    impersonate="chrome",
                    ja3="...",
                    akamai="...",
                    max_retries=5,
                    status_forcelist=[429, 500],
                    allowed_methods={"GET", "HEAD", "OPTIONS"},
                )
                return session  # Uses config default browser
    """

    session_config = {
        "impersonate": browser or config.curl_impersonate.get("browser", "chrome"),
        **kwargs,
    }

    session_obj = CurlSession(**session_config)
    session_obj.headers.update(config.headers)
    return session_obj
