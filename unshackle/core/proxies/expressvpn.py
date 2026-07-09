from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

from unshackle.core.config import config
from unshackle.core.proxies.proxy import Proxy

log = logging.getLogger("proxies.expressvpn")


class ExpressVPN(Proxy):
    """
    ExpressVPN HTTPS proxy provider.

    Mirrors the ExpressVPN Android TV app: an OAuth 2.0 device authorization grant obtains
    API tokens (interactive on first run, silent via cached refresh token afterwards), a
    subscription receipt (SRT) resolves proxy-capable locations, and get_proxy() returns an
    authenticated HTTPS proxy URL (https://cat:<token>@server:443).

    Query format (after the provider prefix, e.g. "expressvpn:ca"):
        ca              random location in the country (smart connection)
        us-ny           city
        us-ny-2 / usny2 pinned server by position
        usa-new-york    full ExpressVPN location slug
        host.expressprovider.com  direct hostname
    """

    CLIENT_ID = "8V18PnFYlrYnnYvRlnxKwifxxhYKfjIG"
    AUTH_BASE = "https://auth.expressvpn.com/oauth"
    API_BASE = "https://cp.expressapisv2.net"
    SCOPE = "openid profile email offline_access"
    DEVICE_POLL_TIMEOUT = 600
    USER_AGENT = "okhttp/5.3.2"

    def __init__(
        self,
        region_map: Optional[dict[str, str]] = None,
        server_map: Optional[dict[str, str]] = None,
        refresh_token: Optional[str] = None,
        access_token: Optional[str] = None,
        connection_token: Optional[str] = None,
        account_json: Optional[str] = None,
        cache_path: Optional[str] = None,
        timeout: float = 10.0,
        enable: bool = False,
    ):
        """
        Proxy Service using ExpressVPN app proxy credentials.

        region_map maps a country code to an optional city/server preset (e.g. "us": "ny-02")
        applied when the CLI query names only the country; empty values mean smart connection.
        server_map maps aliases to location slugs or concrete .expressprovider.com hosts.
        Tokens are optional: without them, cached tokens are used, else account_json is read.
        Set enable: true to run the one-time interactive device login when no session exists.
        """
        if region_map is not None and not isinstance(region_map, dict):
            raise TypeError(f"Expected region_map to be a dict mapping aliases to locations, not '{region_map!r}'.")
        if server_map is not None and not isinstance(server_map, dict):
            raise TypeError(f"Expected server_map to be a dict mapping aliases to locations, not '{server_map!r}'.")

        self.region_map = {
            str(k).lower().strip(): (str(v).lower().strip() if v else None) for k, v in (region_map or {}).items()
        }
        self.server_map = {str(k).lower().strip(): str(v).lower().strip() for k, v in (server_map or {}).items()}
        self.refresh_token = refresh_token or None
        self.access_token = access_token or None
        self.connection_token = connection_token or None
        self.account_json = Path(account_json).expanduser() if account_json else None
        self.timeout = timeout
        self.enable = enable

        self.tokens: Optional[dict] = None
        self.srt: Optional[str] = None
        self.locations: Optional[list[dict]] = None
        self.last_name: Optional[str] = None
        self.last_index: Optional[int] = None
        self.last_total: Optional[int] = None
        self.last_host: Optional[str] = None

        self.cache_path = Path(cache_path).expanduser() if cache_path else self.default_cache_path()

    def __repr__(self) -> str:
        if self.locations is not None:
            countries = len({str(x.get("country_code") or "").upper() for x in self.locations if x.get("country_code")})
            servers = len(self.locations)
            return (
                f"{countries} Countr{'ies' if countries != 1 else 'y'} ({servers} Server{'s' if servers != 1 else ''})"
            )
        aliases = len(self.region_map) + len(self.server_map)
        if aliases:
            return f"{aliases} Region Alias{'es' if aliases != 1 else ''} (ExpressVPN HTTPS Proxy)"
        return "ExpressVPN HTTPS Proxy"

    def get_proxy(self, query: str) -> Optional[str]:
        endpoint = self.resolve_endpoint(query.strip().lower())
        if not endpoint:
            return None

        connection_token = self.get_connection_token()
        if not connection_token:
            log.error("ExpressVPN: connection token was not available")
            return None

        log.debug("ExpressVPN proxy ready: %s", self.last_connection_display() or endpoint)
        return f"https://cat:{connection_token}@{endpoint}:443"

    def last_connection_display(self) -> Optional[str]:
        if not self.last_name or not self.last_host:
            return None
        parts = [self.last_name]
        if self.last_index is not None and self.last_total is not None:
            parts.append(f"#{self.last_index} of {self.last_total}")
        return f"({', '.join(parts)}): {self.last_host.replace('.expressprovider.com', '')}"

    def resolve_endpoint(self, query: str) -> Optional[str]:
        self.last_name = self.last_index = self.last_total = self.last_host = None
        query = self.server_map.get(query) or query

        if "expressprovider" in query:
            self.last_host = query if query.endswith(".expressprovider.com") else f"{query}.expressprovider.com"
            return self.last_host

        country, city, server_num = self.parse_query(query)
        if country and not city and (preset := self.region_map.get(country)):
            _, city, server_num = self.parse_query(f"{country}-{preset}")

        if country:
            location = self.resolve_location(country, city)
        else:
            location = self.resolve_slug(query)
        if not location:
            log.warning("ExpressVPN: no location matched query '%s'", query)
            return None

        self.last_name = location.get("name")
        endpoints = self.get_endpoints(location)
        if not endpoints:
            log.error("ExpressVPN: no proxy endpoints returned for %s", location.get("name") or query)
            return None
        return self.pick_endpoint(endpoints, server_num)

    def parse_query(self, query: str) -> tuple[Optional[str], Optional[str], Optional[int]]:
        """Parse "us", "us-ny", "us-ny-2"/"us-ny2" into (country, city, server number)."""
        server_num = None
        num_match = re.match(r"^(.+?)[-]?(\d+)$", query)
        if num_match and re.search(r"[a-z]", num_match.group(1).rstrip("-")) and 1 <= int(num_match.group(2)) <= 99:
            query = num_match.group(1).rstrip("-")
            server_num = int(num_match.group(2))

        match = re.match(r"^([a-z]{2})(?:-(.+))?$", query)
        if match:
            valid = {str(x.get("country_code") or "").lower() for x in self.get_locations()}
            if match.group(1) in valid:
                return match.group(1), match.group(2) or None, server_num

        # not a country[-city] pattern; caller falls back to slug resolution
        return None, None, None

    def resolve_location(self, country_code: str, city_query: Optional[str] = None) -> Optional[dict]:
        pool = [x for x in self.get_locations() if str(x.get("country_code") or "").lower() == country_code]
        if not pool:
            log.warning("ExpressVPN: no locations found for country '%s'", country_code.upper())
            return None
        if not city_query:
            return random.choice(pool)
        return self.match_city(pool, city_query)

    def match_city(self, locations: list[dict], city_query: str) -> Optional[dict]:
        """Match a city query by abbreviation ("ny"), exact slug, slug prefix, then substring."""
        city_query = city_query.strip().lower()
        abbrev, exact, prefix, substring = [], [], [], []
        for loc in locations:
            # location names look like "USA - New York"; keep the city part
            city = re.sub(r"^[A-Z]{2,}(?:\s*-\s*)", "", str(loc.get("name") or ""), count=1).strip()
            slug = slugify(city)
            abbreviation = "".join(w[0] for w in re.findall(r"[a-zA-Z]+", city)).lower()
            if city_query == abbreviation:
                abbrev.append(loc)
            elif city_query == slug:
                exact.append(loc)
            elif slug.startswith(city_query):
                prefix.append(loc)
            elif city_query in slug:
                substring.append(loc)

        candidates = abbrev or exact or prefix or substring
        if not candidates:
            log.warning("ExpressVPN: no city matched '%s' in available locations", city_query)
            return None
        if len(candidates) > 1:
            log.debug("ExpressVPN: city '%s' matched %d locations", city_query, len(candidates))
        return candidates[0]

    def resolve_slug(self, query: str) -> Optional[dict]:
        for loc in self.get_locations():
            name = str(loc.get("name") or "").lower()
            keys = (str(loc.get("id") or "").lower(), name, str(loc.get("country_code") or "").lower(), slugify(name))
            if query in keys:
                return loc
        return None

    def pick_endpoint(self, endpoints: list[dict], server_num: Optional[int] = None) -> Optional[str]:
        hosts = [str(x.get("host") or "").lower() for x in endpoints if x.get("host")]
        if not hosts:
            return None

        self.last_total = total = len(hosts)
        if server_num is not None and not 1 <= server_num <= total:
            log.warning(
                "ExpressVPN: server #%d not available (%d server%s), picking random",
                server_num,
                total,
                "s" if total != 1 else "",
            )
            server_num = None
        self.last_index = server_num if server_num is not None else random.randint(1, total)
        self.last_host = hosts[self.last_index - 1]
        return self.last_host

    def get_locations(self) -> list[dict]:
        if self.locations is None:
            srt = self.get_srt()
            if not srt:
                return []
            response = self.request(
                "GET", f"{self.API_BASE}/ids2/locations", headers=self.api_headers(srt), params={"protocols": "proxy"}
            )
            self.locations = response.json().get("locations", [])
        return self.locations

    def get_endpoints(self, location: dict) -> list[dict]:
        srt = self.get_srt()
        if not srt:
            return []
        response = self.request(
            "POST",
            f"{self.API_BASE}/ids2/locations/{location.get('id')}/instances",
            headers=self.api_headers(srt),
            params={"protocols": "proxy"},
            json={},
        )
        return response.json().get("endpoints", [])

    def get_tokens(self) -> dict:
        if self.tokens and not jwt_expired(self.tokens.get("access_token")):
            return self.tokens

        overrides = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "connection_token": self.connection_token,
        }
        tokens = {**self.load_cache(), **{k: v for k, v in overrides.items() if v}}

        if tokens.get("access_token") and not jwt_expired(tokens["access_token"]):
            self.tokens = tokens
            return tokens

        if refresh_token := tokens.get("refresh_token"):
            refreshed = self.refresh_access_token(refresh_token)
            if refreshed:
                # refresh tokens rotate; keep the new one, fall back to the old if omitted
                tokens["access_token"] = refreshed.get("access_token")
                tokens["refresh_token"] = refreshed.get("refresh_token") or refresh_token
                self.save_cache(tokens)
                return tokens
            log.warning("ExpressVPN: refresh token failed or session expired")

        if self.account_json and self.account_json.is_file():
            try:
                data = json.loads(self.account_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                log.error("ExpressVPN: failed to read account_json %s: %s", self.account_json, error)
            else:
                tokens.update(
                    access_token=data.get("accessToken"),
                    connection_token=data.get("connectionToken"),
                    subscription_id=data.get("subscriptionId"),
                )
                self.tokens = {k: v for k, v in tokens.items() if v}
                return self.tokens

        if self.enable and sys.stdin.isatty():
            if bootstrapped := self.device_login():
                tokens["access_token"] = bootstrapped.get("access_token")
                tokens["refresh_token"] = bootstrapped.get("refresh_token") or tokens.get("refresh_token")
                self.save_cache(tokens)
                return tokens
        elif not tokens.get("access_token"):
            log.error(
                "ExpressVPN: no session available; set proxy_providers.expressvpn.enable: true "
                "for the one-time device login"
            )

        self.tokens = {k: v for k, v in tokens.items() if v}
        return self.tokens

    def refresh_access_token(self, refresh_token: str) -> Optional[dict]:
        response = self.request(
            "POST",
            f"{self.AUTH_BASE}/token",
            data={"grant_type": "refresh_token", "client_id": self.CLIENT_ID, "refresh_token": refresh_token},
            headers=self.form_headers(),
            allow_error=True,
        )
        if not response.ok:
            log.warning("ExpressVPN: access token refresh failed with HTTP %s", response.status_code)
            return None
        return response.json()

    def device_login(self) -> Optional[dict]:
        """One-time interactive bootstrap via the OAuth 2.0 device authorization grant (RFC 8628)."""
        response = self.request(
            "POST",
            f"{self.AUTH_BASE}/device/code",
            data={"client_id": self.CLIENT_ID, "scope": self.SCOPE, "audience": ""},
            headers=self.form_headers(),
            allow_error=True,
        )
        if not response.ok:
            log.error("ExpressVPN: device code request failed with HTTP %s", response.status_code)
            return None
        data = response.json()

        log.info(
            "ExpressVPN: sign in to authorize this device: open %s and enter code %s",
            data.get("verification_uri", f"{self.AUTH_BASE}/../realms/xvpn/device"),
            data.get("user_code"),
        )

        interval = int(data.get("interval") or 5)
        deadline = time.time() + int(data.get("expires_in") or self.DEVICE_POLL_TIMEOUT)
        while time.time() < deadline:
            time.sleep(interval)
            poll = self.request(
                "POST",
                f"{self.AUTH_BASE}/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.CLIENT_ID,
                    "device_code": data.get("device_code"),
                },
                headers=self.form_headers(),
                allow_error=True,
            )
            if poll.ok:
                return poll.json()
            error = poll.json().get("error")
            if error == "slow_down":
                interval += 5
            elif error != "authorization_pending":
                log.error("ExpressVPN: device authorization failed: %s", error)
                return None
        log.error("ExpressVPN: device authorization timed out before approval")
        return None

    def get_srt(self) -> Optional[str]:
        if self.srt and not jwt_expired(self.srt):
            return self.srt

        tokens = self.get_tokens()
        if (cached := tokens.get("srt")) and not jwt_expired(cached):
            self.srt = cached
            return cached

        access_token = tokens.get("access_token")
        if not access_token:
            log.error("ExpressVPN: access token was not available")
            return None

        response = self.request(
            "POST", f"{self.API_BASE}/srs2/subscription_receipts", headers=self.api_headers(access_token), json={}
        )
        srt = self.select_srt(response.json().get("srts", []), tokens.get("subscription_id"))
        if not srt:
            log.error("ExpressVPN: no active subscription receipt was found")
            return None

        self.srt = tokens["srt"] = srt
        self.save_cache(tokens)
        return srt

    def get_connection_token(self) -> Optional[str]:
        tokens = self.get_tokens()
        connection_token = tokens.get("connection_token")
        if connection_token and not jwt_expired(connection_token):
            return connection_token

        auth_token = self.get_srt() or tokens.get("access_token")
        if not auth_token:
            return connection_token

        response = self.request(
            "POST", f"{self.API_BASE}/srs2/connection_token", headers=self.api_headers(auth_token), json={}
        )
        body = response.json()
        # the API returns the token under "cat"
        connection_token = body.get("cat") or body.get("connection_token") or body.get("token")
        if connection_token:
            tokens["connection_token"] = connection_token
            self.save_cache(tokens)
        return connection_token

    def select_srt(self, receipts: list[dict], subscription_id: Optional[str]) -> Optional[str]:
        for receipt in receipts:
            srt = receipt.get("srt")
            if "xv.vpn" in jwt_payload(srt).get("entitlements", {}):
                return srt
        if subscription_id:
            for receipt in receipts:
                if receipt.get("subscription_id") == subscription_id:
                    return receipt.get("srt")
        return receipts[0].get("srt") if receipts else None

    def load_cache(self) -> dict:
        if not self.cache_path.is_file():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            log.warning("ExpressVPN: failed to read token cache %s: %s", self.cache_path, error)
            return {}
        return data if isinstance(data, dict) else {}

    def save_cache(self, tokens: dict) -> None:
        self.tokens = tokens.copy()
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            write_private(self.cache_path, json.dumps({k: v for k, v in tokens.items() if v}, indent=2))
        except OSError as error:
            log.error("ExpressVPN: failed to save token cache %s: %s", self.cache_path, error)

    def request(self, method: str, url: str, allow_error: bool = False, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        response = requests.request(method, url, **kwargs)
        if response.ok or allow_error:
            return response
        raise ValueError(f"ExpressVPN request failed with HTTP {response.status_code}: {url}")

    def api_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT,
            "X-Client-App-Version": "12.69.0",
            "X-Client-OS": "Android",
            "X-Client-Device-Model": "SHIELD Android TV",
        }

    def form_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": self.USER_AGENT,
        }

    def default_cache_path(self) -> Path:
        return Path(config.directories.cache) / "vpn" / "expressvpn_tokens.json"


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def jwt_payload(token: Optional[str]) -> dict:
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


def jwt_expired(token: Optional[str]) -> bool:
    """Treat tokens without an exp claim (proprietary SRT/connection tokens) as non-expiring."""
    if not token:
        return True
    expires_at = jwt_payload(token).get("exp")
    if not expires_at:
        return False
    return time.time() >= (int(expires_at) - 300)


def write_private(path: Path, content: str) -> None:
    """Write content to path created with owner-only (0600) permissions."""
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
