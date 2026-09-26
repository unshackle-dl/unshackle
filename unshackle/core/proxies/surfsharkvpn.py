import json
import logging
import random
import re
from typing import Optional

import requests
from unidecode import unidecode

from unshackle.core.proxies.proxy import Proxy
from unshackle.core.utilities import COUNTRY_CODE_ALIASES

log = logging.getLogger("proxies.surfsharkvpn")


class SurfsharkVPN(Proxy):
    def __init__(self, username: str, password: str, server_map: Optional[dict[str, str]] = None):
        """
        Proxy provider that uses SurfsharkVPN Service Credentials.

        You must give a username and password. These are Service Credentials, not your Login Credentials.
        The Service Credentials can be found here: https://my.surfshark.com/vpn/manual-setup/main/openvpn
        """
        if not username:
            raise ValueError("No Username was provided to the SurfsharkVPN Proxy Service.")
        if not password:
            raise ValueError("No Password was provided to the SurfsharkVPN Proxy Service.")
        if not re.match(r"^[a-z0-9]{48}$", username + password, re.IGNORECASE) or "@" in username:
            raise ValueError(
                "The Username and Password must be SurfsharkVPN Service Credentials, not your Login Credentials. "
                "The Service Credentials can be found here: https://my.surfshark.com/vpn/manual-setup/main/openvpn"
            )

        if server_map is not None and not isinstance(server_map, dict):
            raise TypeError(
                f"Expected server_map to be a dict mapping a region to a server name, not '{server_map!r}'."
            )

        self.username = username
        self.password = password
        self.server_map: dict[str, str] = {}
        for region, server in (server_map or {}).items():
            server = str(server or "").strip()
            if not server or server.isdigit():
                # A warning, not an error: one stale entry must not disable the whole proxy provider.
                log.warning(
                    "SurfsharkVPN: ignoring server_map entry '%s: %s'. Use a server name such as 'us-dal'. "
                    "SurfsharkVPN no longer uses numeric server IDs.",
                    region,
                    server,
                )
                continue
            country, sep, city = str(region).lower().partition(":")
            self.server_map[COUNTRY_CODE_ALIASES.get(country, country) + sep + city] = server

        self.countries = self.get_countries()

    def __repr__(self) -> str:
        countries = len(set(x.get("country") for x in self.countries if x.get("country")))
        servers = sum(1 for x in self.countries if x.get("connectionName"))

        return f"{countries} Countr{['ies', 'y'][countries == 1]} ({servers} Server{['s', ''][servers == 1]})"

    def get_proxy(self, query: str) -> Optional[str]:
        """
        Get an HTTP(SSL) proxy URI for a SurfsharkVPN server.

        Supports:
        - Country code: "us", "ca", "gb" (or "uk")
        - Specific server: "us-bos" (Boston)
        - City selection: "us:seattle", "us:new-york"

        Returns None for a query that names no SurfsharkVPN country or server, so that the
        next proxy provider can answer it. A city with no matching server raises ValueError.
        """
        query = query.strip().lower()
        city = None

        if ":" in query:
            query, city = query.split(":", maxsplit=1)
            city = city.strip()
        query = COUNTRY_CODE_ALIASES.get(query, query)

        server_map_key = f"{query}:{city}" if city else query
        if server_map_key in self.server_map:
            server = self.server_map[server_map_key]
        elif not city and any(x["connectionName"].split(".", 1)[0] == query for x in self.countries):
            server = query
        elif re.fullmatch(r"[a-z]{2}", query):
            servers = [x for x in self.countries if x["countryCode"] == query.upper()]
            if not servers:
                return None
            server = self.get_random_server(servers, city)
        else:
            return None

        return f"https://{self.username}:{self.password}@{self.get_hostname(server)}:443"

    @staticmethod
    def get_hostname(server: str) -> str:
        """Expand a server name such as 'us-dal' to its full hostname."""
        server = server.lower()
        return server if server.endswith(".surfshark.com") else f"{server}.prod.surfshark.com"

    def get_random_server(self, servers: list[dict], city: Optional[str] = None) -> str:
        """
        Get a random server name from a country's servers, optionally filtered by city.

        A city matches with case, spaces, hyphens and accents ignored ("new-york" matches "New York"),
        exactly first, then as a prefix ("frankfurt" matches "Frankfurt am Main").
        """
        if city:
            wanted = self.fold(city)
            located = [(self.fold(x.get("location", "")), x) for x in servers]
            servers = [x for location, x in located if location == wanted] or [
                x for location, x in located if location.startswith(wanted)
            ]
            if not servers:
                raise ValueError(
                    f"No servers found in city '{city}'. Try a different city or check the city name spelling."
                )

        return random.choice(servers)["connectionName"]

    @staticmethod
    def fold(text: str) -> str:
        """Reduce a place name to lowercase ASCII letters and digits."""
        return re.sub(r"[^a-z0-9]", "", unidecode(text).lower())

    @staticmethod
    def get_countries() -> list[dict]:
        """Get a list of available Countries and their metadata."""
        res = requests.get(
            url="https://api.surfshark.com/v3/server/clusters/all",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if not res.ok:
            raise ValueError(f"Failed to get a list of SurfsharkVPN countries [{res.status_code}]")

        try:
            return res.json()
        except json.JSONDecodeError:
            raise ValueError("Could not decode list of SurfsharkVPN countries, not JSON data.")
