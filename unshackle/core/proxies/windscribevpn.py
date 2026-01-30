import json
import random
import re
from typing import Optional

import requests

from unshackle.core.proxies.proxy import Proxy


class WindscribeVPN(Proxy):
    def __init__(self, username: str, password: str, server_map: Optional[dict[str, str]] = None):
        """
        Proxy Service using WindscribeVPN Service Credentials.

        A username and password must be provided. These are Service Credentials, not your Login Credentials.
        The Service Credentials can be found here: https://windscribe.com/getconfig/openvpn
        """
        if not username:
            raise ValueError("No Username was provided to the WindscribeVPN Proxy Service.")
        if not password:
            raise ValueError("No Password was provided to the WindscribeVPN Proxy Service.")

        if server_map is not None and not isinstance(server_map, dict):
            raise TypeError(f"Expected server_map to be a dict mapping a region to a hostname, not '{server_map!r}'.")

        self.username = username
        self.password = password
        self.server_map = server_map or {}

        self.countries = self.get_countries()

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
        """Get an HTTPS proxy URI for a WindscribeVPN server."""
        query = query.lower()

        if query in self.server_map:
            hostname = self.server_map[query]
        else:
            if re.match(r"^[a-z]+$", query):
                hostname = self.get_random_server(query)
            else:
                raise ValueError(f"The query provided is unsupported and unrecognized: {query}")

            if not hostname:
                return None

        hostname = hostname.split(':')[0]
        return f"https://{self.username}:{self.password}@{hostname}:443"

    def get_random_server(self, country_code: str) -> Optional[str]:
        """
        Get a random server hostname for a country.

        Returns None if no servers are available for the country.
        """
        for location in self.countries:
            if location.get("country_code", "").lower() == country_code.lower():
                hostnames = []
                for group in location.get("groups", []):
                    for host in group.get("hosts", []):
                        if hostname := host.get("hostname"):
                            hostnames.append(hostname)

                if hostnames:
                    return random.choice(hostnames)

        return None

    @staticmethod
    def get_countries() -> list[dict]:
        """Get a list of available Countries and their metadata."""
        res = requests.get(
            url="https://assets.windscribe.com/serverlist/firefox/1/1",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Content-Type": "application/json",
            },
        )
        if not res.ok:
            raise ValueError(f"Failed to get a list of WindscribeVPN locations [{res.status_code}]")

        try:
            data = res.json()
            return data.get("data", [])
        except json.JSONDecodeError:
            raise ValueError("Could not decode list of WindscribeVPN locations, not JSON data.")
