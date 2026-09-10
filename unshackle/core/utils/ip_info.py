from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

import requests

from unshackle.core.cacher import Cacher

CACHE_KEY = "ip_info_v4"
CACHE_TTL = 86400  # 24 hours
PROVIDER_STATE_KEY = "ip_provider_state"
RATE_LIMIT_COOLDOWN = 300  # 5 minutes
REQUEST_TIMEOUT = 10

# Only these keys are persisted to the global cache. "ip" is the local IP, which
# verify_proxy_exit compares against the proxy exit IP.
GEO_CACHE_KEYS = ("ip", "country", "country_code")

Fetcher = Callable[[requests.Session], Optional[dict]]

log = logging.getLogger("ip_info")


class RateLimited(Exception):
    """Raised by a geolocation API fetcher when the upstream returns 429."""


def normalize(
    *,
    country_code: str,
    ip: str = "",
    region: str = "",
    city: str = "",
    org: str = "",
    asn: str = "",
    as_name: str = "",
    continent_code: str = "",
) -> Optional[dict]:
    """Assemble the canonical IP-info dict, or None if no country code is present."""
    code = country_code.strip()
    if not code:
        return None
    return {
        "ip": ip,
        "country": code.lower(),
        "country_code": code.upper(),
        "region": region,
        "city": city,
        "org": org,
        "asn": asn,
        "as_name": as_name,
        "continent_code": continent_code.upper(),
    }


def parse_ipinfo_lite(data: dict) -> Optional[dict]:
    asn = (data.get("asn") or "").strip()
    as_name = (data.get("as_name") or "").strip()
    return normalize(
        country_code=data.get("country_code") or "",
        ip=data.get("ip") or "",
        org=f"{asn} {as_name}".strip(),
        asn=asn,
        as_name=as_name,
        continent_code=data.get("continent_code") or "",
    )


def parse_ipinfo(data: dict) -> Optional[dict]:
    return normalize(
        country_code=data.get("country") or "",
        ip=data.get("ip") or "",
        region=data.get("region") or "",
        city=data.get("city") or "",
        org=data.get("org") or "",
    )


def parse_ip_api_in(data: dict) -> Optional[dict]:
    asn = (data.get("asn") or "").strip()
    org_name = (data.get("organization") or "").strip()
    return normalize(
        country_code=data.get("country_code") or "",
        ip=data.get("ip") or "",
        region=data.get("region") or "",
        city=data.get("city") or "",
        org=f"{asn} {org_name}".strip(),
        asn=asn,
        as_name=org_name,
        continent_code=data.get("continent_code") or "",
    )


def lookup_session(source: Optional[requests.Session]) -> requests.Session:
    """
    Assemble a plain, retry-free `requests` HTTP session for IP geolocation.

    Geolocation needs no TLS fingerprinting, so we skip the impersonated rnet HTTP
    session, and the urllib3 retry loop of the base HTTP session. Both retry 429
    internally, which hides the response and defeats fast handover to the next
    geolocation API. With a bare HTTP session a 429 comes straight back, so we can
    move to the next API immediately. This function carries over the proxy only, so
    proxied lookups still report the proxy's exit IP.
    """
    sess = requests.Session()
    proxies = getattr(source, "proxies", None)
    if proxies:
        proxy = proxies.get("all") or proxies.get("https") or proxies.get("http")
        if proxy:
            sess.trust_env = False
            sess.proxies.update({"http": proxy, "https": proxy})
    return sess


def json_or_raise(response: requests.Response) -> Optional[dict]:
    """Raise RateLimited on 429, return parsed JSON on 200, else None."""
    if response.status_code == 429:
        raise RateLimited()
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def fetch_ipinfo_lite(token: str) -> Fetcher:
    headers = {"Authorization": f"Bearer {token}"}

    def fetch(session: requests.Session) -> Optional[dict]:
        payload = json_or_raise(session.get("https://api.ipinfo.io/lite/me", headers=headers, timeout=REQUEST_TIMEOUT))
        return parse_ipinfo_lite(payload) if payload else None

    return fetch


def fetch_ipinfo(session: requests.Session) -> Optional[dict]:
    payload = json_or_raise(session.get("https://ipinfo.io/json", timeout=REQUEST_TIMEOUT))
    return parse_ipinfo(payload) if payload else None


def fetch_ip_api_in(session: requests.Session) -> Optional[dict]:
    """ip-api.in has no /me endpoint, so get the IP from ipify first, then look it up."""
    ip_resp = session.get("https://api.ipify.org", timeout=REQUEST_TIMEOUT)
    if ip_resp.status_code == 429:
        raise RateLimited()
    ip = (ip_resp.text or "").strip() if ip_resp.status_code == 200 else ""
    if not ip:
        return None
    payload = json_or_raise(session.get(f"https://ip-api.in/api/v1/ip/{ip}", timeout=REQUEST_TIMEOUT))
    if not payload or not payload.get("success"):
        return None
    return parse_ip_api_in(payload.get("data") or {})


def build_providers() -> list[tuple[str, Fetcher]]:
    """Return ordered (name, fetcher) pairs. Token is read at call time."""
    from unshackle.core.config import config

    providers: list[tuple[str, Fetcher]] = []
    token = (getattr(config, "ipinfo_api_key", "") or "").strip()
    if token:
        providers.append(("ipinfo_lite", fetch_ipinfo_lite(token)))
    providers.append(("ipinfo", fetch_ipinfo))
    providers.append(("ip_api_in", fetch_ip_api_in))
    return providers


