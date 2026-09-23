from __future__ import annotations

import base64
import ipaddress
import logging
import random
import re
import socket
import socketserver
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from unshackle.core.proxies.proxy import Proxy

log = logging.getLogger("ControlD")

API = "https://api.controld.com"
DOH_TIMEOUT = 10
MIN_TTL = 30
MAX_TTL = 3600
HEAD_TIMEOUT = 30
IDLE_TIMEOUT = 600
CHUNK = 65536
PREFIX = "unshackle-"
SCHEME = "controld://"
RESOLVER_ID = re.compile(r"[a-z0-9]+", re.IGNORECASE)
MAX_FORWARDERS = 64


def doh_url(resolver: str) -> str:
    """Get the DoH URL of a Control D endpoint's resolver ID."""
    return f"https://dns.controld.com/{resolver}"


def build_query(host: str) -> bytes:
    """Build a minimal DNS A-record query (RFC 1035) for wireformat DoH (RFC 8484)."""
    labels = host.rstrip(".").encode("idna").split(b".")
    qname = b"".join(bytes([len(label)]) + label for label in labels) + b"\x00"
    # header: id 0 (unused by DoH), RD set, 1 question
    return b"\x00\x00\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + qname + b"\x00\x01\x00\x01"


def skip_name(buf: bytes, i: int) -> int:
    """Return the offset past the domain name at `i`. A compression pointer ends it."""
    while i < len(buf):
        length = buf[i]
        if length == 0:
            return i + 1
        if length & 0xC0 == 0xC0:
            return i + 2
        i += length + 1
    return i


def parse_a_records(answer: bytes) -> list[tuple[str, int]]:
    """Return the (IPv4, TTL) pairs of a DNS response, in order."""
    if len(answer) < 12:
        return []
    questions = int.from_bytes(answer[4:6], "big")
    answers = int.from_bytes(answer[6:8], "big")
    i = 12
    for _ in range(questions):
        i = skip_name(answer, i) + 4  # QTYPE + QCLASS
    records = []
    for _ in range(answers):
        i = skip_name(answer, i)
        if i + 10 > len(answer):
            break
        rtype = int.from_bytes(answer[i : i + 2], "big")
        ttl = int.from_bytes(answer[i + 4 : i + 8], "big")
        rdlength = int.from_bytes(answer[i + 8 : i + 10], "big")
        rdata = answer[i + 10 : i + 10 + rdlength]
        i += 10 + rdlength
        if rtype == 1 and len(rdata) == 4:
            records.append((".".join(str(octet) for octet in rdata), ttl))
    return records


class IPv4Adapter(HTTPAdapter):
    """Connect over IPv4 only: bound to 0.0.0.0, an IPv6 address fails and the next one is tried."""

    def init_poolmanager(self, *args: object, **kwargs: object) -> None:
        kwargs["source_address"] = ("0.0.0.0", 0)  # nosec B104 - a source address to bind, not a listener
        super().init_poolmanager(*args, **kwargs)


class Resolver:
    def __init__(self, url: str):
        """Resolve hostnames through one Control D DoH endpoint, caching answers for their TTL."""
        self.url = url
        self.session = requests.Session()
        # Control D redirects only IPv4 queries and authorises the proxy by the asking IP: no IPv6, no env proxy.
        self.session.trust_env = False
        self.session.mount("https://", IPv4Adapter())
        self.cache: dict[str, tuple[str, float]] = {}
        self.lock = threading.Lock()

    def resolve(self, host: str) -> str:
        host = host.lower()
        with self.lock:
            cached = self.cache.get(host)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        query = base64.urlsafe_b64encode(build_query(host)).rstrip(b"=").decode()
        response = self.session.get(
            self.url,
            params={"dns": query},
            headers={"accept": "application/dns-message"},
            timeout=DOH_TIMEOUT,
        )
        response.raise_for_status()

        records = parse_a_records(response.content)
        if not records:
            raise ConnectionError(f"Control D returned no A record for {host}")

        ip, ttl = records[0]
        with self.lock:
            self.cache[host] = (ip, time.monotonic() + min(max(ttl, MIN_TTL), MAX_TTL))
        return ip


