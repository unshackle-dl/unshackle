"""Session utilities for creating HTTP sessions with TLS fingerprinting via rnet (Rust/BoringSSL)."""

from __future__ import annotations

import http
import logging
import random
import time
from collections.abc import Iterator, MutableMapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar
from typing import Any, Optional
from urllib.parse import urlencode, urlparse, urlunparse

import rnet
from requests import HTTPError, Request
from requests.structures import CaseInsensitiveDict

from unshackle.core.config import config

# ---------------------------------------------------------------------------
# Impersonate preset mapping — rnet uses named presets (no custom JA3/Akamai)
# ---------------------------------------------------------------------------

DEFAULT_IMPERSONATE = rnet.Impersonate.Chrome131


def _resolve_impersonate(browser: str) -> rnet.Impersonate:
    """Resolve a browser string to an rnet.Impersonate preset.

    Accepts exact rnet preset names (e.g. "Chrome131", "OkHttp4_12", "Edge101").
    See https://github.com/0x676e67/rnet for the full list of available presets.
    """
    preset = getattr(rnet.Impersonate, browser, None)
    if preset is not None:
        return preset
    raise ValueError(
        f"Unknown impersonate preset: {browser!r}. "
        f"Use exact rnet preset names like 'Chrome131', 'OkHttp4_12', 'Edge101'. "
        f"See rnet.Impersonate for all available presets."
    )

# Map string method names to rnet.Method enum
_METHOD_MAP: dict[str, rnet.Method] = {
    "GET": rnet.Method.GET,
    "POST": rnet.Method.POST,
    "PUT": rnet.Method.PUT,
    "DELETE": rnet.Method.DELETE,
    "HEAD": rnet.Method.HEAD,
    "OPTIONS": rnet.Method.OPTIONS,
    "PATCH": rnet.Method.PATCH,
    "TRACE": rnet.Method.TRACE,
}


# ---------------------------------------------------------------------------
# Response headers adapter — bytes → str
# ---------------------------------------------------------------------------


class RnetResponseHeaders(MutableMapping):
    """Read-only str-based view over rnet's bytes-based HeaderMap."""

    def __init__(self, header_map: Any) -> None:
        self._map = header_map

    def _decode(self, val: Any) -> str:
        return val.decode("utf-8", errors="replace") if isinstance(val, (bytes, bytearray)) else str(val)

    def __getitem__(self, key: str) -> str:
        val = self._map[key]
        return self._decode(val)

    def __setitem__(self, key: str, value: str) -> None:
        raise TypeError("Response headers are read-only")

    def __delitem__(self, key: str) -> None:
        raise TypeError("Response headers are read-only")

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return self._map.contains_key(key)

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for k, _ in self._map.items():
            dk = self._decode(k)
            if dk not in seen:
                seen.add(dk)
                yield dk

    def __len__(self) -> int:
        return self._map.keys_len()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        val = self._map.get(key)
        if val is None:
            return default
        return self._decode(val)

    def items(self) -> list[tuple[str, str]]:
        return [(self._decode(k), self._decode(v)) for k, v in self._map.items()]


# ---------------------------------------------------------------------------
# Response wrapper — requests-compatible interface
# ---------------------------------------------------------------------------


