import json
import random
import re
from typing import Optional, Union

import requests

from unshackle.core.proxies.asn import describe, is_ip, mapped_query, select, split_selector
from unshackle.core.proxies.proxy import Proxy
from unshackle.core.utilities import COUNTRY_CODE_ALIASES


class NordVPN(Proxy):
    def __init__(self, username: str, password: str, server_map: Optional[dict[str, Union[int, str]]] = None):
        """
        Proxy provider that uses NordVPN Service Credentials.

        You must give a username and password. These are Service Credentials, not your Login Credentials.
        The Service Credentials can be found here: https://my.nordaccount.com/dashboard/nordvpn/
        """
        if not username:
            raise ValueError("No username for the NordVPN proxy provider")
        if not password:
            raise ValueError("No password for the NordVPN proxy provider")
        if not re.match(r"^[a-z0-9]{48}$", username + password, re.IGNORECASE) or "@" in username:
            raise ValueError("NordVPN needs its Service Credentials, not your login credentials")

        if server_map is not None and not isinstance(server_map, dict):
            raise TypeError(f"Expected server_map to be a dict mapping a region to a server ID, not '{server_map!r}'.")

        self.username = username
        self.password = password
        self.server_map = server_map or {}
        self.last_hostname: Optional[str] = None

        self.countries = self.get_countries()

    def __repr__(self) -> str:
        countries = len(self.countries)
        servers = sum(x["serverCount"] for x in self.countries)

        return f"{countries} Countr{['ies', 'y'][countries == 1]} ({servers} Server{['s', ''][servers == 1]})"

    def get_proxy(self, query: str) -> Optional[str]:
        """
        Get an HTTP(SSL) proxy URI for a NordVPN server.

        NordVPN disabled HTTP proxies under port 80 on the 15th of Feb, 2021:
        https://nordvpn.com/blog/removing-http-proxies

        Supports:
        - Country code: "us", "ca", "gb"
        - Country ID: "228"
        - Specific server: "us1234"
        - City selection: "us:seattle", "us:new-york", "ca:calgary"
        - Network selection: "ca@datacamp", "ca:toronto@-datacamp" (a leading "-" excludes)
        - One server IP: "fr@187.14.108.15"

        Returns None for a query NordVPN cannot serve: an unrecognised form, a country it does not list,
        or a country with no recommended servers. A city with no matching server raises ValueError.
        """
        query, selector = split_selector(query.lower())
        city = None

        if ":" in query:
            query, city = query.split(":", maxsplit=1)
            city = city.strip()

        if re.fullmatch(r"[a-z]{2}\d+", query):
            if selector:
                raise ValueError(f"'{query}' is already one NordVPN server, so it takes no '@' network selector.")
            hostname = f"{query}.proxy.nordvpn.com"
        else:
            if query.isdigit():
                country = self.get_country(by_id=int(query))
            elif re.fullmatch(r"[a-z]+", query):
                country = self.get_country(by_code=COUNTRY_CODE_ALIASES.get(query, query))
            else:
                return None
            if not country:
                return None

            server_map_key = f"{country['code'].lower()}:{city}" if city else country["code"].lower()
            server_mapping = self.server_map.get(server_map_key) or (
                self.server_map.get(country["code"].lower()) if not city else None
            )

            if server_mapping and not selector:
                if mapped := mapped_query(server_map_key, server_mapping):
                    return self.get_proxy(mapped)
                hostname = f"{country['code'].lower()}{server_mapping}.proxy.nordvpn.com"
            else:
                city_id = None
                if city:
                    # Recommendations are ordered by distance from the caller, so the API has to filter by city.
                    city_id = next(
                        (x["id"] for x in country.get("cities", []) if city in (x["name"].lower(), x.get("dns_name"))),
                        None,
                    )
                    if city_id is None:
                        raise ValueError(f"No servers found in city '{city}' for country '{country['name']}'")
                if selector and is_ip(selector):
                    recommended_servers = self.get_all_servers(country["id"])
                else:
                    recommended_servers = self.get_recommended_servers(
                        country["id"], city_id, limit=0 if selector else None
                    )
                if not recommended_servers:
                    return None
                hostnames = [x["hostname"] for x in recommended_servers]
                if selector:
                    hostnames = select({x["hostname"]: x["station"] for x in recommended_servers}, selector)
                    if not hostnames:
                        raise ValueError(f"No NordVPN server in {query} matches '@{selector}'.")

                hostname = random.choice(hostnames)

        if hostname.startswith("gb"):
            # NordVPN uses the alpha2 of 'GB' in API responses, but 'UK' in the hostname
            hostname = f"uk{hostname[2:]}"

        if hostname.endswith(".nordvpn.com") and not hostname.endswith(".proxy.nordvpn.com"):
            hostname = hostname[: -len(".nordvpn.com")] + ".proxy.nordvpn.com"

        self.last_hostname = hostname
        return f"https://{self.username}:{self.password}@{hostname}:89"

    def last_connection_display(self) -> Optional[str]:
        """Name the last server with its IP and network, so a blocked network can be excluded with '@-'."""
        return describe(self.last_hostname) if self.last_hostname else None

    def get_country(self, by_id: Optional[int] = None, by_code: Optional[str] = None) -> Optional[dict]:
        """Find a Country and its metadata."""
        if all(x is None for x in (by_id, by_code)):
            raise ValueError("At least one search query must be made.")

        for country in self.countries:
            if all(
                [by_id is None or country["id"] == int(by_id), by_code is None or country["code"] == by_code.upper()]
            ):
                return country

    @staticmethod
    def get_all_servers(country_id: int) -> list[dict]:
        """Get the hostname and IP of every server in a Country, recommended or not."""
        res = requests.get(
            url="https://api.nordvpn.com/v1/servers",
            params={
                "filters[country_id]": country_id,
                "limit": 0,
                "fields[servers.hostname]": 1,
                "fields[servers.station]": 1,
            },
            timeout=10,
        )
        if not res.ok:
            raise ValueError(f"Failed to get a list of NordVPN servers [{res.status_code}]")

        try:
            return res.json()
        except json.JSONDecodeError:
            raise ValueError("Could not decode list of NordVPN servers, not JSON data.")

    @staticmethod
    def get_recommended_servers(
        country_id: int, city_id: Optional[int] = None, limit: Optional[int] = None
    ) -> list[dict]:
        """
        Get the list of recommended Servers for a Country, or for one of its cities.

        Note: There may not always be more than one recommended server. A limit of 0 gets all of them.
        """
        params = {"filters[country_id]": country_id}
        if city_id is not None:
            params["filters[country_city_id]"] = city_id
        if limit is not None:
            params.update({"limit": limit, "fields[servers.hostname]": 1, "fields[servers.station]": 1})
        res = requests.get(url="https://api.nordvpn.com/v1/servers/recommendations", params=params, timeout=10)
        if not res.ok:
            raise ValueError(f"Failed to get a list of NordVPN countries [{res.status_code}]")

        try:
            return res.json()
        except json.JSONDecodeError:
            raise ValueError("Could not decode list of NordVPN countries, not JSON data.")

    @staticmethod
    def get_countries() -> list[dict]:
        """Get a list of available Countries and their metadata."""
        res = requests.get(url="https://api.nordvpn.com/v1/servers/countries", timeout=10)
        if not res.ok:
            raise ValueError(f"Failed to get a list of NordVPN countries [{res.status_code}]")

        try:
            return res.json()
        except json.JSONDecodeError:
            raise ValueError("Could not decode list of NordVPN countries, not JSON data.")
