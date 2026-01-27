import base64
import json
import re
from typing import Optional

from urllib.parse import quote

import requests

from unshackle.core.proxies.proxy import Proxy


class WindscribeVPN(Proxy):
    def __init__(self, username: str, password: str, server_map: Optional[dict[str, str]] = None):
        """
        Proxy Service using WindscribeVPN Service Credentials.

        A username and password must be provided. These are Service Credentials, not your Login Credentials.
        The Service Credentials can be found login in through the Windscribe Extension.
        Both username and password are Base64 encoded.
        """

        if not username:
            raise ValueError("No Username was provided to the WindscribeVPN Proxy Service.")
        if not password:
            raise ValueError("No Password was provided to the WindscribeVPN Proxy Service.")

        if server_map is not None and not isinstance(server_map, dict):
            raise TypeError(f"Expected server_map to be a dict mapping a region to a hostname, not '{server_map!r}'.")

        self.username = self._try_decode(username)
        self.password = self._try_decode(password)
        self.server_map = server_map or {}

        self.countries = self.get_countries()

    @staticmethod
    def _try_decode(value: str) -> str:
        """
        Attempt to decode a Base64 string, returning original if failed.
        """
        try:
            return base64.b64decode(value).decode("utf-8")
        except Exception:
            return value

    def __repr__(self) -> str:
        countries = len(set(x.get("country_code") for x in self.countries if x.get("country_code")))
        servers = sum(
            len(host)
            for location in self.countries
            for group in location.get("groups", [])
            for host in group.get("hosts", [])
        )

        return f"{countries} Countr{['ies', 'y'][countries == 1]} ({servers} Server{['s', ''][servers == 1]})"

    def get_proxy(self, query: str) -> Optional[str]:
        """
        Get an HTTPS proxy URI for a WindscribeVPN server.
        Supports:
          - Country code: "us", "ca", "gb"
          - City selection: "us:seattle", "ca:toronto"
          - Server code: "us-central-096", "uk-london-055"
        Note: Windscribes static OpenVPN credentials from the configurator are per server use the extension credentials.
        """
        query = query.lower()
        city = None

        # Check if query includes city specification (e.g., "ca:toronto")
        if ":" in query:
            query, city = query.split(":", maxsplit=1)
            city = city.strip()

        safe_username = quote(self.username, safe="")
        safe_password = quote(self.password, safe="")

        proxy = f"https://{safe_username}:{safe_password}@"

        server_map_key = f"{query}:{city}" if city else query
        try:
            if server_map_key in self.server_map:
                # Use a forced server from server_map if provided
                hostname = f"{self.server_map[server_map_key]}.totallyacdn.com"
            elif "-" in query and not city:
                # Supports server codes like "windscribe:us-central-096"
                hostname = f"{query}.totallyacdn.com"
            else:
                # Query is likely a country code (e.g., "us") or country+city (e.g., "us:seattle") and not in server_map
                if re.match(r"^[a-z]+$", query):
                    hostname = self.get_random_server(query, city)
                else:
                    raise ValueError(f"The query provided is unsupported and unrecognized: {query}")
        except ValueError as e:
            raise Exception(f"Windscribe Proxy Error: {e}")
        if not hostname:
            raise Exception(f"Windscribe has no servers for {query!r}")

        return f"{proxy}{hostname}:443"

    def get_random_server(self, country_code: str, city: Optional[str]) -> Optional[str]:
        """
        Get a random server hostname for a country.
        Args:
            country_code: The country code (e.g., "us", "ca")
            city: Optional city name to filter by (case-insensitive)
        Returns:
          The hostname of a server in the specified country (and city if provided).

        - If city is provided but not found, falls back to any server in the country.
        Raise error if no servers are available for the country.
        """

        country_code = country_code.lower()

        # Find the country entry
        location = next(
            (c for c in self.countries if c.get("country_code", "").lower() == country_code),
            None,
        )

        if not location:
            raise ValueError(f"No servers found for country code '{country_code}'.")

        all_hosts = []
        city_hosts = []

        for group in location.get("groups", []):
            group_city = group.get("city", "").lower()

            for host in group.get("hosts", []):
                entry = {
                    "hostname": host["hostname"],
                    "health": host.get("health", float("inf")),
                }
                all_hosts.append(entry)

                if city and group_city == city.lower():
                    city_hosts.append(entry)

        # Prefer city-specific servers if available and select the healthiest
        if city_hosts:
            return min(city_hosts, key=lambda x: x["health"])["hostname"]

        # Fallback to country-level servers and select the healthiest
        if all_hosts:
            return min(all_hosts, key=lambda x: x["health"])["hostname"]

        # Country exists but has zero servers
        raise ValueError(
            f"No servers found in city '{city}' for country code '{country_code}'. Try a different city or check the city name spelling."
        )

    @staticmethod
    def get_countries() -> list[dict]:
        """Get a list of available Countries and their metadata."""
        res = requests.get(
            url="https://assets.windscribe.com/serverlist/chrome/1/937dd9fcfba6925d7a9253ab34e655a453719e02",
            headers={
                "Host": "assets.windscribe.com",
                "Connection": "keep-alive",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            },
        )
        if not res.ok:
            raise ValueError(f"Failed to get a list of WindscribeVPN locations [{res.status_code}]")

        try:
            data = res.json()
            return data.get("data", [])
        except json.JSONDecodeError:
            raise ValueError("Could not decode list of WindscribeVPN locations, not JSON data.")
