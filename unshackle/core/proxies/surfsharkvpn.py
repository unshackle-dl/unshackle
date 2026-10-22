import asyncio
import json
import logging
import random
import re
import socket
import ssl
import threading
from functools import cache, partial
from typing import Optional

import certifi
import requests

from unshackle.core.proxies.asn import describe, fold, is_ip, mapped_query, select, split_selector
from unshackle.core.proxies.controld import build_query, parse_a_records
from unshackle.core.proxies.proxy import Proxy
from unshackle.core.utilities import COUNTRY_CODE_ALIASES

log = logging.getLogger("proxies.surfsharkvpn")

POOL_QUERIES = 48
PORT = 443
CONNECT_TIMEOUT = 30
CHUNK = 65536
TLS = ssl.create_default_context(cafile=certifi.where())  # the same CA bundle as requests
PINS: dict[tuple[str, str], int] = {}
LOOP: Optional[asyncio.AbstractEventLoop] = None
STATE = threading.Lock()


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy bytes one way until the source closes, then close the sink."""
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def tunnel(ip: str, hostname: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Carry one client connection over TLS to the server at `ip`, with its certificate checked against `hostname`."""
    try:
        upstream = await asyncio.wait_for(
            asyncio.open_connection(ip, PORT, ssl=TLS, server_hostname=hostname), CONNECT_TIMEOUT
        )
    except OSError as e:
        log.warning(f"Could not reach {hostname} at {ip}: {e}")
        writer.close()
        return
    await asyncio.gather(pipe(reader, upstream[1]), pipe(upstream[0], writer))


@cache
def nameserver(zone: str) -> str:
    """Get the IPv4 address of a zone's first authoritative nameserver, through Google's DNS-over-HTTPS JSON API."""
    res = requests.get("https://dns.google/resolve", params={"name": zone, "type": "NS"}, timeout=10)
    res.raise_for_status()
    return socket.gethostbyname([x["data"] for x in res.json().get("Answer", []) if x.get("type") == 2][0])


def pinned(ip: str, hostname: str) -> int:
    """
    Get a loopback port that carries each connection over TLS to one server IP, starting it on first use.

    The client still speaks the HTTP proxy protocol with the credentials itself: the port only fixes the IP,
    which a proxy URI cannot do because the certificate names the hostname, not the IP.
    """
    global LOOP
    with STATE:
        if (ip, hostname) not in PINS:
            if LOOP is None:
                LOOP = asyncio.new_event_loop()
                threading.Thread(target=LOOP.run_forever, daemon=True, name="surfshark").start()
            server = asyncio.run_coroutine_threadsafe(
                asyncio.start_server(partial(tunnel, ip, hostname), "127.0.0.1", 0), LOOP
            ).result()
            PINS[ip, hostname] = server.sockets[0].getsockname()[1]
        return PINS[ip, hostname]


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

        self.last_server: Optional[tuple[str, str]] = None
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
        - Network selection: "fr-bod@datacamp", "fr@-hostroyale" (a leading "-" excludes)
        - One server IP: "fr-bod@195.86.24.103"

        A network or IP selection goes through a loopback port pinned to the server IP (see `pinned`).
        Returns None for a query that names no SurfsharkVPN country or server, so that the
        next proxy provider can answer it. A city with no matching server raises ValueError.
        """
        query, selector = split_selector(query.strip().lower())
        city = None

        if ":" in query:
            query, city = query.split(":", maxsplit=1)
            city = city.strip()
        query = COUNTRY_CODE_ALIASES.get(query, query)

        server_map_key = f"{query}:{city}" if city else query
        mapped = self.server_map.get(server_map_key)
        as_query = mapped_query(server_map_key, mapped) if mapped else None
        if as_query and not selector:
            return self.get_proxy(as_query)
        if mapped and not as_query:
            servers = [mapped]
        elif not city and any(x["connectionName"].split(".", 1)[0] == query for x in self.countries):
            servers = [query]
        elif re.fullmatch(r"[a-z]{2}", query):
            in_country = [x for x in self.countries if x["countryCode"] == query.upper()]
            if not in_country:
                return None
            servers = [x["connectionName"] for x in self.get_city_servers(in_country, city)]
        else:
            return None

        hostnames = [self.get_hostname(x) for x in servers]
        self.last_server = None
        if not selector:
            return f"https://{self.username}:{self.password}@{random.choice(hostnames)}:443"
        if is_ip(selector):
            hostname, ip = random.choice(hostnames), selector
        else:
            hostname, ip = self.get_server_on_network(hostnames, selector)
        self.last_server = (hostname, ip)
        return f"http://{self.username}:{self.password}@127.0.0.1:{pinned(ip, hostname)}"

    def last_connection_display(self) -> Optional[str]:
        """Name the pinned server with its IP and network, so it can be pinned again or excluded with '@-'."""
        return describe(*self.last_server) if self.last_server else None

    def get_server_on_network(self, hostnames: list[str], selector: str) -> tuple[str, str]:
        """Get a random (hostname, IP) that matches the selector (see `asn.select`), trying the clusters in random order."""
        for hostname in random.sample(hostnames, len(hostnames)):
            ips = select({ip: ip for ip in self.get_pool(hostname)}, selector)
            if ips:
                return hostname, random.choice(ips)
        raise ValueError(f"No SurfsharkVPN server matches '@{selector}'.")

    @staticmethod
    def get_pool(hostname: str) -> list[str]:
        """
        Get the server IPs behind a cluster hostname.

        Its nameserver answers with two of them at random, so this sends it many queries at once and keeps every answer.
        """
        ips: set[str] = set()
        try:
            address = nameserver(hostname.split(".", 1)[1])
            query = build_query(hostname)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1)
                for _ in range(POOL_QUERIES):
                    sock.sendto(query, (address, 53))
                try:
                    for _ in range(POOL_QUERIES):
                        ips.update(ip for ip, _ in parse_a_records(sock.recv(4096)))
                except TimeoutError:
                    pass
        except (requests.RequestException, ValueError, IndexError, OSError) as e:
            raise ValueError(f"Could not list the SurfsharkVPN servers of {hostname}: {e}") from None
        if not ips:
            raise ValueError(f"The nameserver of {hostname} did not answer, so its servers could not be listed.")
        return sorted(ips)

    @staticmethod
    def get_hostname(server: str) -> str:
        """Expand a server name such as 'us-dal' to its full hostname."""
        server = server.lower()
        return server if server.endswith(".surfshark.com") else f"{server}.prod.surfshark.com"

    def get_city_servers(self, servers: list[dict], city: Optional[str] = None) -> list[dict]:
        """
        Get a country's servers, optionally filtered by city.

        A city matches with case, spaces, hyphens and accents ignored ("new-york" matches "New York"),
        exactly first, then as a prefix ("frankfurt" matches "Frankfurt am Main").
        """
        if city:
            wanted = fold(city)
            located = [(fold(x.get("location", "")), x) for x in servers]
            servers = [x for location, x in located if location == wanted] or [
                x for location, x in located if location.startswith(wanted)
            ]
            if not servers:
                raise ValueError(
                    f"No servers found in city '{city}'. Try a different city or check the city name spelling."
                )

        return servers

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
