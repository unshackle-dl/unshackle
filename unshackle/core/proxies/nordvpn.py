import json
import random
import re
from typing import Optional

import requests

from unshackle.core.proxies.proxy import Proxy


class NordVPN(Proxy):
    def __init__(self, username: str, password: str, server_map: Optional[dict[str, int]] = None):
        """
        Proxy Service using NordVPN Service Credentials.

        A username and password must be provided. These are Service Credentials, not your Login Credentials.
        The Service Credentials can be found here: https://my.nordaccount.com/dashboard/nordvpn/
        """
        if not username:
            raise ValueError("No Username was provided to the NordVPN Proxy Service.")
        if not password:
            raise ValueError("No Password was provided to the NordVPN Proxy Service.")
        if not re.match(r"^[a-z0-9]{48}$", username + password, re.IGNORECASE) or "@" in username:
            raise ValueError(
                "The Username and Password must be NordVPN Service Credentials, not your Login Credentials. "
                "The Service Credentials can be found here: https://my.nordaccount.com/dashboard/nordvpn/"
            )

        if server_map is not None and not isinstance(server_map, dict):
            raise TypeError(f"Expected server_map to be a dict mapping a region to a server ID, not '{server_map!r}'.")

        self.username = username
        self.password = password
        self.server_map = server_map or {}

        self.countries = self.get_countries()

    def __repr__(self) -> str:
        countries = len(self.countries)
        servers = sum(x["serverCount"] for x in self.countries)

        return f"{countries} Countr{['ies', 'y'][countries == 1]} ({servers} Server{['s', ''][servers == 1]})"

    def get_proxy(self, query: str) -> Optional[str]:
        """
        Get an HTTP(SSL) proxy URI for a NordVPN server.

        HTTP proxies under port 80 were disabled on the 15th of Feb, 2021:
        https://nordvpn.com/blog/removing-http-proxies

        Supports:
        - Country code: "us", "ca", "gb"
        - Country ID: "228"
        - Specific server: "us1234"
        - City selection: "us:seattle", "ca:calgary"
        """
        query = query.lower()
        city = None

        # Check if query includes city specification (e.g., "ca:calgary")
        if ":" in query:
            query, city = query.split(":", maxsplit=1)
            city = city.strip()

        if re.match(r"^[a-z]{2}\d+$", query):
            # country and nordvpn server id, e.g., us1, fr1234
            hostname = f"{query}.nordvpn.com"
        else:
            if query.isdigit():
                # country id
                country = self.get_country(by_id=int(query))
            elif re.match(r"^[a-z]+$", query):
                # country code
                country = self.get_country(by_code=query)
            else:
                raise ValueError(f"The query provided is unsupported and unrecognized: {query}")
            if not country:
                # NordVPN doesnt have servers in this region
                return

            # Check server_map for pinned servers (can include city)
            server_map_key = f"{country['code'].lower()}:{city}" if city else country["code"].lower()
            server_mapping = self.server_map.get(server_map_key) or (
                self.server_map.get(country["code"].lower()) if not city else None
            )

            if server_mapping:
                # country was set to a specific server ID in config
                hostname = f"{country['code'].lower()}{server_mapping}.nordvpn.com"
            else:
                # get the recommended server ID
                recommended_servers = self.get_recommended_servers(country["id"])
                if not recommended_servers:
                    raise ValueError(
                        f"The NordVPN Country {query} currently has no recommended servers. "
                        "Try again later. If the issue persists, double-check the query."
                    )

                # Filter by city if specified
                if city:
                    city_servers = self.filter_servers_by_city(recommended_servers, city)
                    if not city_servers:
                        raise ValueError(
                            f"No servers found in city '{city}' for country '{country['name']}'. "
                            "Try a different city or check the city name spelling."
                        )
                    recommended_servers = city_servers

                # Pick a random server from the filtered list
                hostname = random.choice(recommended_servers)["hostname"]

        if hostname.startswith("gb"):
            # NordVPN uses the alpha2 of 'GB' in API responses, but 'UK' in the hostname
            hostname = f"gb{hostname[2:]}"

        return f"https://{self.username}:{self.password}@{hostname}:89"

    def get_country(self, by_id: Optional[int] = None, by_code: Optional[str] = None) -> Optional[dict]:
        """Search for a Country and it's metadata."""
        if all(x is None for x in (by_id, by_code)):
            raise ValueError("At least one search query must be made.")

        for country in self.countries:
            if all(
                [by_id is None or country["id"] == int(by_id), by_code is None or country["code"] == by_code.upper()]
            ):
                return country

    @staticmethod
    def filter_servers_by_city(servers: list[dict], city: str) -> list[dict]:
        """
        Filter servers by city name.

        The API returns servers with location data that includes city information.
        This method filters servers to only those in the specified city.

        Args:
            servers: List of server dictionaries from the NordVPN API
            city: City name to filter by (case-insensitive)

        Returns:
            List of servers in the specified city
        """
        city_lower = city.lower()
        filtered = []

        for server in servers:
            # Each server has a 'locations' list with location data
            locations = server.get("locations", [])
            for location in locations:
                # City data can be in different formats:
                # - {"city": {"name": "Seattle", ...}}
                # - {"city": "Seattle"}
                city_data = location.get("city")
                if city_data:
                    # Handle both dict and string formats
                    city_name = city_data.get("name") if isinstance(city_data, dict) else city_data
                    if city_name and city_name.lower() == city_lower:
                        filtered.append(server)
                        break  # Found a match, no need to check other locations for this server

        return filtered

    @staticmethod
    def get_recommended_servers(country_id: int) -> list[dict]:
        """
        Get the list of recommended Servers for a Country.

        Note: There may not always be more than one recommended server.
        """
        res = requests.get(
            url="https://api.nordvpn.com/v1/servers/recommendations", params={"filters[country_id]": country_id}
        )
        if not res.ok:
            raise ValueError(f"Failed to get a list of NordVPN countries [{res.status_code}]")

        try:
            return res.json()
        except json.JSONDecodeError:
            raise ValueError("Could not decode list of NordVPN countries, not JSON data.")

    @staticmethod
    def get_countries() -> list[dict]:
        """Get a list of available Countries and their metadata."""
        res = requests.get(
            url="https://api.nordvpn.com/v1/servers/countries",
        )
        if not res.ok:
            raise ValueError(f"Failed to get a list of NordVPN countries [{res.status_code}]")

        try:
            return res.json()
        except json.JSONDecodeError:
            raise ValueError("Could not decode list of NordVPN countries, not JSON data.")