class RnetResponse:
    """Wraps rnet.BlockingResponse with a requests-compatible API."""

    def __init__(self, resp: Any) -> None:
        self._resp = resp
        self._headers: Optional[RnetResponseHeaders] = None
        self._content: Optional[bytes] = None
        self._text: Optional[str] = None
        self._streamed = False

    @property
    def status_code(self) -> int:
        return int(str(self._resp.status_code))

    @property
    def ok(self) -> bool:
        return self._resp.ok

    @property
    def headers(self) -> RnetResponseHeaders:
        if self._headers is None:
            self._headers = RnetResponseHeaders(self._resp.headers)
        return self._headers

    @property
    def url(self) -> str:
        return str(self._resp.url)

    @property
    def content_length(self) -> Optional[int]:
        return self._resp.content_length

    @property
    def content(self) -> bytes:
        if self._content is None:
            self._content = self._resp.bytes()
        return self._content

    @property
    def text(self) -> str:
        if self._text is None:
            encoding = self._resp.encoding or "utf-8"
            self._text = self.content.decode(encoding, errors="replace")
        return self._text

    @property
    def reason(self) -> str:
        try:
            return http.HTTPStatus(self.status_code).phrase
        except ValueError:
            return "Unknown"

    @property
    def cookies(self) -> Any:
        return self._resp.cookies

    def json(self, **kwargs: Any) -> Any:
        import json as _json
        return _json.loads(self.content)

    def raise_for_status(self) -> None:
        if not self.ok:
            raise HTTPError(
                f"{self.status_code} {self.reason}: {self.url}",
                response=self,
            )

    def iter_content(self, chunk_size: Optional[int] = None) -> Iterator[bytes]:
        """Re-chunk rnet's variable-size stream into fixed-size pieces."""
        self._streamed = True
        if chunk_size is None or chunk_size <= 0:
            yield from self._resp.stream()
            return

        buf = bytearray()
        for chunk in self._resp.stream():
            buf.extend(chunk)
            while len(buf) >= chunk_size:
                yield bytes(buf[:chunk_size])
                buf = buf[chunk_size:]
        if buf:
            yield bytes(buf)

    def stream(self) -> Iterator[bytes]:
        """Direct pass-through of rnet's native stream iterator."""
        self._streamed = True
        yield from self._resp.stream()

    def close(self) -> None:
        try:
            self._resp.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Session headers adapter — persists via client.update()
# ---------------------------------------------------------------------------


class RnetSessionHeaders(CaseInsensitiveDict):
    """Dict-like headers that persist to the rnet client via update()."""

    def __init__(self, client: Any) -> None:
        self._client = client
        super().__init__()

    def _sync(self) -> None:
        """Push current headers to the rnet client."""
        if hasattr(self, "_store") and self._store:
            self._client.update(headers={k: v for k, v in self.items()})

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key, value)
        self._sync()

    def update(self, __m: Any = None, **kwargs: Any) -> None:
        if __m:
            if hasattr(__m, "items"):
                for k, v in __m.items():
                    super().__setitem__(k, v)
            else:
                for k, v in __m:
                    super().__setitem__(k, v)
        for k, v in kwargs.items():
            super().__setitem__(k, v)
        self._sync()

    def pop(self, key: str, *args: Any) -> Any:
        result = super().pop(key, *args)
        # rnet doesn't support removing individual headers, but we track locally
        # and always send the full set on next update
        return result

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)


# ---------------------------------------------------------------------------
# Session cookies adapter
# ---------------------------------------------------------------------------