def relay(source: socket.socket, sink: socket.socket) -> None:
    """Copy bytes one way until the source closes, then half-close the sink."""
    try:
        while True:
            data = source.recv(CHUNK)
            if not data:
                break
            sink.sendall(data)
    except OSError:
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


HOP_BY_HOP = (b"connection:", b"proxy-connection:", b"keep-alive:")


class Handler(socketserver.StreamRequestHandler):
    server: LocalProxy
    rbufsize = 0  # unbuffered, so no body bytes are held back from the relay
    timeout = HEAD_TIMEOUT

    def reply(self, status: bytes) -> None:
        try:
            self.connection.sendall(b"HTTP/1.1 " + status + b"\r\n\r\n")
        except OSError:
            pass

    def forward_response_head(self, upstream: socket.socket) -> None:
        """Send the response head on with `Connection: close`, so the client never reuses this connection."""
        data = b""
        try:
            while b"\r\n\r\n" not in data and len(data) < CHUNK:
                chunk = upstream.recv(CHUNK)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        head, found, body = data.partition(b"\r\n\r\n")
        if found:
            lines = [x for x in head.split(b"\r\n") if not x.lower().startswith(HOP_BY_HOP)]
            data = b"\r\n".join(lines) + b"\r\nConnection: close\r\n\r\n" + body
        try:
            self.connection.sendall(data)
        except OSError:
            pass

    def handle(self) -> None:
        try:
            head = self.rfile.readline(CHUNK)
            headers = []
            while True:
                line = self.rfile.readline(CHUNK)
                if not line or line in (b"\r\n", b"\n"):
                    break
                headers.append(line)
        except OSError:
            return
        parts = head.split()
        if len(parts) < 3:
            return

        method = parts[0].decode("latin-1")
        target = parts[1].decode("latin-1")
        try:
            if method == "CONNECT":
                host, _, port_text = target.rpartition(":") if ":" in target else (target, "", "")
                host, port = host.strip("[]"), int(port_text or 443)
            else:
                parsed = urlsplit(target)
                host, port = parsed.hostname or "", parsed.port or 80
        except ValueError:
            return self.reply(b"400 Bad Request")
        if not host:
            return self.reply(b"400 Bad Request")

        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            # an IP literal bypasses Control D's DNS and would leak this machine's own IP
            log.debug(f"Refused {host}:{port}, an address that Control D cannot redirect")
            return self.reply(b"502 Bad Gateway")

        try:
            ip = self.server.resolver.resolve(host)
            upstream = socket.create_connection((ip, port), timeout=HEAD_TIMEOUT)
        except Exception as e:
            log.debug(f"Could not reach {host}:{port}: {e}")
            return self.reply(b"502 Bad Gateway")

        self.connection.settimeout(IDLE_TIMEOUT)
        upstream.settimeout(IDLE_TIMEOUT)
        try:
            if method == "CONNECT":
                self.connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                headers = [x for x in headers if not x.lower().startswith(HOP_BY_HOP)]
                upstream.sendall(head + b"".join(headers) + b"Connection: close\r\n\r\n")
            upward = threading.Thread(target=relay, args=(self.connection, upstream), daemon=True)
            upward.start()
            if method != "CONNECT":
                self.forward_response_head(upstream)
            relay(upstream, self.connection)
            # origin done: unblock the upward relay instead of waiting on a client that holds its side open
            try:
                self.connection.shutdown(socket.SHUT_RD)
            except OSError:
                pass
            upward.join()
        finally:
            upstream.close()


class LocalProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, resolver: Resolver):
        """An HTTP forward proxy on loopback that resolves every host through `resolver`."""
        super().__init__(("127.0.0.1", 0), Handler)
        self.resolver = resolver

    @property
    def uri(self) -> str:
        return f"http://127.0.0.1:{self.socket.getsockname()[1]}"


# process-wide across provider instances; separate processes share only the account's unshackle-<region> names
FORWARDERS: dict[str, LocalProxy] = {}
REGIONS: dict[str, str] = {}  # profile -> region, oldest claim first
STATE = threading.Lock()


