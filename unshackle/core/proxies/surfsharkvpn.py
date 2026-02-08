import json
import random
import re
from typing import Optional

import requests

from unshackle.core.proxies.proxy import Proxy


class SurfsharkVPN(Proxy):
    def __init__(self, username: str, password: str, server_map: Optional[dict[str, int]] = None):
        """
        Proxy Service using SurfsharkVPN Service Credentials.

        A username and password must be provided. These are Service Credentials, not your Login Credentials.
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
            raise TypeError(f"Expected server_map to be a dict mapping a region to a server ID, not '{server_map!r}'.")

        self.username = username
        self.password = password
        self.server_map = server_map or {}

        self.countries = self.get_countries()

    def __repr__(self) -> str:
        countries = len(set(x.get("country") for x in self.countries if x.get("country")))
        servers = sum(1 for x in self.countries if x.get("connectionName"))

        return f"{countries} Countr{['ies', 'y'][countries == 1]} ({servers} Server{['s', ''][servers == 1]})"

    def get_proxy(self, query: str) -> Optional[str]:
        """
        Get an HTTP(SSL) proxy URI for a SurfsharkVPN server.

        Supports:
        - Country code: "us", "ca", "gb"
        - Country ID: "228"
        - Specific server: "us-bos" (Boston)
        - City selection: "us:seattle", "ca:toronto"
        """
        query = query.lower()
        city = None

        # Check if query includes city specification (e.g., "us:seattle")
        if ":" in query:
            query, city = query.split(":", maxsplit=1)
            city = city.strip()

        if re.match(r"^[a-z]{2}\d+$", query):
            # country and surfsharkvpn server id, e.g., au-per, be-anr, us-bos
            hostname = f"{query}.prod.surfshark.com"
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
                # SurfsharkVPN doesnt have servers in this region
                return

            # Check server_map for pinned servers (can include city)
            server_map_key = f"{country['countryCode'].lower()}:{city}" if city else country["countryCode"].lower()
            server_mapping = self.server_map.get(server_map_key) or (
                self.server_map.get(country["countryCode"].lower()) if not city else None
            )

            if server_mapping:
                # country was set to a specific server ID in config
                hostname = f"{country['code'].lower()}{server_mapping}.prod.surfshark.com"
            else:
                # get the random server ID
                random_server = self.get_random_server(country["countryCode"], city)
                if not random_server:
                    raise ValueError(
                        f"The SurfsharkVPN Country {query} currently has no random servers. "
                        "Try again later. If the issue persists, double-check the query."
                    )
                hostname = random_server

        return f"https://{self.username}:{self.password}@{hostname}:443"

    def get_country(self, by_id: Optional[int] = None, by_code: Optional[str] = None) -> Optional[dict]:
        """Search for a Country and it's metadata."""
        if all(x is None for x in (by_id, by_code)):
            raise ValueError("At least one search query must be made.")

        for country in self.countries:
            if all(
                [
                    by_id is None or country["id"] == int(by_id),
                    by_code is None or country["countryCode"] == by_code.upper(),
                ]
            ):
                return country

    def get_random_server(self, country_id: str, city: Optional[str] = None):
        """
        Get a random server for a Country, optionally filtered by city.

        Args:
            country_id: The country code (e.g., "US", "CA")
            city: Optional city name to filter by (case-insensitive)

        Note: The API may include a 'location' field with city information.
        If not available, this will return any server from the country.
        """
        servers = [x for x in self.countries if x["countryCode"].lower() == country_id.lower()]

        # Filter by city if specified
        if city:
            city_lower = city.lower()
            # Check if servers have a 'location' field for city filtering
            city_servers = [
                x
                for x in servers
                if x.get("location", "").lower() == city_lower or x.get("city", "").lower() == city_lower
            ]

            if city_servers:
                servers = city_servers
            else:
                raise ValueError(
                    f"No servers found in city '{city}' for country '{country_id}'. "
                    "Try a different city or check the city name spelling."
                )

        # Get connection names from filtered servers
        if not servers:
            raise ValueError(f"Could not get random server for country '{country_id}': no servers found.")

        # Only include servers that actually have a connection name to avoid KeyError.
        connection_names = [x["connectionName"] for x in servers if "connectionName" in x]
        if not connection_names:
            raise ValueError(
                f"Could not get random server for country '{country_id}': no servers with connectionName found."
            )

        return random.choice(connection_names)

    @staticmethod
    def get_countries() -> list[dict]:
        """Get a list of available Countries and their metadata."""
        res = requests.get(
            url="https://api.surfshark.com/v3/server/clusters/all",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Content-Type": "application/json",
            },
        )
        if not res.ok:
            raise ValueError(f"Failed to get a list of SurfsharkVPN countries [{res.status_code}]")

        try:
            return res.json()
        except json.JSONDecodeError:
            raise ValueError("Could not decode list of SurfsharkVPN countries, not JSON data.")