class RnetCookieAdapter(MutableMapping):
    """Cookie adapter that bridges requests-style cookie access to rnet."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._cookies: dict[str, dict[str, str]] = {}  # {domain: {name: value}}
        self._flat: dict[str, str] = {}  # flat name→value for simple access

    def update(self, other: Any = None, **kwargs: Any) -> None:
        if other is None:
            other = {}
        if isinstance(other, CookieJar):
            for cookie in other:
                domain = cookie.domain or ""
                name = cookie.name
                value = cookie.value or ""
                self._flat[name] = value
                self._cookies.setdefault(domain, {})[name] = value
                try:
                    url = f"https://{domain.lstrip('.')}" if domain else "https://localhost"
                    self._client.set_cookie(url, rnet.Cookie(name, value))
                except Exception:
                    pass
        elif isinstance(other, dict):
            for name, value in other.items():
                self._flat[name] = value
                self._client.set_cookie("https://localhost", rnet.Cookie(name, str(value)))
            self._flat.update(other)
        elif hasattr(other, "items"):
            for name, value in other.items():
                self._flat[name] = str(value)
                self._client.set_cookie("https://localhost", rnet.Cookie(name, str(value)))

        for name, value in kwargs.items():
            self._flat[name] = value
            self._client.set_cookie("https://localhost", rnet.Cookie(name, value))

    def get(self, name: str, default: Optional[str] = None, domain: Optional[str] = None,
            path: Optional[str] = None) -> Optional[str]:
        if domain and domain in self._cookies:
            return self._cookies[domain].get(name, default)
        return self._flat.get(name, default)

    def set(self, name: str, value: str, domain: str = "localhost") -> None:
        self._flat[name] = value
        self._cookies.setdefault(domain, {})[name] = value
        url = f"https://{domain.lstrip('.')}"
        self._client.set_cookie(url, rnet.Cookie(name, value))

    def __getitem__(self, name: str) -> str:
        return self._flat[name]

    def __setitem__(self, name: str, value: str) -> None:
        self.set(name, value)

    def __delitem__(self, name: str) -> None:
        self._flat.pop(name, None)
        for domain_cookies in self._cookies.values():
            domain_cookies.pop(name, None)

    def __contains__(self, name: object) -> bool:
        return name in self._flat

    def __iter__(self) -> Iterator:
        return iter(self._flat)

    def __len__(self) -> int:
        return len(self._flat)

    def __bool__(self) -> bool:
        return bool(self._flat)

    def items(self) -> list[tuple[str, str]]:
        return list(self._flat.items())

    def keys(self) -> list[str]:
        return list(self._flat.keys())

    def values(self) -> list[str]:
        return list(self._flat.values())


# ---------------------------------------------------------------------------
# Session proxy adapter
# ---------------------------------------------------------------------------


class RnetProxyDict(dict):
    """Dict-like proxy config that syncs to the rnet client."""

    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    def _sync(self) -> None:
        proxy = self.get("all") or self.get("https") or self.get("http")
        if proxy:
            self._client.update(proxy=proxy)

    def update(self, __m: Any = None, **kwargs: Any) -> None:
        super().update(__m or {}, **kwargs)
        self._sync()

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key, value)
        self._sync()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MaxRetriesError(Exception):
    def __init__(self, message: str, cause: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.__cause__ = cause


# ---------------------------------------------------------------------------
# RnetSession — main session class
# ---------------------------------------------------------------------------


class RnetSession:
    """
    TLS-fingerprinted HTTP session powered by rnet (Rust/BoringSSL).

    Drop-in replacement for CurlSession with requests-compatible API.
    Supports browser impersonation (Chrome, Firefox, Edge, Safari, OkHttp),
    retry with exponential backoff, cookie persistence, and proxy support.
    """

    def __init__(
        self,
        max_retries: int = 5,
        backoff_factor: float = 0.2,
        max_backoff: float = 60.0,
        status_forcelist: Optional[list[int]] = None,
        allowed_methods: Optional[set[str]] = None,
        catch_exceptions: Optional[tuple[type[Exception], ...]] = None,
        **session_kwargs: Any,
    ) -> None:
        # Extract retry config before passing to rnet
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.max_backoff = max_backoff
        self.status_forcelist = status_forcelist or [429, 500, 502, 503, 504]
        self.allowed_methods = allowed_methods or {"GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"}
        self.catch_exceptions = catch_exceptions or (
            rnet.ConnectionError,
            rnet.TimeoutError,
            rnet.RequestError,
        )
        self.log = logging.getLogger(self.__class__.__name__)

        # Extract rnet-compatible kwargs
        client_kwargs: dict[str, Any] = {}
        for key in ("impersonate", "timeout", "proxy", "verify", "redirect"):
            if key in session_kwargs:
                client_kwargs[key] = session_kwargs.pop(key)

        # Always enable cookie store
        client_kwargs["cookie_store"] = True

        # Handle verify=False
        self.verify: bool = client_kwargs.pop("verify", True)
        if not self.verify:
            client_kwargs["danger_accept_invalid_certs"] = True

        self._client = rnet.BlockingClient(**client_kwargs)

        # Set up attribute adapters
        self.headers = RnetSessionHeaders(self._client)
        self.cookies = RnetCookieAdapter(self._client)
        self.proxies = RnetProxyDict(self._client)

        # Handle initial headers/cookies/proxies from kwargs
        if "headers" in session_kwargs:
            self.headers.update(session_kwargs.pop("headers"))
        if "cookies" in session_kwargs:
            self.cookies.update(session_kwargs.pop("cookies"))
        if "proxies" in session_kwargs:
            self.proxies.update(session_kwargs.pop("proxies"))

    def _build_url(self, url: str, params: Optional[dict] = None) -> str:
        """URL-encode params dict into the URL (rnet ignores params kwarg)."""
        if not params:
            return url
        parsed = urlparse(url)
        separator = "&" if parsed.query else ""
        query = parsed.query + separator + urlencode(params, doseq=True) if parsed.query else urlencode(params, doseq=True)
        return urlunparse(parsed._replace(query=query))

    def get_sleep_time(self, response: Optional[RnetResponse], attempt: int) -> Optional[float]:
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

    def request(self, method: str, url: str, **kwargs: Any) -> RnetResponse:
        method_upper = method.upper() if isinstance(method, str) else str(method).upper()

        # Build URL with params
        url = self._build_url(url, kwargs.pop("params", None))

        # Default allow_redirects=True
        kwargs.setdefault("allow_redirects", True)

        # Pass verify setting
        if not self.verify:
            kwargs.setdefault("verify", False)

        # Remove kwargs rnet doesn't understand
        kwargs.pop("stream", None)  # rnet responses are always lazy

        # Translate requests-compatible 'data' kwarg to rnet equivalents
        data = kwargs.pop("data", None)
        if data is not None:
            if isinstance(data, dict):
                kwargs["form"] = list(data.items())
            elif isinstance(data, (str, bytes)):
                kwargs["body"] = data
            else:
                kwargs["body"] = data

        # Resolve method enum
        rnet_method = _METHOD_MAP.get(method_upper)
        if rnet_method is None:
            raise ValueError(f"Unsupported HTTP method: {method}")

        # Skip retry for non-allowed methods
        if method_upper not in self.allowed_methods:
            raw_resp = self._client.request(rnet_method, url, **kwargs)
            return RnetResponse(raw_resp)

        last_exception: Optional[Exception] = None
        response: Optional[RnetResponse] = None

        for attempt in range(self.max_retries + 1):
            try:
                raw_resp = self._client.request(rnet_method, url, **kwargs)
                response = RnetResponse(raw_resp)
                if response.status_code not in self.status_forcelist:
                    return response
                last_exception = HTTPError(f"Received status code: {response.status_code}")
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
                if sleep_duration := self.get_sleep_time(response, attempt + 1):
                    if sleep_duration > 0:
                        time.sleep(sleep_duration)
            else:
                break

        raise MaxRetriesError(f"Max retries exceeded for {method} {url}", cause=last_exception)

    def get(self, url: str, **kwargs: Any) -> RnetResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> RnetResponse:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> RnetResponse:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> RnetResponse:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> RnetResponse:
        return self.request("HEAD", url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> RnetResponse:
        return self.request("OPTIONS", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> RnetResponse:
        return self.request("PATCH", url, **kwargs)

    def prepare_request(self, req: Request) -> Request:
        """Compatibility shim for services using prepared requests."""
        # Merge session headers into request headers
        if req.headers:
            merged = dict(self.headers)
            merged.update(req.headers)
            req.headers = merged
        else:
            req.headers = dict(self.headers)
        return req

    def send(self, req: Request, **kwargs: Any) -> RnetResponse:
        """Compatibility shim for services using prepared requests."""
        method = req.method or "GET"
        url = req.url or ""

        send_kwargs: dict[str, Any] = {}
        if req.headers:
            send_kwargs["headers"] = dict(req.headers)
        if req.body:
            send_kwargs["data"] = req.body
        if req.json:
            send_kwargs["json"] = req.json

        send_kwargs.update(kwargs)
        return self.request(method, url, **send_kwargs)

    def mount(self, prefix: str, adapter: Any) -> None:
        """No-op — rnet handles TLS and connection pooling natively."""
        pass

    def close(self) -> None:
        """No-op — rnet manages its own resources."""
        pass


# ---------------------------------------------------------------------------
# session() factory
# ---------------------------------------------------------------------------


def session(
    browser: Optional[str] = None,
    **kwargs: Any,
) -> RnetSession:
    """
    Create an rnet session with TLS fingerprinting (browser/app impersonation).

    Args:
        browser: Exact rnet.Impersonate preset name. Examples:
                 "Chrome131", "OkHttp4_12", "Edge101", "Firefox135",
                 "Safari18", "OkHttp5", "Opera118"
                 Uses the configured default from config if not specified.
                 See rnet.Impersonate for all available presets.
        **kwargs: Additional arguments passed to RnetSession constructor.

    Returns:
        RnetSession configured with browser impersonation and retry behavior.

    Examples:
        session()                               # Default browser from config
        session("OkHttp4_12")                   # OkHttp 4.12 fingerprint
        session("Chrome131")                    # Chrome 131
        session("Edge101", max_retries=3)       # Edge 101 with custom retry
    """
    if browser is None:
        browser = config.curl_impersonate.get("browser", "Chrome131")

    impersonate = _resolve_impersonate(browser)

    session_kwargs: dict[str, Any] = {"impersonate": impersonate}
    session_kwargs.update(kwargs)

    session_obj = RnetSession(**session_kwargs)
    session_obj.headers.update(config.headers)
    return session_obj