def forwarder(resolver: str) -> LocalProxy:
    """Get the loopback forwarder for a resolver ID, starting it on first use. Hold STATE."""
    url = doh_url(resolver)
    if url not in FORWARDERS:
        if len(FORWARDERS) >= MAX_FORWARDERS:
            raise ValueError("Too many Control D resolvers in use in this process")
        server = LocalProxy(Resolver(url))
        threading.Thread(target=server.serve_forever, daemon=True, name="controld").start()
        FORWARDERS[url] = server
        log.debug(f"Listening on {server.uri}")
    return FORWARDERS[url]


def local_proxy(uri: str) -> str:
    """
    Get a loopback proxy URI for a `controld://<resolver>@dns.controld.com` proxy.

    That is the form a client sends to a remote server: the resolver ID alone, never the
    API token. The client points the profile at the region; the server only resolves
    through it, from its own address.
    """
    resolver = urlsplit(uri).username or ""
    if not RESOLVER_ID.fullmatch(resolver):
        raise ValueError("A controld:// proxy needs a Control D resolver ID")
    with STATE:
        return forwarder(resolver.lower()).uri


def resolver_id(value: str) -> str:
    """Get the resolver ID from a resolver ID or its DoH URL."""
    value = value.rstrip("/").rsplit("/", 1)[-1]
    if not RESOLVER_ID.fullmatch(value):
        raise ValueError(f"Not a Control D resolver ID: {value}")
    return value.lower()


