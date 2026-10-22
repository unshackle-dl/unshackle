"""Tell VPN servers apart by the network (AS) that hosts them, for the `@` proxy selector."""

from __future__ import annotations

import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

from unidecode import unidecode

WHOIS = ("whois.cymru.com", 43)
TIMEOUT = 3
NETWORKS: dict[str, str] = {}
FAILED: Optional[str] = None


def split_selector(query: str) -> tuple[str, Optional[str]]:
    """Split "fr:paris@-datacamp" into the region query and the network selector after "@"."""
    query, _, selector = query.partition("@")
    return query, selector or None


def lookup(ips: Iterable[str]) -> dict[str, str]:
    """
    Get the network (AS) name of each IP from Team Cymru's bulk whois. An IP it does not know is left out.

    A failure is kept for the process, so a blocked port 43 costs one timeout, not one per proxy.
    """
    global FAILED
    ips = list(dict.fromkeys(ips))
    wanted = [x for x in ips if x not in NETWORKS]
    if wanted:
        if FAILED:
            raise ConnectionError(FAILED)
        data = b""
        try:
            with socket.create_connection(WHOIS, timeout=TIMEOUT) as conn:
                conn.sendall(("begin\n" + "\n".join(wanted) + "\nend\n").encode())
                while chunk := conn.recv(65536):
                    data += chunk
        except OSError as e:
            FAILED = f"Could not get the networks of the VPN servers from {WHOIS[0]}: {e}"
            raise ConnectionError(FAILED) from None
        for line in data.decode("utf-8", "replace").splitlines():
            fields = [x.strip() for x in line.split("|")]
            if len(fields) == 3 and fields[0].isdigit():
                NETWORKS[fields[1]] = fields[2]
    return {x: NETWORKS[x] for x in ips if x in NETWORKS}


def fold(text: str) -> str:
    """Reduce a name to lowercase ASCII letters and digits, so "Frankfurt am Main" and "frankfurt-am-main" match."""
    return re.sub(r"[^a-z0-9]", "", unidecode(text).lower())


def short_name(name: str) -> str:
    """Shorten an AS name such as "CDNEXT - Datacamp Limited, GB" to "Datacamp Limited"."""
    return re.sub(r",\s*[A-Z]{2}$", "", name).split(" - ", 1)[-1]


def is_ip(selector: str) -> bool:
    """Whether a selector names one server IPv4 address, not a network."""
    try:
        ipaddress.IPv4Address(selector)
    except ValueError:
        return False
    return True


def select(servers: dict[str, str], selector: str) -> list[str]:
    """
    Get the servers (a server -> IP map) that match the selector, or with a leading "-", the servers that do not.

    An IPv4 selector matches that server IP. Any other selector matches a network whose name contains it,
    ignoring case, spaces and punctuation; a server whose network is not known is left out either way.
    """
    exclude = selector.startswith("-")
    if is_ip(address := selector.removeprefix("-")):
        return [x for x, ip in servers.items() if (ip == address) != exclude]
    selector = fold(address)
    if not selector:
        raise ValueError("A proxy network selector needs a network name after '@', for example @datacamp")
    networks = lookup(servers.values())
    return [x for x, ip in servers.items() if ip in networks and (selector in fold(networks[ip])) != exclude]


def resolve_all(hostnames: Iterable[str]) -> dict[str, str]:
    """Resolve hostnames to an IPv4 address each, in parallel. A hostname that does not resolve is left out."""

    def resolve(hostname: str) -> Optional[str]:
        try:
            return socket.gethostbyname(hostname)
        except OSError:
            return None

    hostnames = list(dict.fromkeys(hostnames))
    with ThreadPoolExecutor(16) as pool:
        return {x: ip for x, ip in zip(hostnames, pool.map(resolve, hostnames)) if ip}


def describe(hostname: str, ip: Optional[str] = None) -> str:
    """Name a server with its IP and network, for the line that says which proxy is in use. This never raises."""
    try:
        ip = ip or socket.gethostbyname(hostname)
        network = lookup([ip]).get(ip)
    except OSError:
        return f"{hostname} ({ip})" if ip else hostname
    return f"{hostname} ({ip}, {short_name(network)})" if network else f"{hostname} ({ip})"
