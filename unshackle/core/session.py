"""Session utilities for creating HTTP sessions with TLS fingerprinting through rnet (Rust/BoringSSL)."""

from __future__ import annotations

import http
import logging
import math
import random
import re
import sys
import time
from collections.abc import Iterator, MutableMapping
from copy import copy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookiejar import Cookie, CookieJar
from typing import Any, Optional, Union
from urllib.parse import urlencode, urlparse, urlunparse

import rnet
from requests import HTTPError, Request, Response, Session
from requests.cookies import create_cookie
from requests.structures import CaseInsensitiveDict

from unshackle.core.config import config
from unshackle.core.utils import run_proxy

# rnet's only distinguishing role is TLS fingerprinting; retry, backoff,
# pooling, and timeouts must match the plain-requests path. Both RnetSession
# (below) and Service.get_session import these so the two can't drift.
# The unified downloader keeps its own copies (downloaders/requests.py) by design.

MAX_RETRIES = 5
BACKOFF_FACTOR = 0.2
MAX_BACKOFF = 60.0  # shared backoff cap (rnet max_backoff == requests Retry backoff_max)
STATUS_FORCELIST = [429, 500, 502, 503, 504]
RETRY_METHODS = frozenset({"GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})
# pool_block=True means a pool smaller than the in-flight request count stalls threads
POOL_MAX_SIZE = 64  # rnet pool_max_idle_per_host == requests pool_maxsize/pool_connections
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30


DEFAULT_IMPERSONATE = rnet.Impersonate.Chrome131


def resolve_impersonate(browser: str) -> rnet.Impersonate:
    """Change a browser string to an rnet.Impersonate preset.

    Accepts exact rnet preset names (e.g. "Chrome131", "OkHttp4_12", "Edge101").
    See https://github.com/0x676e67/rnet for the full list of available presets.
    """
    preset = getattr(rnet.Impersonate, browser, None)
    if preset is not None:
        return preset
    raise ValueError(f"Unknown impersonate preset {browser!r}, expected an rnet.Impersonate name such as 'Chrome131'")


METHOD_MAP: dict[str, rnet.Method] = {
    "GET": rnet.Method.GET,
    "POST": rnet.Method.POST,
    "PUT": rnet.Method.PUT,
    "DELETE": rnet.Method.DELETE,
    "HEAD": rnet.Method.HEAD,
    "OPTIONS": rnet.Method.OPTIONS,
    "PATCH": rnet.Method.PATCH,
    "TRACE": rnet.Method.TRACE,
}


class RnetResponseHeaders(MutableMapping):
    """Read-only str-based view over rnet's bytes-based HeaderMap."""

    def __init__(self, header_map: Any) -> None:
        self._map = header_map

    def decode(self, val: Any) -> str:
        return val.decode("utf-8", errors="replace") if isinstance(val, (bytes, bytearray)) else str(val)

    def __getitem__(self, key: str) -> str:
        val = self._map[key]
        return self.decode(val)

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
            dk = self.decode(k)
            if dk not in seen:
                seen.add(dk)
                yield dk

    def __len__(self) -> int:
        return self._map.keys_len()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:  # type: ignore[override]
        val = self._map.get(key)
        if val is None:
            return default
        return self.decode(val)

    def items(self) -> list[tuple[str, str]]:  # type: ignore[override]
        return [(self.decode(k), self.decode(v)) for k, v in self._map.items()]


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

    @property
    def version(self) -> str:
        """Get the HTTP version used for the response as a string (e.g. 'HTTP/2', 'HTTP/1.1')."""
        try:
            v_str = str(self._resp.version)
            if "HTTP_2" in v_str:
                return "HTTP/2"
            elif "HTTP_11" in v_str:
                return "HTTP/1.1"
            elif "HTTP_3" in v_str:
                return "HTTP/3"
            elif "HTTP_10" in v_str:
                return "HTTP/1.0"
            elif "HTTP_09" in v_str:
                return "HTTP/0.9"
            return v_str.replace("Version.", "").replace("_", "/")
        except Exception:
            return "HTTP/1.1"

    def json(self, **kwargs: Any) -> Any:
        import json as _json

        return _json.loads(self.content)

    def raise_for_status(self) -> None:
        if not self.ok:
            raise HTTPError(
                f"{self.status_code} {self.reason}: {self.url}",
                response=self,  # type: ignore[arg-type]
            )

    def iter_content(self, chunk_size: Optional[int] = None) -> Iterator[bytes]:
        """Re-chunk rnet's variable-size data into fixed-size pieces."""
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
        """Direct pass-through of rnet's native ``stream()`` iterator."""
        self._streamed = True
        yield from self._resp.stream()

    def close(self) -> None:
        try:
            self._resp.close()
        except Exception:
            pass


def utf8_text(res: Response | RnetResponse) -> str:
    """Return the response text, decoded as UTF-8 unless the server sent a charset.

    With no charset, requests assumes ISO-8859-1 for a `text/*` type and runs charset detection
    on the full body for any other type. Use this only for a format that is UTF-8 by
    specification, such as M3U8.
    """
    if isinstance(res, Response) and "charset" not in res.headers.get("content-type", "").lower():
        res.encoding = "utf-8"
    return res.text


class RnetSessionHeaders(CaseInsensitiveDict):
    """Dict-like headers that write to the rnet client through update()."""

    def __init__(self, session: Optional[RnetSession] = None) -> None:
        self._session = session
        self._client: Any = None
        super().__init__()

    def sync(self) -> None:
        """Push current headers to the rnet client."""
        if self._client is not None and hasattr(self, "_store"):
            self._client.update(headers={k: v for k, v in self.items()})

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key, value)
        self.sync()

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
        self.sync()

    def rebuild(self) -> None:
        """Remake the rnet client so removed headers stop going out.

        rnet's update() merges into the client default headers and offers no removal,
        so a header dropped from this dict alone keeps reaching every later host.
        """
        if self._session is not None and self._client is not None:
            self._session.rebuild_client()

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self.rebuild()

    def clear(self) -> None:
        for key in list(self.keys()):
            super().__delitem__(key)
        self.rebuild()


class RnetCookieAdapter(MutableMapping):
    """Cookie adapter that bridges requests-style cookie access to rnet.

    One store keyed by (domain, path, name) holds every cookie set through this class, in write
    order. Same-name cookies on different paths stay apart on the wire, and the latest write
    answers a lookup by name.
    """

    def __init__(self, client: Any, session: Optional[RnetSession] = None) -> None:
        self._client = client
        self._session = session
        self._store: dict[tuple[str, str, str], Cookie] = {}
        self._from_jar: set[tuple[str, str, str]] = set()

    @staticmethod
    def client_url(cookie: Cookie) -> str:
        """The URL rnet stores *cookie* under; a domain-less cookie goes to localhost."""
        host = (cookie.domain or "").lstrip(".")
        return f"https://{host}" if host else "https://localhost"

    @staticmethod
    def to_rnet(cookie: Cookie) -> rnet.Cookie:
        """The rnet form of *cookie*.

        A leading dot marks a cookie for subdomains too, as in a Netscape cookie file; without
        one the cookie stays on its own host. Expiry stays out, so no layer drops a cookie the
        cookie loader deliberately kept.
        """
        domain = cookie.domain or ""
        return rnet.Cookie(
            cookie.name,
            cookie.value or "",
            domain=domain if domain.startswith(".") else None,
            path=cookie.path or "/",
            secure=bool(cookie.secure),
        )

    def push(self, cookie: Cookie) -> None:
        """Set *cookie* on the rnet client; before the client exists the store buffers it."""
        if self._client is not None:
            try:
                self._client.set_cookie(self.client_url(cookie), self.to_rnet(cookie))
            except Exception:
                pass

    def add(self, cookie: Cookie, from_jar: bool = False) -> None:
        """Store *cookie* as the latest write for its (domain, path, name) and send it to the client."""
        key = (cookie.domain or "", cookie.path or "/", cookie.name)
        self._store.pop(key, None)
        self._store[key] = cookie
        if from_jar:
            self._from_jar.add(key)
        self.push(cookie)

    def snapshot(self) -> list[Cookie]:
        """Every stored cookie, oldest write first. Download threads write the store, so read a copy."""
        return list(self._store.values())

    def flush_to_client(self) -> None:
        """Push all buffered cookies to the rnet client after unshackle makes it."""
        for cookie in self.snapshot():
            self.push(cookie)

    def client_urls(self, domain: Optional[str] = None, name: Optional[str] = None) -> list[str]:
        """URLs the rnet cookie jar could hold a cookie under.

        rnet keys its jar by URL, removes a cookie only at its exact path, and cannot list the
        jar. So a removal names every host this class has set a cookie on, every origin the
        HTTP session has requested, and every stored path of the cookie *name*.
        """
        cookies = self.snapshot()
        hosts = {self.client_url(c) for c in cookies} | {"https://localhost"}
        if self._session is not None:
            hosts |= set(self._session._origins)
        if domain is not None:
            host = domain.lstrip(".")
            hosts = {url for url in hosts if (urlparse(url).hostname or "") == host} | {f"https://{host}"}
        paths = {c.path for c in cookies if c.path and c.path != "/" and (name is None or c.name == name)}
        return sorted(hosts | {url + path for url in hosts for path in paths})

    def remove_cookie_on_client(self, name: str, domain: Optional[str] = None) -> None:
        """Drop a cookie from the rnet cookie jar; call it before the store forgets the cookie's paths.

        The client is built with cookie_store=True, so a cookie the server set lives in that
        jar alone. Dropping it from the store leaves it going out on the wire.
        """
        if self._client is None:
            return
        for url in self.client_urls(domain, name):
            try:
                self._client.remove_cookie(url, name)
            except Exception:
                pass

    def absorb(self, response: Any) -> None:
        """Take a value the server set for a stored cookie, so a client rebuild or a cookie save keeps it.

        A server cookie replaces each stored cookie of the same name and path that covers the
        response host, and an expired one drops it.
        """
        server_cookies = response.cookies if self._store else None
        if not server_cookies:
            return
        parsed = urlparse(response.url)
        host = parsed.hostname or ""
        default_path = (parsed.path[: parsed.path.rfind("/")] or "/") if parsed.path.startswith("/") else "/"
        now = datetime.now(timezone.utc)
        for server_cookie in server_cookies:
            path = server_cookie.path if (server_cookie.path or "").startswith("/") else default_path
            expires, max_age = server_cookie.expires, server_cookie.max_age
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            expired = (max_age is not None and max_age.total_seconds() <= 0) or (expires is not None and expires <= now)
            for cookie in self.snapshot():
                domain = cookie.domain or ""
                if cookie.name != server_cookie.name or cookie.path != path or not domain:
                    continue
                if host != domain.lstrip(".") and not (domain.startswith(".") and host.endswith(domain)):
                    continue
                if expired:
                    self.clear(domain=domain, path=path, name=cookie.name)
                elif cookie.value != server_cookie.value:
                    fresh = copy(cookie)
                    fresh.value = server_cookie.value
                    self.add(fresh)

    @property
    def jar(self) -> CookieJar:
        """Return a CookieJar of the cookies that arrived as a CookieJar (requests compat).

        ``save_cookies`` in dl.py uses this to write cookies back to disk, so it carries a value
        the server rotated for one of those cookies but not the cookies set by name.
        """
        jar = CookieJar()
        for key, cookie in list(self._store.items()):
            if key in self._from_jar:
                jar.set_cookie(cookie)
        return jar

    def update(self, other: Any = None, /, **kwargs: Any) -> None:
        if other is None:
            other = {}
        if isinstance(other, (CookieJar, list)):
            for cookie in other:
                self.add(cookie, from_jar=True)
        elif hasattr(other, "items"):
            for name, value in other.items():
                self.add(create_cookie(name, str(value)))
        for name, value in kwargs.items():
            self.add(create_cookie(name, str(value)))

    def get(  # type: ignore[override]
        self, name: str, default: Optional[str] = None, domain: Optional[str] = None, path: Optional[str] = None
    ) -> Optional[str]:
        """The latest value of *name*."""
        for cookie in reversed(self.snapshot()):
            if cookie.name == name and domain in (None, cookie.domain) and path in (None, cookie.path):
                return cookie.value
        return default

    def set(self, name: str, value: str, domain: str = "localhost", path: str = "/", **kwargs: Any) -> None:
        self.add(create_cookie(name, value, domain=domain, path=path, **kwargs))

    def flat(self) -> dict[str, str]:
        """Each cookie name with its latest value."""
        return {cookie.name: cookie.value or "" for cookie in self.snapshot()}

    def __getitem__(self, name: str) -> str:
        return self.flat()[name]

    def __setitem__(self, name: str, value: str) -> None:
        self.set(name, value)

    def __delitem__(self, name: str) -> None:
        self.clear(name=name)

    def __contains__(self, name: object) -> bool:
        return any(cookie.name == name for cookie in self.snapshot())

    def __iter__(self) -> Iterator:
        return iter(self.flat())

    def __len__(self) -> int:
        return len(self.flat())

    def __bool__(self) -> bool:
        return bool(self._store)

    def get_dict(self, domain: Optional[str] = None, path: Optional[str] = None) -> dict[str, str]:
        """Return cookies as a plain dict (requests RequestsCookieJar compat), filtered by exact domain and path."""
        return {c.name: c.value or "" for c in self.snapshot() if domain in (None, c.domain) and path in (None, c.path)}

    def clear(self, domain: Optional[str] = None, path: Optional[str] = None, name: Optional[str] = None) -> None:
        """Remove cookies (requests RequestsCookieJar compat).

        - ``clear()`` removes all cookies.
        - ``clear(domain=..., path=..., name=...)`` removes the cookies that match every given filter.
        """
        if domain is None and path is None and name is None:
            self._store.clear()
            self._from_jar.clear()
            if self._client is not None:
                self._client.clear_cookies()
            return

        def hit(cookie: Cookie) -> bool:
            return domain in (None, cookie.domain) and path in (None, cookie.path) and name in (None, cookie.name)

        names = {name} if name is not None else {c.name for c in self.snapshot() if hit(c)}
        for each in names:
            self.remove_cookie_on_client(each, domain)
        self._store = {key: c for key, c in list(self._store.items()) if not hit(c)}
        self._from_jar &= self._store.keys()
        # removal on the client is by host and name, so put back what a path filter spared
        for cookie in self.snapshot():
            if cookie.name in names:
                self.push(cookie)

    def items(self) -> list[tuple[str, str]]:  # type: ignore[override]
        return list(self.flat().items())

    def keys(self) -> list[str]:  # type: ignore[override]
        return list(self.flat().keys())

    def values(self) -> list[str]:  # type: ignore[override]
        return list(self.flat().values())


class RnetProxyDict(dict):
    """Dict-like proxy config that syncs to the rnet client.

    Accepts ``{"all": url}``, ``{"https": url}``, or ``{"http": url}``
    and applies through rnet's native ``proxies`` parameter (``List[rnet.Proxy]``).
    Supports both lazy (pre-client) and live (post-client) proxy updates.
    """

    def __init__(self, session: "RnetSession") -> None:
        super().__init__()
        self._session = session

    def sync(self) -> None:
        proxy = self.get("all") or self.get("https") or self.get("http")
        proxies = [rnet.Proxy.all(proxy)] if proxy else []
        self._session._client_kwargs["proxies"] = proxies or None
        if self._session._client is not None:
            self._session._client.update(proxies=proxies or None)

    def update(self, __m: Any = None, **kwargs: Any) -> None:
        super().update(__m or {}, **kwargs)
        self.sync()

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key, value)
        self.sync()


