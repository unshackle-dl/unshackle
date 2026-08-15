from __future__ import annotations

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
from rich.text import Text

from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.proxies.proxy import Proxy

log = logging.getLogger("proxies.proton")


class ProtonVPN(Proxy):
    """
    Proton VPN HTTPS proxy provider.

    Authenticates via TV login (a self-sustaining session cached at <cache>/vpn/protonvpn.json, the
    only refreshable kind) or an exported account.proton.me cookie session (AUTH-<UID>, access only —
    re-export on expiry). Resolves a server by country/city, mints short-lived proxy credentials, and
    returns an authenticated HTTPS proxy URL (https://user:pass@server:4443).

    Query format (after the provider prefix, e.g. "protonvpn:us"):
        us             random server in the country (paid preferred over free)
        us12           Proton server #12 (the number shown in the connection log)
        us:ny          city, NordVPN-style

    Free Proton accounts work, limited to the free-tier exit countries.
    """

    API_BASE = "https://account.proton.me/api"
    API_HOSTS = ("account.proton.me", "account.protonvpn.com")
    APP_VERSION = "browser-vpn@1.3.5"
    PROXY_PORT = 4443
    SECURE_CORE_PORT = 443
    SECURE_CORE_FEATURE = 1  # Features bitmask: 1=SecureCore, 2=Tor, 4=P2P, 8=Streaming, 16=IPv6
    TOR_FEATURE = 2
    EXCLUDE_FEATURES = SECURE_CORE_FEATURE | TOR_FEATURE  # skip Secure Core + Tor; both are slow for a plain proxy
    TOKEN_DURATION = 1200  # free tier caps the granted lifetime lower
    COUNTRY_ALIASES = {"gb": "uk"}  # Proton labels the United Kingdom "UK", not the ISO "GB"

    TV_API_BASE = "https://vpn-api.proton.me"
    TV_APP_VERSION = "android_tv-vpn@5.18.75.1+play"
    TV_USER_AGENT = "ProtonVPN/5.18.75.1 (Android 11; NVIDIA SHIELD Android TV)"
    TV_ACCEPT = "application/vnd.protonmail.v1+json"
    POLL_TIMEOUT = 600.0
    POLL_INTERVAL = 5.0

    def __init__(
        self,
        server_map: Optional[dict[str, str]] = None,
        cookie_path: Optional[str] = None,
        cache_path: Optional[str] = None,
        timeout: float = 10.0,
        enable: bool = False,
    ):
        self.server_map = {str(k).lower().strip(): str(v).lower().strip() for k, v in (server_map or {}).items()}
        self.timeout = timeout
        self.enable = enable
        self.api_base = self.API_BASE

        self.uid: Optional[str] = None
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.logicals: Optional[list[dict]] = None
        self.max_tier: Optional[int] = None
        self.last_name: Optional[str] = None
        self.last_city: Optional[str] = None
        self.last_host: Optional[str] = None

        self.cookie_path = Path(cookie_path).expanduser() if cookie_path else self.default_cookie_path()
        self.cache_path = Path(cache_path).expanduser() if cache_path else self.default_cache_path()

    def __repr__(self) -> str:
        try:
            # only fetch the catalog if a session exists; never start TV login here (that belongs in get_proxy)
            if self.access_token or self.cookie_path.is_file() or self.cache_path.is_file():
                servers = self.get_logicals()
            else:
                servers = self.logicals or []
        except Exception:
            servers = self.logicals or []
        if not servers:
            return "Proton VPN HTTPS Proxy"
        countries = len({str(s.get("ExitCountry") or "").upper() for s in servers if s.get("ExitCountry")})
        return (
            f"{countries} Countr{'ies' if countries != 1 else 'y'} ({len(servers)} Server{'s' if servers != 1 else ''})"
        )

    def get_proxy(self, query: str) -> Optional[str]:
        self.ensure_session()
        server = self.resolve_server(query.strip().lower())
        if not server:
            return None
        creds = self.get_browser_token()
        if not creds:
            log.error("Proton: could not obtain proxy credentials")
            return None

        self.last_host = host = str(server.get("Domain") or "")
        port = self.SECURE_CORE_PORT if int(server.get("Features") or 0) & self.SECURE_CORE_FEATURE else self.PROXY_PORT
        # Username (a JWT) and Password are URL-safe, but quote so requests rebuilds Basic auth cleanly
        user, pwd = (requests.utils.quote(c, safe="") for c in creds)
        return f"https://{user}:{pwd}@{host}:{port}"

    def last_connection_display(self) -> Optional[str]:
        if not self.last_name or not self.last_host:
            return None
        label = f"{self.last_name} - {self.last_city}" if self.last_city else self.last_name
        return f"({label}): {self.last_host.replace('.protonvpn.net', '')}"

    def resolve_server(self, query: str) -> Optional[dict]:
        self.last_name = self.last_city = self.last_host = None
        query = self.server_map.get(query, query)
        country, city, server_num = self.parse_query(query)

        pool = []
        if country:
            max_tier = self.get_max_tier()  # only servers the account can actually connect to
            pool = [
                s
                for s in self.get_logicals()
                if str(s.get("ExitCountry") or "").lower() == country
                and s.get("Status") == 1
                and int(s.get("Tier") or 0) <= max_tier
                and not int(s.get("Features") or 0) & self.EXCLUDE_FEATURES
            ]
            if city:
                pool = self.match_city(pool, city)

        if not pool:
            log.warning("Proton: no server matched query '%s'", query)
            return None
        return self.select(pool, server_num)

    def parse_query(self, query: str) -> tuple[Optional[str], Optional[str], Optional[int]]:
        """Parse "us", "us12"/"us-12" (server #12), and "us:ny" (city)."""
        city = None
        if ":" in query:
            query, city = query.split(":", 1)
            city = city.strip() or None

        match = re.match(r"^([a-z]{2})-?(\d+)?$", query.strip())
        if not match:
            return None, None, None
        country = self.COUNTRY_ALIASES.get(match.group(1), match.group(1))
        server_num = int(match.group(2)) if match.group(2) else None

        valid = {str(s.get("ExitCountry") or "").lower() for s in self.get_logicals()}
        return (country, city, server_num) if country in valid else (None, None, None)

    def match_city(self, servers: list[dict], city_query: str) -> list[dict]:
        city_query = city_query.strip().lower()
        exact, prefix, substring = [], [], []
        for s in servers:
            city = str(s.get("City") or "")
            slug = slugify(city)
            abbreviation = "".join(w[0] for w in re.findall(r"[a-z]+", city.lower()))
            if city_query in (slug, abbreviation):
                exact.append(s)
            elif slug.startswith(city_query):
                prefix.append(s)
            elif city_query in slug:
                substring.append(s)
        return exact or prefix or substring

    def select(self, pool: list[dict], server_num: Optional[int]) -> dict:
        if server_num is not None:
            matches = [s for s in pool if server_no(s) == server_num]
            if matches:
                pool = matches
            else:
                log.warning("Proton: server #%d not found (%d available), picking random", server_num, len(pool))
        else:
            pool = [s for s in pool if int(s.get("Tier") or 0) > 0] or pool
        chosen = random.choice(pool)
        self.last_name = chosen.get("Name")
        self.last_city = chosen.get("City")
        return chosen

    def get_logicals(self) -> list[dict]:
        if self.logicals is None:
            response = self.api("GET", "/vpn/v1/logicals")
            self.logicals = (response.json().get("LogicalServers") or []) if response is not None else []
        return self.logicals

    def get_max_tier(self) -> int:
        """The account's plan tier (0=free, 2=plus). Caps which servers it can connect to."""
        if self.max_tier is None:
            response = self.api("GET", "/vpn")
            vpn = (response.json().get("VPN") or {}) if response is not None else {}
            self.max_tier = int(vpn.get("MaxTier") or 0)
        return self.max_tier

    def get_browser_token(self) -> Optional[tuple[str, str]]:
        response = self.api("GET", f"/vpn/v1/browser/token?Duration={self.TOKEN_DURATION}")
        if response is None:
            return None
        body = response.json()
        if body.get("Username") and body.get("Password"):
            return body["Username"], body["Password"]
        log.error("Proton: browser/token response missing credentials")
        return None

    def api(self, method: str, path: str) -> Optional[requests.Response]:
        """Call the Proton API, refreshing the session once on a 401."""
        if not self.access_token:
            self.load_session()
        if not self.access_token:
            log.error(
                "Proton: no session available; export account.proton.me cookies to %s "
                "or set proxy_providers.protonvpn.enable: true for the one-time TV login",
                self.cookie_path,
            )
            return None

        for attempt in range(2):
            response = requests.request(method, f"{self.api_base}{path}", headers=self.headers(), timeout=self.timeout)
            if response.status_code == 401 and attempt == 0 and self.refresh_session():
                continue
            if response.status_code == 401:
                log.error(
                    "Proton: session expired; re-export cookies to %s or use TV login",
                    self.cookie_path,
                )
                return None
            if not response.ok:
                raise ValueError(f"Proton request failed with HTTP {response.status_code}: {path}")
            return response
        return None

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "x-pm-uid": self.uid or "",
            "x-pm-appversion": self.APP_VERSION,
            "Accept": "application/json",
        }

    def refresh_session(self) -> bool:
        if not self.refresh_token or not self.uid:
            return False
        response = requests.post(
            f"{self.api_base}/auth/refresh",
            json={
                "UID": self.uid,
                "ResponseType": "token",
                "GrantType": "refresh_token",
                "RefreshToken": self.refresh_token,
                "RedirectURI": "https://protonvpn.com",
            },
            headers={"x-pm-uid": self.uid, "x-pm-appversion": self.APP_VERSION, "Accept": "application/json"},
            timeout=self.timeout,
        )
        if not response.ok:
            log.warning("Proton: session refresh failed with HTTP %s", response.status_code)
            return False
        body = response.json()
        self.access_token = body.get("AccessToken") or self.access_token
        self.refresh_token = body.get("RefreshToken") or self.refresh_token
        self.save_session()
        return True

    def load_session(self) -> None:
        cached = self.load_cache()

        cookie_uid = cookie_access = cookie_host = None
        for name, value, domain in self.load_cookies():
            host = domain.lstrip(".") if domain else ""
            if host and host not in self.API_HOSTS:
                continue
            if name.startswith("AUTH-"):
                cookie_uid, cookie_access, cookie_host = name[len("AUTH-") :], value, host

        # The TV cache is the only refreshable session
        cookie_newer = (
            self.cookie_path.is_file()
            and self.cache_path.is_file()
            and self.cookie_path.stat().st_mtime > self.cache_path.stat().st_mtime
        )
        if cached.get("access_token") and not cookie_newer:
            self.uid = cached["uid"]
            self.access_token = cached["access_token"]
            self.refresh_token = cached.get("refresh_token")
            self.api_base = cached.get("api_base") or self.api_base
            return

        # Cookie session: access token only. Browser sessions can't refresh headlessly
        self.uid, self.access_token, self.refresh_token = cookie_uid, cookie_access, None
        if cookie_host:
            self.api_base = f"https://{cookie_host}/api"

    def ensure_session(self) -> None:
        if not self.access_token:
            self.load_session()
        if not self.access_token and self.enable and sys.stdin.isatty():
            self.tv_login()

    def tv_login(self) -> bool:
        """Fork a TV session and have the user approve the code at proton.me/tv; tokens refresh like the cookie path."""
        headers = {
            "x-pm-appversion": self.TV_APP_VERSION,
            "x-pm-locale": "en",
            "User-Agent": self.TV_USER_AGENT,
            "Accept": self.TV_ACCEPT,
        }
        try:
            response = requests.get(f"{self.TV_API_BASE}/auth/v4/sessions/forks", headers=headers, timeout=self.timeout)
            response.raise_for_status()
            fork = response.json()
        except (requests.RequestException, ValueError) as error:
            log.error("Proton: failed to start TV login: %s", error)
            return False

        selector, user_code = fork.get("Selector"), fork.get("UserCode")
        if not selector or not user_code:
            log.error("Proton: unexpected TV login response")
            return False

        indent = " " * 5
        prompt = (
            "Proton: open https://account.proton.me/vpn/tv/code\n"
            f"Enter code {user_code}, then press Enter to continue..."
        )
        try:
            console.input(Text(indent + prompt.replace("\n", "\n" + indent), style="text"))
        except EOFError:
            return False

        poll_url = f"{self.TV_API_BASE}/auth/v4/sessions/forks/{selector}"
        deadline = time.monotonic() + self.POLL_TIMEOUT
        while time.monotonic() < deadline:
            response = requests.get(poll_url, headers=headers, timeout=self.timeout)
            if response.status_code == 422:  # not approved yet
                time.sleep(self.POLL_INTERVAL)
                continue
            if not response.ok:
                log.error("Proton: TV login poll failed with HTTP %s", response.status_code)
                return False
            try:
                tokens = response.json()
            except ValueError:
                log.error("Proton: unexpected TV login token response")
                return False
            uid, access, refresh = tokens.get("UID"), tokens.get("AccessToken"), tokens.get("RefreshToken")
            if uid and access and refresh:
                self.uid, self.access_token, self.refresh_token = uid, access, refresh
                self.save_session()
                log.info("Proton: TV login successful")
                return True
            log.error("Proton: TV login response missing tokens")
            return False

        log.error("Proton: TV login not approved in time; run the command again to retry")
        return False

    def load_cookies(self) -> list[tuple[str, str, str]]:
        """Return [(name, value, domain), ...] from a Netscape cookies.txt or JSON cookie list."""
        if not self.cookie_path.is_file():
            return []
        try:
            content = self.cookie_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            log.error("Proton: failed to read cookies file %s: %s", self.cookie_path, error)
            return []

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Netscape: domain \t flag \t path \t secure \t expiry \t name \t value
            # Proton's AUTH-/REFRESH- cookies are HttpOnly, exported as "#HttpOnly_<domain>\t..."
            out = []
            for line in content.splitlines():
                line = line.removeprefix("#HttpOnly_")
                parts = line.split("\t")
                if not line.startswith("#") and len(parts) >= 7:
                    out.append((parts[5], parts[6], parts[0]))
            return out

        items = data.get("cookies") if isinstance(data, dict) else data
        return [
            (str(c["name"]), str(c.get("value", "")), str(c.get("domain", "")))
            for c in (items or [])
            if isinstance(c, dict) and c.get("name")
        ]

    def load_cache(self) -> dict:
        if not self.cache_path.is_file():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            log.warning("Proton: failed to read token cache %s: %s", self.cache_path, error)
            return {}
        return data if isinstance(data, dict) else {}

    def save_session(self) -> None:
        tokens = {
            "uid": self.uid,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "api_base": self.api_base,
        }
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            write_private(self.cache_path, json.dumps({k: v for k, v in tokens.items() if v}, indent=2))
        except OSError as error:
            log.error("Proton: failed to save token cache %s: %s", self.cache_path, error)

    def default_cookie_path(self) -> Path:
        cookies_dir = Path(config.directories.cookies)
        for folder in ("vpn", "vpns"):
            for fname in ("protonvpn.txt", "proton.txt"):
                if (candidate := cookies_dir / folder / fname).is_file():
                    return candidate
        return cookies_dir / "vpn" / "protonvpn.txt"

    def default_cache_path(self) -> Path:
        return Path(config.directories.cache) / "vpn" / "protonvpn.json"


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def server_no(server: dict) -> Optional[int]:
    """Proton's server number from the logical name, e.g. "US#231" -> 231, "US-CO#21" -> 21."""
    match = re.search(r"#(\d+)", str(server.get("Name") or ""))
    return int(match.group(1)) if match else None


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