def purge_stale_cache() -> None:
    """Delete superseded ip_info cache files (older CACHE_KEY versions)."""
    from unshackle.core.config import config

    global_dir = config.directories.cache / "global"
    for stale in global_dir.glob("ip_info_v*.json"):
        if stale.stem != CACHE_KEY:
            stale.unlink(missing_ok=True)


def load_provider_state(cacher: Cacher) -> dict[str, Any]:
    return cacher.data if cacher and not cacher.expired and isinstance(cacher.data, dict) else {}


def get_ip_info(
    session: Optional[requests.Session] = None,
    *,
    cached: bool = False,
    errors: Optional[list[Exception]] = None,
) -> Optional[dict]:
    """
    Look up IP/geolocation info through ipinfo.io (Lite when `ipinfo_api_key` configured)
    with fallback to ip-api.in.

    Live lookups return a dict with `ip`, `country` (lowercase ISO2), `country_code`
    (uppercase ISO2), `region`, `city`, `org`, `asn`, `as_name`, `continent_code` and
    `_provider`. Cached lookups return only `ip`/`country`/`country_code` (see GEO_CACHE_KEYS).
    Returns None if every geolocation API fails.

    Args:
        session: Optional `requests` HTTP session. If you pass a proxied session,
            the returned info reflects the proxy's exit IP. unshackle sends the
            ipinfo auth headers per-request, and never mutates them onto
            session.headers.
        cached: When True, read/write a 24h Cacher-backed entry. Use only for
            local IP lookups, never with a proxied HTTP session.
        errors: When given, each geolocation API that raised an exception adds it to
            this list. A 429 or an unusable response adds nothing.
    """
    cache = None
    if cached:
        purge_stale_cache()
        cache = Cacher("global").get(CACHE_KEY)
        if cache and not cache.expired and cache.data:
            return cache.data

    state_cache = Cacher("global").get(PROVIDER_STATE_KEY)
    state = load_provider_state(state_cache)
    now = time.time()

    def on_cooldown(item: tuple[str, Fetcher]) -> int:
        rate_limited_at = (state.get(item[0]) or {}).get("rate_limited_at", 0)
        return 1 if (now - rate_limited_at) < RATE_LIMIT_COOLDOWN else 0

    providers = sorted(build_providers(), key=on_cooldown)
    sess = lookup_session(session)

    for name, fetcher in providers:
        log.debug(f"Trying IP provider: {name}")
        try:
            normalized = fetcher(sess)
        except RateLimited:
            log.warning(f"Provider {name} returned 429 (rate limited), trying next provider")
            entry = state.setdefault(name, {})
            entry["rate_limited_at"] = now
            entry["rate_limit_count"] = entry.get("rate_limit_count", 0) + 1
            state_cache.set(state, expiration=RATE_LIMIT_COOLDOWN)
            continue
        except Exception as e:
            log.debug(f"Provider {name} failed with exception: {e}")
            if errors is not None:
                errors.append(e)
            continue

        if not normalized:
            log.debug(f"Provider {name} returned no usable data")
            continue

        normalized["_provider"] = name
        log.debug(f"Successfully got IP info from provider: {name}")

        if name in state and state[name].pop("rate_limited_at", None) is not None:
            state_cache.set(state, expiration=RATE_LIMIT_COOLDOWN)

        if cache is not None:
            cache.set({k: normalized.get(k, "") for k in GEO_CACHE_KEYS}, expiration=CACHE_TTL)

        return normalized

    log.warning("All IP geolocation providers failed")
    return None


def verify_proxy_exit(session: requests.Session) -> dict:
    """
    Look up the exit IP of the proxy on `session`, and check that it is not the local IP.

    Returns the live IP info of the proxy exit, or an empty dict when geolocation APIs
    answered through the proxy (a 429, or no usable data) but none returned an IP. Raises
    ConnectionError when no geolocation API got a response through the proxy, or
    when the proxy exit IP is the local IP. This function does not check the exit country.
    The local IP comes from the 24h cached local lookup, and a live local lookup must
    confirm a match before this function rejects the proxy. If the local IP is not known,
    the IP comparison does not occur.
    """
    errors: list[Exception] = []
    info = get_ip_info(session, errors=errors)
    if not info:
        if len(errors) < len(build_providers()):
            log.warning("No geolocation API returned the proxy exit IP, so the exit IP check does not occur")
            return {}
        raise ConnectionError(
            f"Proxy check failed: no IP lookup got through the proxy ({type(errors[-1]).__name__}). "
            "The proxy is down or refused the connection."
        )
    exit_ip = info.get("ip")
    server_ip = (get_ip_info(cached=True) or {}).get("ip")
    # The cached IP can be up to 24h old: a machine that was on a VPN when it cached its IP,
    # and now uses that VPN as a proxy, matches the old IP. A live lookup runs only on a match.
    if exit_ip and server_ip == exit_ip and (get_ip_info() or {}).get("ip") == exit_ip:
        raise ConnectionError(
            "Proxy check failed: the proxy exit IP is the IP of this machine, so traffic does not go through the proxy."
        )
    return info