class MaxRetriesError(Exception):
    def __init__(self, message: str, cause: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.__cause__ = cause


# the innermost io error text in rnet's wreq::Error debug form, e.g. "stream closed because of a broken pipe"
RNET_ERROR_TEXT_RE = re.compile(r'error: "([^"]+)"')


def failure_response(exc: BaseException) -> Any:
    """The HTTP response of a status failure, or None.

    RnetSession wraps a forcelist HTTPError in MaxRetriesError as ``__cause__``, so this unwraps one level.
    """
    response = getattr(exc, "response", None)
    return response if response is not None else getattr(exc.__cause__, "response", None)


def failure_reason(exc: BaseException) -> str:
    """Name a request failure without its URL, which can carry signed tokens.

    Gives the HTTP status of a status failure, else the exception class and, for an rnet error,
    its innermost error text.
    """
    inner = exc.__cause__ or exc
    response = failure_response(exc)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return f"{status} {getattr(response, 'reason', '') or ''}".strip()
    texts = RNET_ERROR_TEXT_RE.findall(str(inner))
    return f"{type(inner).__name__} ({texts[-1]})" if texts else type(inner).__name__


class RnetSession:
    """TLS-fingerprinted HTTP session powered by rnet (Rust/BoringSSL).

    Drop-in replacement for CurlSession with requests-compatible API.
    Supports browser impersonation (Chrome, Firefox, Edge, Safari, OkHttp),
    retry with exponential backoff, cookie persistence, and proxies.

    unshackle makes the client lazily on the first request, so you can
    configure headers, cookies, and proxies freely before it opens any
    connection.
    """

    def __init__(
        self,
        max_retries: int = MAX_RETRIES,
        backoff_factor: float = BACKOFF_FACTOR,
        max_backoff: float = MAX_BACKOFF,
        status_forcelist: Optional[list[int]] = None,
        allowed_methods: Optional[set[str]] = None,
        catch_exceptions: Optional[tuple[type[Exception], ...]] = None,
        **session_kwargs: Any,
    ) -> None:
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.max_backoff = max_backoff
        self.status_forcelist = status_forcelist or list(STATUS_FORCELIST)
        self.allowed_methods = allowed_methods or set(RETRY_METHODS)
        self.catch_exceptions = catch_exceptions or (
            rnet.ConnectionError,
            rnet.ConnectionResetError,  # sibling of ConnectionError; mid-request resets are retryable
            rnet.TimeoutError,
            rnet.RequestError,
        )
        self.log = logging.getLogger(self.__class__.__name__)

        client_kwargs: dict[str, Any] = {}
        for key in (
            "impersonate",
            "timeout",
            "connect_timeout",
            "read_timeout",
            "proxies",
            "verify",
            "allow_redirects",
            "pool_idle_timeout",
            "pool_max_idle_per_host",
            "pool_max_size",
            "tcp_keepalive",
            "tcp_keepalive_interval",
            "tcp_keepalive_retries",
            "http1_only",
            "http2_only",
            "tcp_nodelay",
        ):
            if key in session_kwargs:
                client_kwargs[key] = session_kwargs.pop(key)
        proxy_url = session_kwargs.pop("proxy", None)

        client_kwargs["cookie_store"] = True

        self.verify: bool = client_kwargs.pop("verify", True)
        if not self.verify:
            client_kwargs["danger_accept_invalid_certs"] = True

        self._client_kwargs = dict(client_kwargs)
        self._client: Optional[rnet.BlockingClient] = None
        self._origins: set[str] = set()

        self.headers = RnetSessionHeaders(self)
        self.cookies = RnetCookieAdapter(None, self)
        self.proxies = RnetProxyDict(self)
        self.proxy_exempt = False  # True: this HTTP session goes direct on purpose, so the proxy check skips it

        if "headers" in session_kwargs:
            self.headers.update(session_kwargs.pop("headers"))
        if "cookies" in session_kwargs:
            self.cookies.update(session_kwargs.pop("cookies"))
        if "proxies" in session_kwargs:
            self.proxies.update(session_kwargs.pop("proxies"))
        if proxy_url:
            self.proxies.update({"all": proxy_url})

    @property
    def impersonate_name(self) -> Optional[str]:
        """Preset name (e.g. 'Chrome131') for rebuilding this HTTP session in another process.

        rnet enums stringify as 'Impersonate.Chrome131'. Return the trailing name, or None
        when no preset was set (then the HTTP session is not cheaply rebuildable across
        processes).
        """
        preset = self._client_kwargs.get("impersonate")
        if preset is None:
            return None
        name = str(preset).rsplit(".", 1)[-1]
        return name or None

    def ensure_client(self) -> rnet.BlockingClient:
        """Lazily make the rnet client on first use, flushing any buffered state."""
        if self._client is None:
            client = rnet.BlockingClient(**self._client_kwargs)
            self.headers._client = client
            self.headers.sync()
            self.cookies._client = client
            self.cookies.flush_to_client()
            self._client = client
        return self._client

    def rebuild_client(self) -> None:
        """Replace the rnet client with one that carries only the current header set.

        rnet merges header updates and has no header removal, so a new client is the only
        way to drop a header the caller deleted. Do not pass the headers as default_headers:
        that replaces the impersonate header set instead of overlaying it, which breaks the
        fingerprint.

        A new client also starts with an empty cookie jar. This method replays the stored
        cookies, which hold any value the server rotated for them, then the root-path cookies
        of every origin this HTTP session has used. A cookie the server set on a deeper path
        that the store never held is lost, because rnet cannot list its jar. The new client
        goes on the HTTP session last, so a request from another thread keeps the old client
        until the new one holds the headers and the cookies.
        """
        old_client = self._client
        if old_client is None:
            return
        client = rnet.BlockingClient(**self._client_kwargs)
        self.headers._client = client
        self.headers.sync()
        self.cookies._client = client
        self.cookies.flush_to_client()
        for origin in list(self._origins):
            self.copy_cookies(old_client, client, origin)
        self._client = client

    @staticmethod
    def parse_cookie_header(header: str) -> Iterator[tuple[str, str]]:
        """The ``(name, value)`` pairs of a ``name=value; ...`` header string."""
        for pair in header.split(";"):
            name, separator, value = pair.partition("=")
            if separator:
                yield name.strip(), value.strip()

    @staticmethod
    def set_cookie_header(client: rnet.BlockingClient, origin: str, header: str) -> None:
        """Set every cookie in a ``name=value; ...`` header string on one origin of *client*."""
        for name, value in RnetSession.parse_cookie_header(header):
            try:
                client.set_cookie(origin, rnet.Cookie(name, value))
            except Exception:
                pass

    @staticmethod
    def copy_cookies(source: rnet.BlockingClient, target: rnet.BlockingClient, origin: str) -> None:
        """Copy one origin's cookies between clients. rnet cannot list the whole cookie jar."""
        try:
            raw = source.get_cookies(origin)
        except Exception:
            return
        if not raw:
            return
        RnetSession.set_cookie_header(target, origin, raw.decode("utf-8", errors="replace"))

    def export_origin_cookies(self) -> dict[str, str]:
        """Cookie headers the live client holds, one per origin this HTTP session has requested.

        The client owns its own cookie jar, so a cookie a server set lives there and in no
        part of :class:`RnetCookieAdapter`, which records only what was set through it. rnet
        cannot list the jar, so this covers the origins already requested and no others.
        """
        if self._client is None:
            return {}
        exported: dict[str, str] = {}
        for origin in list(self._origins):
            try:
                raw = self._client.get_cookies(origin)
            except Exception:
                continue
            if raw:
                exported[origin] = raw.decode("utf-8", errors="replace")
        return exported

    def scoped_cookies(self) -> CookieJar:
        """Every cookie this HTTP session sends, each scoped to its domain.

        The live client's root-path cookies of each requested origin come first, then the
        cookies stored in :attr:`cookies` with their own paths.
        """
        jar = CookieJar()
        hosts: dict[str, set[str]] = {}
        for origin, header in self.export_origin_cookies().items():
            host = urlparse(origin).hostname or ""
            for name, value in self.parse_cookie_header(header):
                jar.set_cookie(create_cookie(name, value, domain=host))
                hosts.setdefault(name, set()).add(host)
        for cookie in self.cookies.snapshot():
            bare = (cookie.domain or "").lstrip(".")
            live = any(h == bare or h.endswith("." + bare) for h in hosts.get(cookie.name, ()))
            if not (live and cookie.path in ("", "/")):
                jar.set_cookie(cookie)
        return jar

    def import_origin_cookies(self, exported: dict[str, str]) -> None:
        """Load cookie headers from :meth:`export_origin_cookies` into this HTTP session's client."""
        if not exported:
            return
        client = self.ensure_client()
        for origin, header in exported.items():
            self.set_cookie_header(client, origin, header)

    def build_url(self, url: str, params: Optional[Any] = None) -> str:
        """Encode params into the URL (rnet ignores the params kwarg).

        Accepts the same shapes as requests: a mapping, a sequence of pairs, or a
        pre-built query string/bytes. This method appends a string verbatim (already
        encoded). urlencode() would raise TypeError on it.
        """
        if not params:
            return url
        if isinstance(params, bytes):
            extra = params.decode("utf-8")
        elif isinstance(params, str):
            extra = params
        else:
            extra = urlencode(params, doseq=True)
        parsed = urlparse(url)
        separator = "&" if parsed.query else ""
        query = parsed.query + separator + extra if parsed.query else extra
        return urlunparse(parsed._replace(query=query))

    def get_sleep_time(self, response: Optional[RnetResponse], attempt: int) -> Optional[float]:
        if response:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                wait: Optional[float] = None
                try:
                    wait = float(retry_after)
                except ValueError:
                    try:
                        retry_date = parsedate_to_datetime(retry_after)
                        if retry_date.tzinfo is None:
                            retry_date = retry_date.replace(tzinfo=timezone.utc)
                        wait = (retry_date - datetime.now(timezone.utc)).total_seconds()
                    except Exception:
                        # parsedate_to_datetime itself raises ValueError on malformed dates
                        # (Python >= 3.10); an unusable header must not escape the retry loop
                        wait = None
                if wait is not None and math.isfinite(wait):
                    # a hostile Retry-After (e.g. 86400) would otherwise park the caller for a day
                    return min(wait, self.max_backoff)

        if attempt == 0:
            return 0.0

        backoff_value = self.backoff_factor * (2 ** (attempt - 1))
        jitter = backoff_value * 0.1
        sleep_time = backoff_value + random.uniform(-jitter, jitter)
        return min(sleep_time, self.max_backoff)

    def absorb_cookies(self, response: RnetResponse) -> RnetResponse:
        """Record a value the server set for a stored cookie; a cookie fault must never fail the request."""
        try:
            self.cookies.absorb(response)
        except Exception:  # nosec B110 - the store keeps its old value, the rnet jar still has the new one
            pass
        return response

    def request(self, method: str, url: str, **kwargs: Any) -> RnetResponse:
        """Send a request, retrying on the status forcelist and on the caught exception types.

        This method retries only the methods in allowed_methods. Any other method gets one try.
        A max_retries kwarg overrides the HTTP session retry budget for this call alone, where 0
        means one try with no retries. Once the budget is spent, this method raises
        MaxRetriesError with the last failure as its cause.
        """
        client = self.ensure_client()
        method_upper = method.upper() if isinstance(method, str) else str(method).upper()

        max_retries = kwargs.pop("max_retries", None)
        if max_retries is None:
            max_retries = self.max_retries

        url = self.build_url(url, kwargs.pop("params", None))

        parsed_origin = urlparse(url)
        if run_proxy.watched and not self.proxy_exempt and not self._client_kwargs.get("proxies"):
            try:
                run_proxy.check(parsed_origin.hostname, sys._getframe(1))
            except Exception:  # nosec B110 - the proxy check only warns, it must never fail a request
                pass
        if parsed_origin.scheme and parsed_origin.netloc:
            self._origins.add(f"{parsed_origin.scheme}://{parsed_origin.netloc}")

        kwargs.setdefault("allow_redirects", True)

        if not self.verify:
            kwargs.setdefault("verify", False)

        # Remove kwargs rnet doesn't understand
        kwargs.pop("stream", None)  # rnet responses are always lazy

        data = kwargs.pop("data", None)
        if data is not None:
            if isinstance(data, dict):
                kwargs["form"] = list(data.items())
            elif isinstance(data, (str, bytes)):
                kwargs["body"] = data
            else:
                kwargs["body"] = data

        rnet_method = METHOD_MAP.get(method_upper)
        if rnet_method is None:
            raise ValueError(f"Unsupported HTTP method: {method}")

        # Convert headers to standard dict once to resolve PyO3 CaseInsensitiveDict rejection.
        if kwargs.get("headers") is not None:
            kwargs["headers"] = dict(kwargs["headers"])

        if method_upper not in self.allowed_methods:
            return self.absorb_cookies(RnetResponse(client.request(rnet_method, url, **kwargs)))

        last_exception: Optional[Exception] = None
        response: Optional[RnetResponse] = None

        for attempt in range(max_retries + 1):
            try:
                response = self.absorb_cookies(RnetResponse(client.request(rnet_method, url, **kwargs)))

                if config.debug_requests:
                    parsed_url = urlparse(url)
                    port_str = f":{parsed_url.port}" if parsed_url.port else ""
                    if not port_str:
                        port_str = ":443" if parsed_url.scheme == "https" else ":80"
                    host_url = f"{parsed_url.scheme}://{parsed_url.hostname}{port_str}"
                    path_and_query = parsed_url.path + (f"?{parsed_url.query}" if parsed_url.query else "")
                    if not path_and_query:
                        path_and_query = "/"
                    content_length = response.headers.get("content-length") or response.content_length

                    log_msg = f'{host_url} "{method_upper} {path_and_query} {response.version}" {response.status_code} {content_length}'
                    self.log.debug(log_msg)

                if response.status_code not in self.status_forcelist:
                    return response
                last_exception = HTTPError(
                    f"Received status code: {response.status_code}",
                    response=response,  # type: ignore[arg-type]
                )
                failure = failure_reason(last_exception)

            except self.catch_exceptions as e:
                last_exception = e
                response = None
                failure = failure_reason(e)

            if attempt < max_retries:
                self.log.warning(f"{failure}({urlparse(url).path}). Retrying... ({attempt + 1}/{max_retries})")
                if sleep_duration := self.get_sleep_time(response, attempt + 1):
                    if sleep_duration > 0:
                        time.sleep(sleep_duration)
            else:
                break

        attempts = f" after {max_retries + 1} attempts" if max_retries else ""
        raise MaxRetriesError(
            f"{method_upper} {parsed_origin.hostname} failed{attempts}: {failure}", cause=last_exception
        )

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
        if req.data:
            send_kwargs["data"] = req.data
        if req.json:
            send_kwargs["json"] = req.json

        send_kwargs.update(kwargs)
        return self.request(method, url, **send_kwargs)

    def mount(self, prefix: str, adapter: Any) -> None:
        """No-op. rnet does TLS and connection pooling natively."""
        pass

    def close(self) -> None:
        """No-op. rnet manages its own resources."""
        pass


AnySession = Union[Session, RnetSession]


def session(
    browser: Optional[str] = None,
    **kwargs: Any,
) -> RnetSession:
    """
    Make an rnet HTTP session with TLS fingerprinting (browser/app impersonation).

    Args:
        browser: Exact rnet.Impersonate preset name. Examples:
                 "Chrome131", "OkHttp4_12", "Edge101", "Firefox135",
                 "Safari18", "OkHttp5", "Opera118"
                 Uses the configured default from config if not specified.
                 See rnet.Impersonate for all available presets.
        **kwargs: Additional arguments passed to RnetSession constructor.

    Returns:
        RnetSession configured with browser impersonation and retry behaviour.

    Examples:
        session()                               # Default browser from config
        session("OkHttp4_12")                   # OkHttp 4.12 fingerprint
        session("Chrome131")                    # Chrome 131
        session("Edge101", max_retries=3)       # Edge 101 with custom retry
    """
    if browser is None:
        browser = config.network.get("browser", "Chrome131")

    impersonate = resolve_impersonate(browser)

    session_kwargs: dict[str, Any] = {"impersonate": impersonate}
    # optional rnet client knobs, see docs/NETWORK_CONFIG.md
    for key in (
        "http1_only",
        "http2_only",
        "pool_max_idle_per_host",
        "pool_max_size",
        "tcp_nodelay",
        "connect_timeout",
        "read_timeout",
        "timeout",
        "pool_idle_timeout",
        "tcp_keepalive",
        "tcp_keepalive_interval",
        "tcp_keepalive_retries",
        "allow_redirects",
    ):
        if key in config.network:
            session_kwargs[key] = config.network[key]
    session_kwargs.update(kwargs)

    # Connection-pool / timeout defaults applied only when neither config.network nor the caller set them.
    # connect_timeout + read_timeout mirror the requests-path default timeout (CONNECT_TIMEOUT, READ_TIMEOUT)
    # so an unset config still gets a bounded connect and read like requests; pool_idle_timeout stays under
    # the typical ~60s CDN idle kill; pool_max_idle_per_host follows POOL_MAX_SIZE, sized above the worker
    # cap because hedge racers and tail-boost parts push in-flight requests past it; tcp_keepalive keeps
    # long idle segments warm.
    session_kwargs.setdefault("connect_timeout", CONNECT_TIMEOUT)
    session_kwargs.setdefault("read_timeout", READ_TIMEOUT)
    session_kwargs.setdefault("pool_idle_timeout", 55)
    session_kwargs.setdefault("pool_max_idle_per_host", POOL_MAX_SIZE)
    session_kwargs.setdefault("tcp_keepalive", 30)

    session_obj = RnetSession(**session_kwargs)
    session_obj.headers.update(config.headers)
    return session_obj
