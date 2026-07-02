from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import re
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from unshackle.core.config import config
from unshackle.core.proxies.proxy import Proxy

log = logging.getLogger("proxies.expressvpn")


class ExpressVPN(Proxy):
    """
    ExpressVPN HTTPS proxy provider.

    This provider follows the browser extension proxy flow:
    browser cookies or a cached refresh token are exchanged for API tokens,
    those tokens are used to resolve proxy-capable locations, and get_proxy()
    returns an authenticated HTTPS proxy URL.

    Query format:
        country                   -- smart connection (random location)
        country-city              -- specific city
        country-city-N / cityN   -- pinned server by position
        full-slug                 -- ExpressVPN location slug
        hostname.expressprovider.com -- direct hostname
    """

    CLIENT_ID = "f457fed092a54b9e9f1e2113782d74a2"
    AUTH_BASE = "https://auth.expressvpn.com/realms/xvpn/protocol/openid-connect"
    API_BASE = "https://cp.expressapisv2.net"
    EXTENSION_REDIRECT_URI = "chrome-extension://fgddmllnllkalaagkghckoinaemmogpe/src/html/auth-callback.html"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        region_map: Optional[dict[str, str]] = None,
        server_map: Optional[dict[str, str]] = None,
        refresh_token: Optional[str] = None,
        access_token: Optional[str] = None,
        connection_token: Optional[str] = None,
        account_json: Optional[str] = None,
        cookie_path: Optional[str] = None,
        cache_path: Optional[str] = None,
        timeout: float = 10.0,
    ):
        """
        Proxy Service using ExpressVPN browser-extension proxy credentials.

        Args:
            region_map: Optional country-to-preset mapping. Keys are country
                codes (e.g. ``"us"``), values are optional city/server presets
                (e.g. ``"ny-02"``). When a key is used without a city in the
                CLI query, the preset is applied. Empty/null values enable
                smart connection (random location in that country).
            server_map: Optional aliases using the same convention as other
                proxy providers. Values may be ExpressVPN location slugs or
                concrete .expressprovider.com hosts.
            refresh_token: Optional OAuth refresh token. If omitted, cached
                tokens or browser cookies are used.
            access_token: Optional OAuth access token for advanced/manual use.
            connection_token: Optional cached connection authorization token.
            account_json: Optional path to ExpressVPN desktop account.json.
            cookie_path: Optional path to exported ExpressVPN browser cookies.
            cache_path: Optional path for cached ExpressVPN tokens.
            timeout: Request timeout in seconds.
        """
        if region_map is not None and not isinstance(region_map, dict):
            raise TypeError(f"Expected region_map to be a dict mapping aliases to locations, not '{region_map!r}'.")
        if server_map is not None and not isinstance(server_map, dict):
            raise TypeError(f"Expected server_map to be a dict mapping aliases to locations, not '{server_map!r}'.")

        # region_map: country code → optional preset (city[-server])
        self.region_map = {
            str(k).lower().strip(): (str(v).lower().strip() if v else None)
            for k, v in (region_map or {}).items()
        }
        # server_map: legacy alias → location slug or direct hostname
        self.server_map = {
            str(k).lower().strip(): str(v).lower().strip()
            for k, v in (server_map or {}).items()
        }
        self.refresh_token = refresh_token or None
        self.access_token = access_token or None
        self.connection_token = connection_token or None
        self.account_json = Path(account_json).expanduser() if account_json else None
        self.timeout = timeout
        self._tokens: Optional[dict] = None
        self._srt: Optional[str] = None
        self._locations: Optional[list[dict]] = None

        # Display info set during _resolve_endpoint for log messages
        self._last_location_name: Optional[str] = None
        self._last_server_index: Optional[int] = None
        self._last_server_total: Optional[int] = None
        self._last_endpoint_host: Optional[str] = None

        self.cookie_path = Path(cookie_path).expanduser() if cookie_path else self._default_cookie_path()
        self.cache_path = Path(cache_path).expanduser() if cache_path else self._default_cache_path()

    def __repr__(self) -> str:
        if self._locations is not None:
            locations = self._locations
            countries = len({
                str(loc.get("country_code") or "").upper()
                for loc in locations if loc.get("country_code")
            })
            servers = len(locations)
            return (
                f"{countries} Countr{'ies' if countries != 1 else 'y'} "
                f"({servers} Server{'s' if servers != 1 else ''})"
            )
        alias_count = len(self.region_map) + len(self.server_map)
        if alias_count:
            return f"{alias_count} Region Alias{'es' if alias_count != 1 else ''} (ExpressVPN HTTPS Proxy)"
        return "ExpressVPN HTTPS Proxy"

    def get_proxy(self, query: str) -> Optional[str]:
        query = query.strip().lower()
        endpoint = self._resolve_endpoint(query)
        if not endpoint:
            return None

        connection_token = self._get_connection_token()
        if not connection_token:
            log.error("ExpressVPN: connection token was not available")
            return None

        display = self.last_connection_display()
        log.debug("ExpressVPN proxy ready: %s", display or f"https://cat:***@{endpoint}:443")
        return f"https://cat:{connection_token}@{endpoint}:443"

    def last_connection_display(self) -> Optional[str]:
        """Return a human-readable string describing the last resolved connection.

        Used by the download/search commands to display a friendly log message
        instead of a raw sanitized proxy URL.

        Returns:
            A string like ``"(USA - New York, #3 of 5): .214"`` or None if
            no connection has been resolved yet.
        """
        if not self._last_location_name or not self._last_endpoint_host:
            return None

        # Strip the .expressprovider.com suffix for readability
        short_host = self._last_endpoint_host.replace(".expressprovider.com", "")

        parts = [self._last_location_name]
        if self._last_server_index is not None and self._last_server_total is not None:
            parts.append(f"#{self._last_server_index} of {self._last_server_total}")

        return f"({', '.join(parts)}): {short_host}"

    # ------------------------------------------------------------------
    # Endpoint Resolution
    # ------------------------------------------------------------------

    def _resolve_endpoint(self, query: str) -> Optional[str]:
        # Reset display info
        self._last_location_name = None
        self._last_server_index = None
        self._last_server_total = None
        self._last_endpoint_host = None

        # 1) Check legacy server_map first (direct hostname / slug aliases)
        mapped = self.server_map.get(query)
        if mapped:
            query = mapped

        # 2) Direct hostname pass-through
        if "expressprovider" in query:
            host = query if query.endswith(".expressprovider.com") else f"{query}.expressprovider.com"
            self._last_endpoint_host = host
            return host

        # 3) Parse the query into (country, city, server_num)
        country, city, server_num = self._parse_query(query)

        # 4) If the query matched a country in region_map and no city was
        #    specified in the CLI, apply the preset from region_map
        if country and not city:
            preset = self.region_map.get(country)
            if preset:
                # Preset is like "ny-02" or "ny" or "ny2"
                _, preset_city, preset_server = self._parse_query(f"{country}-{preset}")
                city = preset_city
                server_num = preset_server

        # 5) Resolve the location
        if country:
            location = self._resolve_location_by_country(country, city)
        else:
            # Fallback: try the full query as a location slug
            location = self._resolve_location_by_slug(query)

        if not location:
            log.warning("ExpressVPN: no location matched query '%s'", query)
            return None

        self._last_location_name = location.get("name")

        # 6) Get endpoints and pick server
        endpoints = self._get_endpoints_for_location(location)
        if not endpoints:
            log.error("ExpressVPN: no proxy endpoints returned for %s", location.get("name") or query)
            return None

        host = self._pick_endpoint(endpoints, server_num)
        return host

    def _parse_query(self, query: str) -> tuple[Optional[str], Optional[str], Optional[int]]:
        """Parse a query string into (country_code, city_query, server_number).

        Supported formats::

            us                  -> ("us", None, None)
            us-ny               -> ("us", "ny", None)
            us-ny-2             -> ("us", "ny", 2)
            us-ny2              -> ("us", "ny", 2)
            us-ny-02            -> ("us", "ny", 2)
            usa-new-york        -> (None, None, None)  -- treated as slug
            mx                  -> ("mx", None, None)

        When the query doesn't look like a country[-city] pattern, all
        fields are returned as None and the caller falls back to slug
        resolution.
        """
        # Try to extract a trailing server number: "us-ny-2" or "us-ny2"
        server_num = None
        base_query = query

        # Match trailing -N or trailing digits glued to alpha
        num_match = re.match(r"^(.+?)[-]?(\d+)$", query)
        if num_match:
            candidate_base = num_match.group(1).rstrip("-")
            candidate_num = int(num_match.group(2))
            # Only treat as server number if the base contains alpha characters
            # and the number is reasonable (1-99)
            if re.search(r"[a-z]", candidate_base) and 1 <= candidate_num <= 99:
                base_query = candidate_base
                server_num = candidate_num

        # Try to split as country-city
        # Country codes are 2 letters; city is the rest
        country_match = re.match(r"^([a-z]{2})(?:-(.+))?$", base_query)
        if country_match:
            country = country_match.group(1)
            city = country_match.group(2)  # may be None

            # Verify this is a valid country code by checking locations
            locations = self._get_locations()
            valid_countries = {str(loc.get("country_code") or "").lower() for loc in locations}
            if country in valid_countries:
                return country, city or None, server_num

        # Not a country-city pattern; return None to trigger slug fallback
        return None, None, None

    def _resolve_location_by_country(
        self, country_code: str, city_query: Optional[str] = None
    ) -> Optional[dict]:
        """Find a location by country code and optional city query.

        If *city_query* is None, a random location in the country is selected
        (smart connection). Otherwise, the city is matched against location
        names using abbreviation, prefix, and substring strategies.
        """
        locations = self._get_locations()
        if not locations:
            return None

        # Filter locations for this country
        country_locations = [
            loc for loc in locations
            if str(loc.get("country_code") or "").lower() == country_code
        ]
        if not country_locations:
            log.warning("ExpressVPN: no locations found for country '%s'", country_code.upper())
            return None

        if not city_query:
            # Smart connection: random location in the country
            return random.choice(country_locations)

        # Match city query against location names
        return self._match_city(country_locations, city_query)

    def _match_city(self, locations: list[dict], city_query: str) -> Optional[dict]:
        """Match a short city query against a list of locations.

        Matching strategies (in priority order):
        1. First-letter abbreviation: ``"ny"`` matches ``"New York"``
        2. Exact slug match: ``"miami"`` matches ``"miami"``
        3. Prefix match on slug: ``"mia"`` matches ``"miami"``
        4. Substring match on slug: ``"york"`` matches ``"new-york"``

        Returns the first match from the highest-priority non-empty bucket.
        """
        city_query = city_query.strip().lower()
        abbreviation_hits: list[dict] = []
        exact_hits: list[dict] = []
        prefix_hits: list[dict] = []
        substring_hits: list[dict] = []

        for loc in locations:
            full_name = str(loc.get("name") or "")
            # Extract city part: strip country prefix like "USA - "
            city_part = re.sub(r"^[A-Z]{2,}(?:\s*-\s*)", "", full_name, count=1).strip()
            if not city_part:
                city_part = full_name

            city_slug = _slugify(city_part)

            # Strategy 1: first-letter abbreviation
            words = re.findall(r"[a-zA-Z]+", city_part)
            abbreviation = "".join(w[0] for w in words).lower() if words else ""
            if city_query == abbreviation:
                abbreviation_hits.append(loc)
                continue

            # Strategy 2: exact slug match
            if city_query == city_slug:
                exact_hits.append(loc)
                continue

            # Strategy 3: prefix match on slug
            if city_slug.startswith(city_query):
                prefix_hits.append(loc)
                continue

            # Strategy 4: substring match on slug
            if city_query in city_slug:
                substring_hits.append(loc)

        # Return the first match from the highest-priority non-empty bucket
        candidates = abbreviation_hits or exact_hits or prefix_hits or substring_hits
        if not candidates:
            log.warning("ExpressVPN: no city matched '%s' in available locations", city_query)
            return None

        if len(candidates) > 1:
            names = [c.get("name") for c in candidates]
            log.debug("ExpressVPN: city '%s' matched %d locations: %s", city_query, len(candidates), names)

        return candidates[0]

    def _resolve_location_by_slug(self, query: str) -> Optional[dict]:
        """Resolve a location by matching the full query against slugified names, IDs, or country codes."""
        target = query.strip().lower()
        locations = self._get_locations()
        if not locations:
            return None

        for location in locations:
            location_id = str(location.get("id") or "").lower()
            location_name = str(location.get("name") or "").lower()
            country_code = str(location.get("country_code") or "").lower()
            slug = _slugify(location_name)
            if target in (location_id, location_name, country_code, slug):
                return location
        return None

    def _pick_endpoint(self, endpoints: list[dict], server_num: Optional[int] = None) -> Optional[str]:
        """Select an endpoint host from the list, optionally by position index.

        Args:
            endpoints: List of endpoint dicts with ``"host"`` keys.
            server_num: 1-based server position index, or None for random.

        Returns:
            The selected hostname, or None if no endpoints are available.
        """
        hosts = [str(ep.get("host") or "").lower() for ep in endpoints if ep.get("host")]
        if not hosts:
            return None

        total = len(hosts)
        self._last_server_total = total

        if server_num is not None:
            if server_num < 1 or server_num > total:
                log.warning(
                    "ExpressVPN: server #%d is not available (only %d server%s), selecting random",
                    server_num, total, "s" if total != 1 else "",
                )
                chosen = random.choice(hosts)
                self._last_server_index = hosts.index(chosen) + 1
            else:
                chosen = hosts[server_num - 1]
                self._last_server_index = server_num
        else:
            chosen = random.choice(hosts)
            self._last_server_index = hosts.index(chosen) + 1

        self._last_endpoint_host = chosen
        return chosen

    # ------------------------------------------------------------------
    # Locations & Endpoints API
    # ------------------------------------------------------------------

    def _get_locations(self) -> list[dict]:
        if self._locations is not None:
            return self._locations

        srt = self._get_srt()
        if not srt:
            return []

        response = self._request(
            "GET",
            f"{self.API_BASE}/ids2/locations",
            headers=self._api_headers(srt),
            params={"protocols": "proxy"},
        )
        self._locations = response.json().get("locations", [])
        return self._locations

    def _get_endpoints_for_location(self, location: dict) -> list[dict]:
        srt = self._get_srt()
        if not srt:
            return []

        location_id = location.get("id")
        response = self._request(
            "POST",
            f"{self.API_BASE}/ids2/locations/{location_id}/instances",
            headers=self._api_headers(srt),
            params={"protocols": "proxy"},
            json={},
        )
        return response.json().get("endpoints", [])

    # ------------------------------------------------------------------
    # Token Management
    # ------------------------------------------------------------------

    def _get_tokens(self) -> dict:
        # Return in-memory cache if access_token is still valid
        if self._tokens:
            access_token = self._tokens.get("access_token")
            if access_token and not self._is_jwt_expired(access_token):
                return self._tokens

        cached = self._load_cached_tokens()
        tokens = {
            **cached,
            **{k: v for k, v in {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "connection_token": self.connection_token,
            }.items() if v},
        }

        access_token = tokens.get("access_token")
        if access_token and not self._is_jwt_expired(access_token):
            self._tokens = tokens
            return tokens

        refresh_token = tokens.get("refresh_token")
        if refresh_token:
            refreshed = self._refresh_access_token(refresh_token)
            if refreshed:
                tokens.update(
                    {
                        "access_token": refreshed.get("access_token"),
                        "refresh_token": refreshed.get("refresh_token") or refresh_token,
                    }
                )
                self._save_cached_tokens(tokens)
                self._tokens = tokens
                return tokens
            log.warning("ExpressVPN: refresh token failed or session expired")

        if self.account_json and self.account_json.is_file():
            try:
                data = json.loads(self.account_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                log.error("ExpressVPN: failed to read account_json %s: %s", self.account_json, error)
            else:
                tokens.update(
                    {
                        "access_token": data.get("accessToken"),
                        "connection_token": data.get("connectionToken"),
                        "subscription_id": data.get("subscriptionId"),
                    }
                )
                tokens = {k: v for k, v in tokens.items() if v}
                self._tokens = tokens
                return tokens

        if self.cookie_path.is_file():
            bootstrapped = self._run_pkce_bootstrap()
            if bootstrapped:
                tokens.update(
                    {
                        "access_token": bootstrapped.get("access_token"),
                        "refresh_token": bootstrapped.get("refresh_token"),
                    }
                )
                self._save_cached_tokens(tokens)
                self._tokens = tokens
                return tokens

        tokens = {k: v for k, v in tokens.items() if v}
        self._tokens = tokens
        return tokens

    def _refresh_access_token(self, refresh_token: str) -> Optional[dict]:
        response = self._request(
            "POST",
            f"{self.AUTH_BASE}/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.CLIENT_ID,
                "refresh_token": refresh_token,
            },
            headers=self._form_headers(),
            allow_error=True,
        )
        if not response.ok:
            log.warning("ExpressVPN: access token refresh failed with HTTP %s", response.status_code)
            return None
        return response.json()

    def _run_pkce_bootstrap(self) -> Optional[dict]:
        cookies = self._load_cookies()
        keycloak_identity = cookies.get("KEYCLOAK_IDENTITY")
        keycloak_session = cookies.get("KEYCLOAK_SESSION")
        auth_session_id = cookies.get("AUTH_SESSION_ID")
        if not keycloak_identity or not keycloak_session:
            log.error("ExpressVPN: KEYCLOAK_IDENTITY or KEYCLOAK_SESSION is missing from %s", self.cookie_path)
            return None

        verifier = base64.urlsafe_b64encode(
            secrets.token_bytes(32)
        ).decode("utf-8").rstrip("=")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("utf-8")).digest()
        ).decode("utf-8").rstrip("=")
        auth_cookies = requests.cookies.RequestsCookieJar()
        auth_cookies.set("KEYCLOAK_IDENTITY", keycloak_identity, domain="auth.expressvpn.com")
        auth_cookies.set("KEYCLOAK_SESSION", keycloak_session, domain="auth.expressvpn.com")
        if auth_session_id:
            auth_cookies.set("AUTH_SESSION_ID", auth_session_id, domain="auth.expressvpn.com")

        response = self._request(
            "GET",
            f"{self.AUTH_BASE}/auth",
            params={
                "client_id": self.CLIENT_ID,
                "response_type": "code",
                "redirect_uri": self.EXTENSION_REDIRECT_URI,
                "scope": "profile offline_access",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "ui_locales": "en",
            },
            headers=self._browser_headers(),
            cookies=auth_cookies,
            allow_redirects=False,
            allow_error=True,
        )

        redirect_url = response.headers.get("Location", "")
        code = parse_qs(urlparse(redirect_url).query).get("code", [None])[0]
        if not code:
            log.error("ExpressVPN: OAuth bootstrap did not return an authorization code")
            return None

        token_response = self._request(
            "POST",
            f"{self.AUTH_BASE}/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self.CLIENT_ID,
                "code": code,
                "redirect_uri": self.EXTENSION_REDIRECT_URI,
                "code_verifier": verifier,
            },
            headers=self._form_headers(),
            allow_error=True,
        )
        if not token_response.ok:
            log.error("ExpressVPN: OAuth token exchange failed with HTTP %s", token_response.status_code)
            return None
        return token_response.json()

    def _get_srt(self) -> Optional[str]:
        if self._srt and not self._is_jwt_expired(self._srt):
            return self._srt

        tokens = self._get_tokens()
        cached_srt = tokens.get("srt")
        if cached_srt and not self._is_jwt_expired(cached_srt):
            self._srt = cached_srt
            return self._srt

        access_token = tokens.get("access_token")
        if not access_token:
            log.error("ExpressVPN: access token was not available")
            return None

        response = self._request(
            "POST",
            f"{self.API_BASE}/srs2/subscription_receipts",
            headers=self._api_headers(access_token),
            json={},
        )
        receipts = response.json().get("srts", [])
        active_subscription_id = tokens.get("subscription_id")
        srt = self._select_srt(receipts, active_subscription_id)
        if not srt:
            log.error("ExpressVPN: no active subscription receipt was found")
            return None

        self._srt = srt
        tokens["srt"] = srt
        self._save_cached_tokens(tokens)
        return srt

    def _get_connection_token(self) -> Optional[str]:
        tokens = self._get_tokens()
        connection_token = tokens.get("connection_token")
        if connection_token and not self._is_jwt_expired(connection_token):
            return connection_token

        auth_token = self._get_srt() or tokens.get("access_token")
        if not auth_token:
            return connection_token

        response = self._request(
            "POST",
            f"{self.API_BASE}/srs2/connection_token",
            headers=self._api_headers(auth_token),
            json={},
        )
        body = response.json()
        connection_token = body.get("connection_token") or body.get("token")
        if connection_token:
            tokens["connection_token"] = connection_token
            self._save_cached_tokens(tokens)
        return connection_token

    # ------------------------------------------------------------------
    # Cookies & Cache
    # ------------------------------------------------------------------

    def _load_cookies(self) -> dict[str, str]:
        if not self.cookie_path.is_file():
            return {}

        try:
            content = self.cookie_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            log.error("ExpressVPN: failed to read cookies file %s: %s", self.cookie_path, error)
            return {}

        cookies: dict[str, str] = {}
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            for line in content.splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 7 and "expressvpn.com" in parts[0]:
                    cookies[parts[5]] = parts[6]
                elif "=" in line:
                    name, value = line.split("=", maxsplit=1)
                    cookies[name.strip()] = value.strip()
        else:
            if isinstance(data, list):
                for cookie in data:
                    if isinstance(cookie, dict) and "expressvpn.com" in str(cookie.get("domain", "")):
                        name = cookie.get("name")
                        value = cookie.get("value")
                        if name and value is not None:
                            cookies[str(name)] = str(value)
            elif isinstance(data, dict):
                cookies.update({str(k): str(v) for k, v in data.items()})
        return cookies

    def _load_cached_tokens(self) -> dict:
        if not self.cache_path.is_file():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            log.warning("ExpressVPN: failed to read token cache %s: %s", self.cache_path, error)
            return {}
        return data if isinstance(data, dict) else {}

    def _save_cached_tokens(self, tokens: dict) -> None:
        # Invalidate in-memory cache so next _get_tokens() re-reads
        self._tokens = tokens.copy()
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({k: v for k, v in tokens.items() if v}, indent=2)
            _write_private(self.cache_path, payload)
        except OSError as error:
            log.error("ExpressVPN: failed to save token cache %s: %s", self.cache_path, error)

    # ------------------------------------------------------------------
    # HTTP & Helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, url: str, allow_error: bool = False, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        response = requests.request(method, url, **kwargs)
        if response.ok or allow_error:
            return response
        raise ValueError(f"ExpressVPN request failed with HTTP {response.status_code}: {url}")

    def _api_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT,
            "X-Client-App-Version": "12.0.0",
            "X-Client-OS": "Windows",
            "X-Client-Device-Model": "Browser Extension",
        }

    def _browser_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

    def _form_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": self.USER_AGENT,
        }

    def _select_srt(self, receipts: list[dict], active_subscription_id: Optional[str]) -> Optional[str]:
        for receipt in receipts:
            srt = receipt.get("srt")
            if "xv.vpn" in self._decode_jwt_payload(srt).get("entitlements", {}):
                return srt

        if active_subscription_id:
            for receipt in receipts:
                if receipt.get("subscription_id") == active_subscription_id:
                    return receipt.get("srt")

        if receipts:
            return receipts[0].get("srt")
        return None

    def _decode_jwt_payload(self, token: Optional[str]) -> dict:
        if not token:
            return {}
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return {}
            payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
            return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}

    def _is_jwt_expired(self, token: str) -> bool:
        expires_at = self._decode_jwt_payload(token).get("exp")
        if not expires_at:
            # Tokens without ``exp`` are treated as non-expiring.  Keycloak
            # access/refresh tokens always carry ``exp``; proprietary SRT/
            # connection tokens may omit it — they are refreshed via the
            # normal SRT → connection_token flow anyway.
            return False
        return time.time() >= (int(expires_at) - 300)

    def _default_cookie_path(self) -> Path:
        cookies_dir = Path(config.directories.cookies)
        for folder in ("vpn", "vpns"):
            candidate = cookies_dir / folder / "expressvpn.txt"
            if candidate.is_file():
                return candidate
        return cookies_dir / "vpn" / "expressvpn.txt"

    def _default_cache_path(self) -> Path:
        cache_dir = Path(config.directories.cache)
        for folder in ("vpn", "vpns"):
            candidate = cache_dir / "global" / f"{folder}_expressvpn_tokens.json"
            if candidate.is_file():
                return candidate
        candidate = cache_dir / "global" / "expressvpn_tokens.json"
        if candidate.is_file():
            return candidate
        return cache_dir / "global" / "expressvpn_tokens.json"

    def close(self) -> None:
        pass

    def __enter__(self) -> ExpressVPN:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _write_private(path: Path, content: str) -> None:
    """Write *content* to *path* with owner-only permissions (0600).

    Opens with ``os.open`` so the file is **created** at 0600 — no window
    where it is world-readable.  On Windows ``os.fchmod`` is unavailable,
    so we fall back to ``path.chmod`` after close.
    """
    if hasattr(os, "fchmod"):
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
    else:
        path.write_text(content, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