class ControlD(Proxy):
    def __init__(
        self,
        *,
        token: str,
        profile: Optional[str] = None,
        resolver: Optional[str] = None,
        profiles: Optional[list[dict]] = None,
        max_profiles: int = 4,
    ):
        """
        Proxy provider that uses Control D's transparent proxies.

        Control D is a DNS service rather than a proxy: it answers redirected hostnames
        with the IP of a proxy in the region you chose, and that proxy relays onwards by
        SNI. There is no proxy URI to hand to Python-Requests, so this proxy provider runs a
        forwarder on loopback per resolver, and the rest of unshackle sees an ordinary
        HTTP proxy.

        A query points a profile's default rule at the region asked for, so everything
        that profile resolves leaves through it, and each profile must be one kept for
        unshackle alone. A profile holds one region at a time, so each region in use gets
        a profile of its own: the account's endpoint named `unshackle-<region>`, else a
        configured pair (`profile` and `resolver`, `profiles`, or both), else a new
        `unshackle-<region>` endpoint and profile, up to `max_profiles` in all. The
        locations come from Control D itself, so nothing needs listing here.
        """
        if bool(profile) != bool(resolver):
            raise ValueError("Control D needs both a profile and a resolver")
        if max_profiles < 1:
            raise ValueError("Control D needs max_profiles of 1 or more")
        pairs = ([{"profile": profile, "resolver": resolver}] if profile else []) + (profiles or [])

        self.token = token
        self.configured = {x["profile"]: resolver_id(x["resolver"]) for x in pairs}
        self.max_profiles = max_profiles
        self.resolvers = dict(self.configured)

        self.catalogue = self.get_locations()

    def __repr__(self) -> str:
        countries = len({x["country"] for x in self.catalogue})
        servers = len(self.catalogue)

        return f"{countries} Countr{['ies', 'y'][countries == 1]} ({servers} Server{['s', ''][servers == 1]})"

    def get_locations(self) -> list[dict]:
        """Get Control D's proxy locations. Public data, so no token is needed."""
        try:
            response = requests.get(f"{API}/proxies", timeout=DOH_TIMEOUT)
            response.raise_for_status()
            return response.json()["body"]["proxies"]
        except Exception as e:
            # never fail the command: all providers load up front, even for a run that wants no proxy
            log.warning(f"Could not read Control D's locations: {e}")
            return []

    def location(self, query: str) -> Optional[str]:
        """Get the identifier of a Control D proxy location for a country or location code."""
        if not self.catalogue:
            self.catalogue = self.get_locations()

        # a hidden (residential) location is reachable by exact code only, never by country
        codes = [x["PK"] for x in self.catalogue if x["PK"].lower() == query]
        if not codes:
            codes = [x["PK"] for x in self.catalogue if x["country"].lower() == query and not x.get("hidden")]

        return random.choice(codes) if codes else None

    def api(self, method: str, path: str, **data: str) -> dict:
        """Call the Control D API with the account token and return the response body."""
        response = requests.request(
            method,
            f"{API}{path}",
            data=data or None,
            headers={"authorization": f"Bearer {self.token}"},
            timeout=DOH_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()["body"]

    def owned(self) -> dict[str, dict]:
        """
        Get the account's `unshackle-<region>` endpoints, by profile.

        An endpoint counts only when no endpoint without that name shares its profile, so a
        profile that also serves the user's own devices is never redirected.
        """
        devices = self.api("GET", "/devices")["devices"]
        shared = {x["profile"]["PK"] for x in devices if x.get("profile") and not x["name"].startswith(PREFIX)}
        return {
            x["profile"]["PK"]: {"device": x["PK"], "name": x["name"], "resolver": resolver_id(x["resolvers"]["uid"])}
            for x in devices
            if x.get("profile") and x["name"].startswith(PREFIX) and x["profile"]["PK"] not in shared
        }

    def pick(self, query: str) -> str:
        """
        Get the profile for a region: one this process already has on it, else the account's
        `unshackle-<region>`, else a configured pair this process has not claimed, else a new
        `unshackle-<region>`, else one to move, which is renamed if unshackle made it.
        """
        for profile, region in REGIONS.items():
            if region == query and profile in self.resolvers:
                return profile

        owned = self.owned()
        for profile, x in owned.items():
            self.resolvers[profile] = x["resolver"]
            if x["name"] == PREFIX + query:
                return profile

        for profile in self.configured:
            if profile not in REGIONS:
                return profile

        if len(self.configured) + len(owned) < self.max_profiles:
            return self.create(query)

        # out of profiles: move one this process has not claimed (maybe idle) first, then the oldest claim
        candidates = [x for x in [*owned, *self.configured] if x not in REGIONS]
        candidates += [x for x in REGIONS if x in self.resolvers]
        profile = candidates[0]
        log.warning(f"Every Control D profile is in use; moving profile {profile} to {query}")
        if profile in owned:
            self.api("PUT", f"/profiles/{profile}", name=PREFIX + query)
            self.api("PUT", f"/devices/{owned[profile]['device']}", name=PREFIX + query)
        return profile

    def create(self, query: str) -> str:
        """Create a profile and an endpoint that uses it, for unshackle alone."""
        name = PREFIX + query
        profile = self.api("POST", "/profiles", name=name)["profiles"][0]["PK"]
        try:
            device = self.api("POST", "/devices", name=name, profile_id=profile, icon="desktop-linux")
        except Exception:
            self.api("DELETE", f"/profiles/{profile}")  # an orphan profile is never found again
            raise
        self.resolvers[profile] = resolver_id(device["resolvers"]["uid"])
        log.info(f"Created profile {name} ({profile}) on the Control D account")
        return profile

    def claim(self, query: str) -> Optional[str]:
        """Point a profile at this region, unless one already is, and get its resolver ID. Hold STATE."""
        location = self.location(query)
        if not location:
            return None

        profile = self.pick(query)
        resolver = self.resolvers[profile]
        if REGIONS.get(profile) != query:
            self.api("PUT", f"/profiles/{profile}/default", do="3", via=location, status="1")
            log.info(f"Profile {profile} now redirects through {location}")
            server = FORWARDERS.get(doh_url(resolver))
            if server:
                with server.resolver.lock:
                    server.resolver.cache.clear()
            REGIONS.pop(profile, None)
            REGIONS[profile] = query
        return resolver

    def get_proxy(self, query: str) -> Optional[str]:
        """Get the local HTTP proxy URI that resolves through Control D for this region."""
        with STATE:
            resolver = self.claim(query.lower())
            return forwarder(resolver).uri if resolver else None

    def get_remote_proxy(self, query: str) -> Optional[str]:
        """Get the `controld://` proxy URI for this region, for a server on another machine."""
        with STATE:
            resolver = self.claim(query.lower())
            return f"{SCHEME}{resolver}@dns.controld.com" if resolver else None
