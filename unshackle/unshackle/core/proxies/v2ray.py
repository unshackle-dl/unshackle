"""V2Ray/Xray proxy provider.

Spins up a local V2Ray (https://www.v2fly.org/) or Xray (https://xtls.github.io/) instance
with an ephemeral SOCKS5 / HTTP inbound on 127.0.0.1, then routes traffic through a
user-selected outbound (VMess, VLESS, Trojan, or Shadowsocks).

Servers can be supplied in four ways (all can coexist):

1. ``subscription_url`` — base64/plain subscription endpoint(s).
2. ``config_path`` — a pre-built V2Ray/Xray JSON config file.
3. ``servers`` — inline list of ``vmess://`` / ``vless://`` / ``trojan://`` / ``ss://`` URIs.
4. ``countries`` — ``basic``-style per-country URI assignment.

A V2Ray URI can also be passed directly on the CLI (``--proxy v2ray:vmess://...``) for
one-shot use without any YAML configuration.

Query format (after the ``v2ray:`` prefix):
    v2ray:us                -- any server assigned to / detected as US
    v2ray:us:1              -- the first US server (1-indexed, like Basic)
    v2ray:tokyo             -- match a server by remark substring (case-insensitive)
    v2ray:us:tokyo          -- country + remark substring
    v2ray:stream-us         -- an alias from ``server_map`` or ``countries``
    v2ray:vmess://...       -- spawn a one-shot subprocess for that exact URI

The provider follows the same lifecycle pattern as ``Gluetun``: each unique query gets its
own subprocess on a dedicated port, instances are reused for the rest of the process, and
everything is torn down via an ``atexit`` handler.
"""

from __future__ import annotations

import atexit
import base64
import json
import logging
import os
import random
import re
import select
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests

from unshackle.core import binaries
from unshackle.core.proxies.proxy import Proxy
from unshackle.core.utilities import COUNTRY_CODE_ALIASES, get_country_code, get_country_name, log_event
from unshackle.core.utils.ip_info import get_ip_info

log = logging.getLogger("proxies.v2ray")


def _normalise_country_code(code: str) -> Optional[str]:
    """Lowercase ISO 3166-1 alpha-2 code, applying the project's UK -> GB alias."""
    if not code:
        return None
    lowered = code.strip().lower()
    return COUNTRY_CODE_ALIASES.get(lowered, lowered)

# Module-level registry so every V2Ray instance can be cleaned up on interpreter exit,
# mirroring the Gluetun pattern. Multiple Proxy providers may exist concurrently
# (e.g. one in the main process, one in a serve worker) so we keep them all here.
_v2ray_instances: list["V2Ray"] = []
_cleanup_lock = threading.Lock()
_cleanup_registered = False


def _cleanup_all_v2ray_processes() -> None:
    """Stop every spawned V2Ray/Xray subprocess on exit."""
    with _cleanup_lock:
        instances = list(_v2ray_instances)
        _v2ray_instances.clear()
    for instance in instances:
        try:
            instance.cleanup()
        except Exception:
            pass


def _register_cleanup() -> None:
    """Register the atexit handler exactly once."""
    global _cleanup_registered
    with _cleanup_lock:
        if not _cleanup_registered:
            atexit.register(_cleanup_all_v2ray_processes)
            _cleanup_registered = True


# ---------------------------------------------------------------------------
# Server model + URI parsing
# ---------------------------------------------------------------------------

# Regex used to recover a country code from a remark/PS string. Handles flag-emoji
# prefixes (🇺🇸), parenthesised codes, and bare 2-letter prefixes like "US-...".
_FLAG_EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF]{2}"  # regional indicator pair (a flag)
)
_COUNTRY_HINT_RE = re.compile(
    r"(?:^|[\s\-\|_(\[])([A-Z]{2})(?:$|[\s\-\|_)\]])"
)
# Common human-readable country names that show up in subscription remarks.
_COUNTRY_NAME_PATTERNS = (
    "United States", "United Kingdom", "Canada", "Germany", "France",
    "Netherlands", "Japan", "Singapore", "Hong Kong", "South Korea",
    "Australia", "India", "Italy", "Spain", "Switzerland", "Sweden",
    "Norway", "Denmark", "Finland", "Austria", "Belgium", "Ireland",
    "Poland", "Portugal", "Czech Republic", "Romania", "Hungary",
    "Greece", "Turkey", "Russia", "Ukraine", "Brazil", "Mexico",
    "Argentina", "South Africa", "New Zealand", "Thailand", "Philippines",
    "Indonesia", "Malaysia", "Vietnam", "Taiwan", "United Arab Emirates",
    "Israel",
)


@dataclass
class V2RayServer:
    """A single parsed outbound server."""

    protocol: str  # vmess | vless | trojan | shadowsocks
    address: str
    port: int
    remark: str = ""
    # Protocol-specific fields. Kept loose (dict) so we can serialise to V2Ray JSON
    # without having to model every protocol's quirks as a dataclass.
    settings: dict = field(default_factory=dict)
    # Network/stream settings (ws, tcp, grpc, h2, quic, httpupgrade)
    network: str = "tcp"
    stream: dict = field(default_factory=dict)
    # Detected ISO 3166-1 alpha-2 country code (lowercase) if any.
    country: Optional[str] = None

    @property
    def label(self) -> str:
        """Short, human-readable identifier used for logs and __repr__."""
        return (self.remark or f"{self.protocol}-{self.address}:{self.port}").strip()


def _b64_decode_loose(payload: str) -> str:
    """Decode a base64 / base64url string, padding it leniently."""
    payload = payload.strip().replace("\n", "").replace("\r", "")
    # v2ray-style base64url (no padding) is common
    pad = "=" * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(payload + pad).decode("utf-8")
    except Exception:
        try:
            return base64.b64decode(payload + pad).decode("utf-8")
        except Exception as error:
            raise ValueError(f"Could not base64-decode payload ({len(payload)} chars): {error}")


def _detect_country(remark: str, server_name: str = "") -> Optional[str]:
    """Heuristically infer an ISO 3166-1 alpha-2 (lowercase) country code from a remark."""
    if not remark:
        remark = ""
    text = remark

    # Strip flag emojis first so the surrounding text is easier to parse.
    text = _FLAG_EMOJI_RE.sub(" ", text)

    # Explicit 2-letter hints like "(US)", "- US -", "US | Server 1"
    for match in _COUNTRY_HINT_RE.finditer(text):
        candidate = match.group(1).lower()
        # Filter out obvious false positives like "VR", "4K", "HD" by checking
        # that the alias-or-ISO map actually recognises the candidate.
        if get_country_name(candidate):
            return _normalise_country_code(candidate)

    # Common full country names
    lowered = text.lower()
    for name in _COUNTRY_NAME_PATTERNS:
        if name.lower() in lowered:
            code = get_country_code(name)
            if code:
                return code.lower()

    # UK is the most common non-ISO alias; normalise here so the rest of the
    # pipeline only has to reason about ISO codes.
    if re.search(r"\buk\b|united kingdom|\bgreat britain\b", lowered):
        return "gb"

    # Last resort: TLD of the SNI / server hostname.
    if server_name:
        host = server_name.split(":")[0]
        tld = host.rsplit(".", 1)[-1].lower() if "." in host else ""
        if len(tld) == 2 and get_country_name(tld):
            return _normalise_country_code(tld)

    return None


def _split_url_fragment(uri: str) -> tuple[str, str]:
    """Split a URI into (body, fragment) where fragment is URL-decoded."""
    if "#" in uri:
        body, frag = uri.split("#", 1)
        return body, unquote(frag)
    return uri, ""


def _parse_vmess(uri: str) -> Optional[V2RayServer]:
    """Parse a ``vmess://`` (V2Ray VMess) URI into a server record."""
    if not uri.startswith("vmess://"):
        return None
    payload = uri[len("vmess://"):]
    try:
        data = json.loads(_b64_decode_loose(payload))
    except (ValueError, json.JSONDecodeError) as error:
        log.debug("skipping unparseable vmess URI: %s", error)
        return None

    address = str(data.get("add") or "").strip()
    port_raw = data.get("port")
    if not address or not port_raw:
        return None
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        return None

    network = str(data.get("net") or "tcp").lower()
    tls_setting = str(data.get("tls") or "").lower()
    security = "tls" if tls_setting in ("tls", "1", "true") else ("reality" if tls_setting == "reality" else "none")

    stream: dict = {
        "security": security,
        "network": network,
    }
    if security == "tls":
        sni = str(data.get("sni") or data.get("host") or "").strip()
        if sni:
            stream["tlsSettings"] = {"serverName": sni, "allowInsecure": bool(data.get("verify_cert", False) is False)}
    if network in ("ws", "httpupgrade"):
        stream["wsSettings" if network == "ws" else "httpupgradeSettings"] = {
            "path": str(data.get("path") or "/"),
            "host": str(data.get("host") or ""),
        }
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": str(data.get("path") or "")}
    elif network == "h2":
        stream["httpSettings"] = {
            "path": str(data.get("path") or "/"),
            "host": [str(data.get("host") or "")] if data.get("host") else [],
        }

    remark = str(data.get("ps") or "")
    server_name = stream.get("tlsSettings", {}).get("serverName", "") if security == "tls" else ""
    return V2RayServer(
        protocol="vmess",
        address=address,
        port=port,
        remark=remark,
        settings={
            "id": str(data.get("id") or ""),
            "alterId": int(data.get("aid") or 0),
            "security": str(data.get("scy") or "auto"),
        },
        network=network,
        stream=stream,
        country=_detect_country(remark, server_name or address),
    )


def _parse_vless(uri: str) -> Optional[V2RayServer]:
    """Parse a ``vless://`` URI (VLESS, used heavily by Xray + XTLS)."""
    if not uri.startswith("vless://"):
        return None
    body, fragment = _split_url_fragment(uri[len("vless://"):])
    if "@" not in body:
        return None
    user_part, host_part = body.split("@", 1)
    uuid = unquote(user_part)
    if not uuid or not host_part:
        return None

    parsed = urlparse(f"//{host_part}")
    address = parsed.hostname or ""
    if not address:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    query = parse_qs(parsed.query)

    network = (query.get("type", ["tcp"])[0]).lower()
    security = (query.get("security", ["none"])[0]).lower()

    stream: dict = {"security": security, "network": network}
    if security == "tls":
        sni = query.get("sni", [query.get("peer", [""])[0]])[0]
        alpn = query.get("alpn", [""])[0]
        fp = query.get("fp", [""])[0]
        tls_settings: dict = {"serverName": sni}
        if alpn:
            tls_settings["alpn"] = alpn.split(",")
        if fp:
            tls_settings["fingerprint"] = fp
        if query.get("allowInsecure", ["0"])[0] in ("1", "true"):
            tls_settings["allowInsecure"] = True
        stream["tlsSettings"] = tls_settings
    elif security == "reality":
        reality_settings: dict = {
            "serverName": query.get("sni", [""])[0],
            "publicKey": query.get("pbk", [""])[0],
            "shortId": query.get("sid", [""])[0],
            "fingerprint": query.get("fp", ["chrome"])[0],
        }
        if query.get("spiderX", [""])[0]:
            reality_settings["spiderX"] = query["spiderX"][0]
        stream["realitySettings"] = reality_settings

    if network == "ws":
        stream["wsSettings"] = {
            "path": query.get("path", ["/"])[0],
            "host": query.get("host", [""])[0],
        }
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": query.get("serviceName", [""])[0]}
    elif network == "tcp" and security == "tls":
        # Xray's "tcp+tls" with XTLS Vision / Flow
        flow = query.get("flow", [""])[0]
        if flow:
            stream["tcpSettings"] = {"header": {"type": "none"}}
    elif network == "httpupgrade":
        stream["httpupgradeSettings"] = {
            "path": query.get("path", ["/"])[0],
            "host": query.get("host", [""])[0],
        }

    return V2RayServer(
        protocol="vless",
        address=address,
        port=port,
        remark=fragment,
        settings={
            "id": uuid,
            "encryption": query.get("encryption", ["none"])[0],
            "flow": query.get("flow", [""])[0] or None,
        },
        network=network,
        stream=stream,
        country=_detect_country(fragment, query.get("sni", [""])[0] or address),
    )


def _parse_trojan(uri: str) -> Optional[V2RayServer]:
    """Parse a ``trojan://`` URI."""
    if not uri.startswith("trojan://"):
        return None
    body, fragment = _split_url_fragment(uri[len("trojan://"):])
    if "@" not in body:
        return None
    password, host_part = body.split("@", 1)
    password = unquote(password)
    if not password or not host_part:
        return None
    parsed = urlparse(f"//{host_part}")
    address = parsed.hostname or ""
    if not address:
        return None
    port = parsed.port or 443
    query = parse_qs(parsed.query)

    network = (query.get("type", ["tcp"])[0]).lower()
    security = (query.get("security", ["tls"])[0]).lower() or "tls"

    stream: dict = {"security": security, "network": network}
    sni = query.get("sni", [query.get("peer", [""])[0]])[0]
    if security == "tls":
        tls_settings: dict = {"serverName": sni or address}
        if query.get("alpn", [""])[0]:
            tls_settings["alpn"] = query["alpn"][0].split(",")
        if query.get("allowInsecure", ["0"])[0] in ("1", "true"):
            tls_settings["allowInsecure"] = True
        stream["tlsSettings"] = tls_settings
    if network == "ws":
        stream["wsSettings"] = {
            "path": query.get("path", ["/"])[0],
            "host": query.get("host", [""])[0],
        }
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": query.get("serviceName", [""])[0]}

    return V2RayServer(
        protocol="trojan",
        address=address,
        port=port,
        remark=fragment,
        settings={"password": password},
        network=network,
        stream=stream,
        country=_detect_country(fragment, sni or address),
    )


def _parse_shadowsocks(uri: str) -> Optional[V2RayServer]:
    """Parse a ``ss://`` (Shadowsocks) URI in either the legacy or SIP002 form."""
    if not uri.startswith("ss://"):
        return None
    body, fragment = _split_url_fragment(uri[len("ss://"):])

    method = password = ""
    address = port = None

    if "@" in body:
        # SIP002 form: base64(method:password)@host:port  OR  method:password@host:port
        creds_part, host_part = body.split("@", 1)
        if ":" in creds_part and not creds_part.startswith("%2F"):
            # Plain-text creds (some servers ship this)
            method, password = creds_part.split(":", 1)
            method = unquote(method)
            password = unquote(password)
        else:
            try:
                decoded = _b64_decode_loose(creds_part)
                if ":" in decoded:
                    method, password = decoded.split(":", 1)
            except ValueError:
                return None
        parsed = urlparse(f"//{host_part}")
        address = parsed.hostname or ""
        port = parsed.port
    else:
        # Legacy form: ss://base64(method:password@host:port)
        try:
            decoded = _b64_decode_loose(body)
        except ValueError:
            return None
        if "@" not in decoded or ":" not in decoded:
            return None
        creds, host_port = decoded.rsplit("@", 1)
        if ":" not in creds or ":" not in host_port:
            return None
        method, password = creds.split(":", 1)
        host, port_str = host_port.rsplit(":", 1)
        address = host
        try:
            port = int(port_str)
        except ValueError:
            return None

    if not address or not port or not method:
        return None

    return V2RayServer(
        protocol="shadowsocks",
        address=address,
        port=port,
        remark=fragment,
        settings={"method": method, "password": password},
        network="tcp",
        stream={"security": "none", "network": "tcp"},
        country=_detect_country(fragment, address),
    )


_PROTOCOL_PARSERS = (
    _parse_vmess,
    _parse_vless,
    _parse_trojan,
    _parse_shadowsocks,
)

# Schemes recognised as V2Ray-family URIs. Used by _is_v2ray_uri() to detect direct-URI
# queries on the CLI (e.g. ``--proxy v2ray:vmess://...``).
_V2RAY_URI_SCHEMES = ("vmess://", "vless://", "trojan://", "ss://")


def _is_v2ray_uri(text: str) -> bool:
    """Return True if ``text`` looks like a V2Ray-family URI (vmess/vless/trojan/ss).

    Used by :meth:`V2Ray.get_proxy` to detect direct-URI queries — e.g.
    ``--proxy v2ray:vmess://...`` — and spawn a one-shot subprocess for that exact
    server, bypassing the country/remark selection logic.
    """
    if not text:
        return False
    text = text.strip()
    return any(text.startswith(scheme) for scheme in _V2RAY_URI_SCHEMES)


def parse_server_uri(uri: str) -> Optional[V2RayServer]:
    """Parse a single ``vmess://``, ``vless://``, ``trojan://``, or ``ss://`` URI.

    Returns ``None`` (and logs a debug message) if the URI is not recognised or malformed.
    Exposed publicly so the test-suite can exercise each parser without going through the
    subscription/network layer.
    """
    uri = (uri or "").strip()
    if not uri:
        return None
    for parser in _PROTOCOL_PARSERS:
        try:
            server = parser(uri)
        except Exception as error:
            log.debug("parser %s raised on %r: %s", parser.__name__, uri[:80], error)
            server = None
        if server is not None:
            return server
    log.debug("no parser matched URI %r", uri[:80])
    return None


# ---------------------------------------------------------------------------
# Subscription + config-file loaders
# ---------------------------------------------------------------------------

_SUBSCRIPTION_TIMEOUT = 20.0
_SUBSCRIPTION_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "*/*",
}


def fetch_subscription(url: str, *, timeout: float = _SUBSCRIPTION_TIMEOUT) -> list[V2RayServer]:
    """Fetch a base64/plain subscription URL and return the parsed server list.

    Subscription endpoints customarily return either a base64 blob of newline-separated
    URIs or the URIs themselves (one per line). Both shapes are handled here, and any
    line that fails to parse is skipped with a debug log so a single bad entry never
    aborts the whole subscription.
    """
    response = requests.get(url, headers=_SUBSCRIPTION_HEADERS, timeout=timeout)
    response.raise_for_status()
    body = response.text.strip()
    if not body:
        return []

    # If the body doesn't contain a recognised scheme, assume it's base64-encoded.
    if not re.search(r"(vmess|vless|trojan|ss)://", body, re.IGNORECASE):
        try:
            body = _b64_decode_loose(body)
        except ValueError as error:
            log.warning("subscription %s looked base64 but could not be decoded: %s", url, error)
            return []

    servers: list[V2RayServer] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        server = parse_server_uri(line)
        if server is not None:
            servers.append(server)
    log.info("subscription %s yielded %d server(s)", url, len(servers))
    return servers


def load_config_file(path: Path) -> list[V2RayServer]:
    """Extract outbound servers from a pre-built V2Ray/Xray JSON config file.

    The file is parsed for ``outbounds`` whose protocol is one of vmess/vless/trojan/
    shadowsocks. Each one is rebuilt as a :class:`V2RayServer` so it can take part in the
    same query/selection logic as subscription- or inline-defined servers.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read config file {path}: {error}")

    if "outbounds" not in data or not isinstance(data.get("outbounds"), list):
        raise ValueError(f"config file {path} has no 'outbounds' list")
    outbounds = data["outbounds"]

    servers: list[V2RayServer] = []
    for entry in outbounds:
        if not isinstance(entry, dict):
            continue
        protocol = str(entry.get("protocol") or "").lower()
        if protocol not in ("vmess", "vless", "trojan", "shadowsocks"):
            continue
        settings = entry.get("settings") or {}
        stream = entry.get("streamSettings") or {}
        servers_v2 = settings.get("vnext") or []
        servers_ss = settings.get("servers") or []
        if protocol in ("vmess", "vless") and servers_v2:
            target = servers_v2[0]
            address = str(target.get("address") or "")
            port = int(target.get("port") or 0)
            users = target.get("users") or [{}]
            user = users[0] if users else {}
            settings_flat: dict[str, Any] = {"id": str(user.get("id") or "")}
            if protocol == "vmess":
                settings_flat["alterId"] = int(user.get("alterId") or 0)
                settings_flat["security"] = str(user.get("security") or "auto")
            else:
                settings_flat["encryption"] = str(user.get("encryption") or "none")
                if user.get("flow"):
                    settings_flat["flow"] = str(user.get("flow"))
        elif protocol == "trojan" and servers_ss:
            target = servers_ss[0]
            address = str(target.get("address") or "")
            port = int(target.get("port") or 0)
            settings_flat = {"password": str(target.get("password") or "")}
        elif protocol == "shadowsocks" and servers_ss:
            target = servers_ss[0]
            address = str(target.get("address") or "")
            port = int(target.get("port") or 0)
            settings_flat = {
                "method": str(target.get("method") or ""),
                "password": str(target.get("password") or ""),
            }
        else:
            continue

        if not address or not port:
            continue
        remark = str(entry.get("tag") or entry.get("remark") or "")
        sni = ""
        if isinstance(stream.get("tlsSettings"), dict):
            sni = str(stream["tlsSettings"].get("serverName") or "")
        elif isinstance(stream.get("realitySettings"), dict):
            sni = str(stream["realitySettings"].get("serverName") or "")
        servers.append(
            V2RayServer(
                protocol=protocol,
                address=address,
                port=port,
                remark=remark,
                settings=settings_flat,
                network=str(stream.get("network") or "tcp"),
                stream=stream or {"network": "tcp", "security": "none"},
                country=_detect_country(remark, sni or address),
            )
        )

    log.info("config file %s yielded %d server(s)", path, len(servers))
    return servers


# ---------------------------------------------------------------------------
# V2Ray config builder
# ---------------------------------------------------------------------------

def build_outbound(server: V2RayServer, tag: str = "proxy") -> dict:
    """Build a single V2Ray/Xray outbound dict for the given server."""
    if server.protocol == "vmess":
        outbound = {
            "tag": tag,
            "protocol": "vmess",
            "settings": {
                "vnext": [
                    {
                        "address": server.address,
                        "port": server.port,
                        "users": [
                            {
                                "id": server.settings.get("id", ""),
                                "alterId": int(server.settings.get("alterId") or 0),
                                "security": server.settings.get("security", "auto"),
                            }
                        ],
                    }
                ]
            },
            "streamSettings": _normalise_stream(server),
        }
    elif server.protocol == "vless":
        user: dict = {"id": server.settings.get("id", ""), "encryption": server.settings.get("encryption", "none")}
        if server.settings.get("flow"):
            user["flow"] = server.settings["flow"]
        outbound = {
            "tag": tag,
            "protocol": "vless",
            "settings": {
                "vnext": [{"address": server.address, "port": server.port, "users": [user]}]
            },
            "streamSettings": _normalise_stream(server),
        }
    elif server.protocol == "trojan":
        outbound = {
            "tag": tag,
            "protocol": "trojan",
            "settings": {
                "servers": [
                    {
                        "address": server.address,
                        "port": server.port,
                        "password": server.settings.get("password", ""),
                    }
                ]
            },
            "streamSettings": _normalise_stream(server),
        }
    elif server.protocol == "shadowsocks":
        outbound = {
            "tag": tag,
            "protocol": "shadowsocks",
            "settings": {
                "servers": [
                    {
                        "address": server.address,
                        "port": server.port,
                        "method": server.settings.get("method", ""),
                        "password": server.settings.get("password", ""),
                    }
                ]
            },
            "streamSettings": _normalise_stream(server),
        }
    else:
        raise ValueError(f"Unsupported V2Ray protocol: {server.protocol!r}")

    return outbound


def _normalise_stream(server: V2RayServer) -> dict:
    """Return a streamSettings dict suitable for the V2Ray/Xray JSON config."""
    stream = dict(server.stream or {})
    stream.setdefault("network", server.network or "tcp")
    stream.setdefault("security", "none")
    # V2Ray expects "tcpSettings" etc. for per-transport settings; we already store
    # them under those keys in the parser, so just copy through.
    return stream


def build_config(server: V2RayServer, *, socks_port: int, http_port: Optional[int] = None) -> dict:
    """Build the full V2Ray/Xray JSON config for a single outbound + local inbounds."""
    inbounds: list[dict] = [
        {
            "tag": "socks-in",
            "listen": "127.0.0.1",
            "port": socks_port,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": True},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        }
    ]
    if http_port:
        inbounds.append(
            {
                "tag": "http-in",
                "listen": "127.0.0.1",
                "port": http_port,
                "protocol": "http",
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
            }
        )

    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": [
            build_outbound(server, tag="proxy"),
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
        "routing": {
            "rules": [
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
            ]
        },
    }


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class V2Ray(Proxy):
    """V2Ray / Xray local proxy provider.

    Servers may be provided in four ways (all can coexist — see the module docstring for
    details): ``subscription_url``, ``config_path``, ``servers``, and ``countries``.

    Optional ``server_map`` (dict[str, str]) lets the user override the country/alias
    used for selection: keys are matched (case-insensitive) against either an explicit
    alias or the server's remark substring, and the value replaces the query used to
    reach that server. This is handy when a subscription's remarks don't include a
    country code or use non-standard naming.

    Query format (after the ``v2ray:`` prefix):
      - ``us``         any server assigned to / detected as US
      - ``us:1``       first US server (1-indexed)
      - ``tokyo``      server whose remark contains "tokyo" (case-insensitive)
      - ``us:tokyo``   US server whose remark contains "tokyo"
      - ``stream-us``  an alias from ``server_map`` or ``countries``
      - ``vmess://...`` spawn a one-shot subprocess for that exact URI

    The provider spawns the ``xray`` binary (preferred) or falls back to ``v2ray``. Each
    unique query gets its own subprocess on a dedicated local port; the SOCKS5 proxy URI
    is returned. Processes are reused for the rest of the process and cleaned up on exit.
    """

    DEFAULT_VERIFY_IP = True
    DEFAULT_STARTUP_TIMEOUT = 30.0  # seconds

    def __init__(
        self,
        subscription_url: Optional[Any] = None,
        config_path: Optional[Any] = None,
        servers: Optional[list[Any]] = None,
        countries: Optional[dict[str, Any]] = None,
        server_map: Optional[dict[str, str]] = None,
        binary: Optional[Any] = None,
        bind_host: str = "127.0.0.1",
        base_port: int = 11080,
        proxy_scheme: str = "socks5",
        verify_ip: bool = DEFAULT_VERIFY_IP,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        auto_cleanup: bool = True,
        cache_path: Optional[Any] = None,
        **kwargs: Any,
    ):
        """Initialise the V2Ray provider.

        Args:
            subscription_url: A single subscription URL or a list of URLs. Each is
                fetched once at init and the parsed servers are merged.
            config_path: Path to a V2Ray/Xray JSON config file. Outbounds with known
                protocols are extracted and added to the selectable server pool.
            servers: Inline list of ``vmess://``/``vless://``/``trojan://``/``ss://``
                URIs or pre-parsed server dicts (useful for tests / programmatic use).
            countries: ``basic``-style per-country assignment — a dict mapping a country
                code (or arbitrary alias) to a single URI string or a list of URI strings.
                Queries like ``v2ray:us`` then pick from the URIs assigned to ``us``
                (random pick by default; ``v2ray:us:2`` selects the second one). This is
                the simplest way to pin specific servers to specific regions without
                dealing with subscription URLs or remark substrings. URIs that fail to
                parse are skipped with a warning, so a single bad entry never aborts the
                whole map.
            server_map: Optional dict mapping a query alias (e.g. ``"stream-us"``) to
                either a country code, a remark substring, or a ``country:remark`` pair.
                Lets users give friendly names to specific servers.
            binary: Optional path to the V2Ray/Xray binary. Defaults to auto-discovery
                via :mod:`unshackle.core.binaries` (Xray preferred, v2ray fallback).
            bind_host: Host the local inbounds listen on (default ``127.0.0.1`` —
                proxies are never exposed publicly by default).
            base_port: First port to try when allocating local inbounds (default 11080).
                Each query gets ``base_port + N`` (SOCKS) and ``base_port + N + 1`` (HTTP)
                for its inbounds.
            proxy_scheme: Scheme used for the returned proxy URI. ``socks5`` (default)
                and ``socks5h`` are supported; ``http`` returns the HTTP inbound URI
                instead (requires the HTTP inbound to be enabled, which it is by default).
            verify_ip: When True (default), verify the exit IP / country of the spawned
                proxy via :func:`unshackle.core.utils.ip_info.get_ip_info` before
                returning it to the caller.
            startup_timeout: Seconds to wait for the V2Ray subprocess to start accepting
                connections (default 30s).
            auto_cleanup: When True (default), the subprocess is killed on object
                destruction / interpreter exit.
            cache_path: Optional path to cache the parsed subscription server list. The
                cache is invalidated whenever any subscription URL is re-fetched and the
                response changes; on a fetch failure the cached list is used as a
                fallback. Disabled by default.
        """
        # Resolve the binary first; the rest of init can proceed even without one (so
        # the constructor doesn't raise during config validation in tests), but
        # ``get_proxy`` will refuse to run without a usable binary.
        self.binary = self._resolve_binary(binary)
        self.bind_host = bind_host
        self.base_port = int(base_port)
        self.proxy_scheme = str(proxy_scheme or "socks5").lower()
        if self.proxy_scheme not in ("socks5", "socks5h", "http", "https"):
            raise ValueError(
                f"proxy_scheme must be one of socks5/socks5h/http/https, got {self.proxy_scheme!r}"
            )
        self.verify_ip = bool(verify_ip)
        self.startup_timeout = float(startup_timeout)
        self.auto_cleanup = bool(auto_cleanup)
        self.cache_path = Path(cache_path).expanduser() if cache_path else None

        self.server_map = self._normalise_server_map(server_map or {})

        # Per-instance state
        self._port_lock = threading.Lock()
        self._servers: list[V2RayServer] = []
        self._country_servers: dict[str, list[V2RayServer]] = {}
        self._active: dict[str, dict] = {}  # query_key -> process info

        # Load servers (priority: subscription > config_path > inline).
        self._servers = self._load_servers(subscription_url, config_path, servers)

        # Load the per-country map (basic-style assignment). Done after the flat list so
        # the two can coexist: ``v2ray:us`` first checks the explicit country map, then
        # falls back to country-detection on the flat server list.
        self._country_servers = self._load_country_map(countries or {})

        # Register for atexit cleanup (always, even if no servers loaded yet —
        # get_proxy may load more later via subscription refresh or direct URI queries).
        _register_cleanup()
        with _cleanup_lock:
            _v2ray_instances.append(self)

        total_servers = len(self._servers) + sum(len(v) for v in self._country_servers.values())
        log_event(
            "v2ray_init",
            level="INFO",
            message=f"V2Ray proxy provider initialized with {total_servers} server(s)",
            context={
                "binary": str(self.binary) if self.binary else None,
                "bind_host": self.bind_host,
                "base_port": self.base_port,
                "proxy_scheme": self.proxy_scheme,
                "verify_ip": self.verify_ip,
                "server_count": len(self._servers),
                "country_server_count": total_servers - len(self._servers),
                "country_keys": list(self._country_servers.keys()),
            },
        )

    # -- Proxy interface ---------------------------------------------------

    def __repr__(self) -> str:
        flat_countries = {s.country for s in self._servers if s.country}
        mapped_countries = set(self._country_servers.keys())
        all_countries = flat_countries | mapped_countries
        total_servers = len(self._servers) + sum(len(v) for v in self._country_servers.values())
        countries = len(all_countries)
        return f"{countries} Countr{['ies', 'y'][countries == 1]} ({total_servers} Server{['s', ''][total_servers == 1]})"

    def get_proxy(self, query: str) -> Optional[str]:
        """Resolve ``query`` to a local proxy URI backed by a V2Ray subprocess.

        ``query`` may be:

        - A direct V2Ray URI (``vmess://...``, ``vless://...``, ``trojan://...``,
          ``ss://...``) — a one-shot subprocess is spawned for that exact server.
          Useful for ad-hoc invocations like ``--proxy v2ray:vmess://...`` without
          any YAML configuration at all.
        - A country code, ``country:index``, ``country:remark``, remark substring, or
          ``server_map`` alias — resolved against the configured server pool
          (``countries`` map first, then the flat ``servers`` / ``subscription_url`` /
          ``config_path`` list).
        """
        if not self.binary:
            raise EnvironmentError(
                "V2Ray/Xray binary not found. Install xray (https://github.com/XTLS/Xray-core) "
                "or v2ray (https://github.com/v2fly/v2ray-core) and ensure it is on your PATH, "
                "or set proxy_providers.v2ray.binary in your config."
            )

        raw_query = query or ""
        query_key = raw_query.strip().lower()
        if not query_key:
            raise ValueError("empty query — supply a country code, alias, remark substring, or V2Ray URI")

        # Direct-URI mode: the query is itself a vmess:// / vless:// / trojan:// / ss:// URI.
        # Parse it on the fly and spawn a one-shot subprocess. This works even when no
        # servers are configured in YAML — handy for ``--proxy v2ray:vmess://...``.
        if _is_v2ray_uri(raw_query):
            server = parse_server_uri(raw_query)
            if server is None:
                raise ValueError(
                    f"query looked like a V2Ray URI but could not be parsed: {raw_query[:80]!r}"
                )
            return self._spawn_for_server(server, query_key)

        # Country / remark / alias mode requires at least one preloaded server source.
        if not self._servers and not self._country_servers:
            raise ValueError(
                "V2Ray proxy provider has no servers configured. Provide subscription_url, "
                "config_path, servers, or countries under proxy_providers.v2ray — "
                "or pass a vmess:// / vless:// / trojan:// / ss:// URI directly on the CLI."
            )

        # Reuse a running subprocess for the same query.
        if query_key in self._active and self._is_process_alive(self._active[query_key]):
            entry = self._active[query_key]
            if self.verify_ip and not entry.get("verified"):
                self._verify_proxy(query_key)
            return self._build_proxy_uri(entry["socks_port"], entry["http_port"])

        server = self._select_server(query_key)
        if server is None:
            # No server matched — return None to indicate "accepted but unavailable" per Proxy contract.
            return None

        return self._spawn_for_server(server, query_key)

    def _spawn_for_server(self, server: V2RayServer, query_key: str) -> str:
        """Allocate ports, spawn the subprocess for ``server``, and return the proxy URI.

        Used by both the country/remark selection path and the direct-URI path so the
        readiness / verify / cleanup behaviour is identical.
        """
        # Reuse an already-running subprocess for the same key (e.g. the same URI passed twice).
        if query_key in self._active and self._is_process_alive(self._active[query_key]):
            entry = self._active[query_key]
            if self.verify_ip and not entry.get("verified"):
                self._verify_proxy(query_key)
            return self._build_proxy_uri(entry["socks_port"], entry["http_port"])

        socks_port, http_port = self._allocate_ports()
        config = build_config(server, socks_port=socks_port, http_port=http_port)
        process_info = self._spawn_process(config, socks_port, http_port, server)
        process_info["server"] = server
        process_info["query"] = query_key
        self._active[query_key] = process_info

        # Wait for the SOCKS5 inbound to accept connections.
        if not self._wait_for_ready(socks_port, timeout=self.startup_timeout):
            self._kill_process(process_info)
            self._active.pop(query_key, None)
            raise RuntimeError(
                f"subprocess did not become ready within {self.startup_timeout:.0f}s "
                f"(server={server.label}, port={socks_port})"
            )

        if self.verify_ip:
            self._verify_proxy(query_key)

        return self._build_proxy_uri(socks_port, http_port)

    # -- Server selection --------------------------------------------------

    def _select_server(self, query: str) -> Optional[V2RayServer]:
        """Pick a server matching ``query`` (already lowercased).

        Selection order:

        1. ``server_map`` alias (if the query matches a key).
        2. The ``countries`` map — by ISO country code (normalised), then by the raw
           head token (so arbitrary aliases like ``"stream-us"`` work).
        3. The flat server list (``subscription_url`` / ``config_path`` / ``servers``),
           filtered by country and/or remark substring.

        Query grammar::

            <country>              any server in that country
            <country>:<index>      the Nth server in the country pool (1-indexed)
            <country>:<remark>     country-filtered remark substring match
            <remark>               remark substring match across all servers
            <index>                the Nth server overall (1-indexed)
            <alias>                a key from ``countries`` or ``server_map``
        """
        # Apply server_map aliases first.
        mapped = self.server_map.get(query)
        if mapped:
            query = mapped.lower()

        # Split "head:tail" — head is a country code / alias, tail is an index or remark.
        head = query
        tail = ""
        if ":" in query:
            head, tail = query.split(":", 1)
            head = head.strip()
            tail = tail.strip()

        index: Optional[int] = None
        remark_query: Optional[str] = None
        if tail:
            if tail.isdigit():
                index = int(tail)
            else:
                remark_query = tail
        elif query.isdigit():
            # No colon but all digits — treat as a global index into the flat list.
            index = int(query)

        # Normalise head to a country code if it parses as one.
        country: Optional[str] = None
        if head and not head.isdigit():
            country = self._normalise_country(head)

        # If head didn't parse as a country AND isn't a known countries-map alias, treat
        # the whole query as a remark substring. This makes "tokyo" match a server whose
        # remark contains "tokyo", while still returning None for unknown 2-letter codes
        # like "zz" (instead of falling through to "return the first server in the list").
        if head and country is None and remark_query is None and index is None:
            if head not in self._country_servers:
                remark_query = query

        # 1. Check the explicit ``countries`` map first (basic-style assignment). The map
        # key may be either an ISO country code (e.g. "us") or an arbitrary alias (e.g.
        # "stream-us"), so we look it up by every plausible form of the head.
        for key in self._country_lookup_keys(country, head, query):
            mapped_pool = self._country_servers.get(key)
            if not mapped_pool:
                continue
            pool = mapped_pool
            if remark_query:
                pool = [s for s in pool if remark_query in (s.remark or "").lower()]
                if not pool:
                    log.warning(
                        "countries[%s] has no server matching remark %r",
                        key, remark_query,
                    )
                    return None
            # The countries map uses random pick to match Basic's behaviour for
            # multi-server lists; the flat-list fallback below uses first-match.
            return self._pick_from_pool(pool, index, random_pick=True)

        # 2. Fall back to the flat server list (subscription / config_path / servers).
        pool = self._servers
        if country:
            pool = [s for s in pool if (s.country or "").lower() == country]
        if remark_query:
            pool = [s for s in pool if remark_query in (s.remark or "").lower()]

        if not pool:
            log.warning(
                "no server matched query %r (country=%s, remark=%s, index=%s)",
                query, country, remark_query, index,
            )
            return None

        return self._pick_from_pool(pool, index, random_pick=False)

    @staticmethod
    def _country_lookup_keys(country: Optional[str], head: str, query: str) -> list[str]:
        """Build the ordered list of keys to try against the ``countries`` map."""
        keys: list[str] = []
        if country:
            keys.append(country)
        if head:
            keys.append(head)
        if not head and query:
            keys.append(query)
        return keys

    @staticmethod
    def _pick_from_pool(
        pool: list[V2RayServer], index: Optional[int], *, random_pick: bool = False
    ) -> Optional[V2RayServer]:
        """Pick a server from ``pool``, honouring the 1-indexed ``index`` if set.

        When ``index`` is None and ``random_pick`` is True, a random server is chosen
        (matching :class:`unshackle.core.proxies.basic.Basic`'s behaviour for multi-server
        lists). Otherwise the first server is returned deterministically (preserving the
        flat-list behaviour for subscription / inline servers).
        """
        if index is not None:
            if index < 1 or index > len(pool):
                log.warning("index %d out of range for pool of %d server(s)", index, len(pool))
                return None
            return pool[index - 1]
        if random_pick:
            return random.choice(pool)
        return pool[0]

    @staticmethod
    def _normalise_country(token: str) -> Optional[str]:
        """Normalise a country code or full name to a lowercase ISO 3166-1 alpha-2 code.

        For 2-letter tokens, an exact alpha-2 lookup is used (with the project's UK -> GB
        alias applied) — pycountry's fuzzy search would happily map ``"zz"`` to ``"Italy"``,
        which is misleading. For longer tokens, fuzzy matching against full country names
        is allowed.
        """
        token = token.strip().lower()
        if not token:
            return None
        if len(token) == 2:
            return _normalise_country_code(token) if get_country_name(token) else None
        code = get_country_code(token)
        return code.lower() if code else None

    @staticmethod
    def _normalise_server_map(server_map: dict) -> dict[str, str]:
        return {str(k).strip().lower(): str(v).strip() for k, v in server_map.items() if k and v}

    # -- Server loading ----------------------------------------------------

    def _load_servers(
        self,
        subscription_url: Optional[Any],
        config_path: Optional[Any],
        servers: Optional[list[Any]],
    ) -> list[V2RayServer]:
        loaded: list[V2RayServer] = []

        # 1. Subscription URL(s)
        urls: list[str] = []
        if subscription_url:
            if isinstance(subscription_url, (list, tuple)):
                urls.extend(str(u) for u in subscription_url if u)
            else:
                urls.append(str(subscription_url))
        for url in urls:
            try:
                loaded.extend(self._fetch_subscription_cached(url))
            except Exception as error:
                log.warning("subscription %s failed: %s", url, error)

        # 2. Config file
        if config_path:
            path = Path(config_path).expanduser()
            if not path.is_file():
                log.warning("config_path %s does not exist", path)
            else:
                try:
                    loaded.extend(load_config_file(path))
                except Exception as error:
                    log.warning("config file %s failed: %s", path, error)

        # 3. Inline servers
        if servers:
            for entry in servers:
                if isinstance(entry, str):
                    parsed = parse_server_uri(entry)
                    if parsed is not None:
                        loaded.append(parsed)
                elif isinstance(entry, dict):
                    # Accept a pre-parsed server dict (useful for tests / programmatic use).
                    try:
                        loaded.append(V2RayServer(**entry))
                    except TypeError as error:
                        log.warning("skipping malformed inline server dict: %s", error)

        # De-duplicate by (protocol, address, port) — many subscriptions list the same
        # server under multiple remarks.
        seen: set[tuple[str, str, int]] = set()
        unique: list[V2RayServer] = []
        for s in loaded:
            key = (s.protocol, s.address.lower(), s.port)
            if key in seen:
                continue
            seen.add(key)
            unique.append(s)

        return unique

    def _fetch_subscription_cached(self, url: str) -> list[V2RayServer]:
        """Fetch a subscription, optionally caching the parsed server list to disk."""
        if not self.cache_path:
            return fetch_subscription(url)

        cache_key = f"sub:{url}"
        cache = self._load_cache()
        cached_entry = cache.get(cache_key)
        try:
            servers = fetch_subscription(url)
        except Exception:
            if cached_entry:
                log.warning("subscription %s failed, using cached copy", url)
                return [V2RayServer(**s) for s in cached_entry.get("servers", [])]
            raise
        if servers:
            cache[cache_key] = {
                "servers": [
                    {
                        "protocol": s.protocol,
                        "address": s.address,
                        "port": s.port,
                        "remark": s.remark,
                        "settings": s.settings,
                        "network": s.network,
                        "stream": s.stream,
                        "country": s.country,
                    }
                    for s in servers
                ],
                "updated_at": time.time(),
            }
            self._save_cache(cache)
        return servers

    def _load_cache(self) -> dict:
        if not self.cache_path or not self.cache_path.is_file():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            log.warning("cache read failed (%s): %s", self.cache_path, error)
            return {}

    def _save_cache(self, cache: dict) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        except OSError as error:
            log.warning("cache write failed (%s): %s", self.cache_path, error)

    def _load_country_map(self, countries: dict) -> dict[str, list[V2RayServer]]:
        """Parse the ``basic``-style ``countries`` config map.

        The input is a dict mapping a country code (or arbitrary alias) to either a
        single URI string or a list of URI strings. Each URI is parsed via
        :func:`parse_server_uri`; entries that fail to parse are skipped with a warning
        so a single bad URI never aborts the whole map. The returned dict maps each
        lowercased key to the list of successfully-parsed servers.

        This mirrors :class:`unshackle.core.proxies.basic.Basic` — the user explicitly
        asked for V2Ray to support the same per-country assignment pattern.
        """
        if not isinstance(countries, dict):
            raise TypeError(
                f"'countries' must be a dict mapping country code -> URI or list of URIs, "
                f"got {type(countries).__name__}"
            )

        result: dict[str, list[V2RayServer]] = {}
        for raw_key, raw_value in countries.items():
            key = str(raw_key).strip().lower()
            if not key:
                continue

            # Normalise the value to a list of URI strings.
            if isinstance(raw_value, str):
                uri_list: list[str] = [raw_value]
            elif isinstance(raw_value, (list, tuple)):
                uri_list = [str(v) for v in raw_value if v]
            else:
                log.warning(
                    "countries[%s] has unsupported value type %s — expected str or list, skipping",
                    key, type(raw_value).__name__,
                )
                continue

            servers: list[V2RayServer] = []
            for uri in uri_list:
                parsed = parse_server_uri(uri)
                if parsed is None:
                    log.warning("countries[%s] could not parse URI %r — skipping", key, uri[:80])
                    continue
                # Force the server's country to match the map key, so the IP-verification
                # step (when enabled) compares against the user's intent rather than the
                # auto-detected country from the remark.
                if key and get_country_name(key):
                    parsed.country = _normalise_country_code(key)
                servers.append(parsed)

            if servers:
                result[key] = servers
            else:
                log.warning("countries[%s] produced zero valid servers", key)

        return result

    # -- Binary discovery --------------------------------------------------

    @staticmethod
    def _resolve_binary(explicit: Any) -> Optional[Path]:
        if explicit:
            path = Path(explicit).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"configured binary not found at {path}")
            return path
        # Xray is the modern, fully-compatible fork; prefer it when both are installed.
        return binaries.Xray or binaries.V2Ray

    # -- Process management ------------------------------------------------

    def _allocate_ports(self) -> tuple[int, int]:
        """Pick the next free (socks_port, http_port) pair, thread-safely.

        SOCKS ports are tried at ``base_port + 2N`` and the HTTP inbound at
        ``base_port + 2N + 1``, so each subprocess gets a dedicated non-overlapping pair.
        """
        with self._port_lock:
            used = {info["socks_port"] for info in self._active.values()}
            used |= {info["http_port"] for info in self._active.values() if info.get("http_port")}
            socks_port = self.base_port
            while True:
                # Find a free SOCKS port on an even stride.
                while socks_port in used or self._is_port_in_use(socks_port):
                    socks_port += 2
                http_port = socks_port + 1
                # If the HTTP port is taken, bump the SOCKS port and retry.
                if http_port in used or self._is_port_in_use(http_port):
                    socks_port += 2
                    continue
                return socks_port, http_port

    @staticmethod
    def _is_port_in_use(port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
        except OSError:
            return True
        return False

    def _spawn_process(
        self,
        config: dict,
        socks_port: int,
        http_port: int,
        server: V2RayServer,
    ) -> dict:
        """Write the config to a temp file and start the V2Ray/Xray subprocess."""
        config_dir = Path(tempfile.gettempdir()) / "unshackle-v2ray"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / f"config-{socks_port}-{http_port}.json"

        try:
            # Best-effort: write with 0600 so other users on the box can't read credentials.
            fd = os.open(str(config_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                os.write(fd, json.dumps(config, indent=2).encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as error:
            raise RuntimeError(f"could not write config file {config_path}: {error}")

        log.debug("wrote config for %s to %s", server.label, config_path)
        log_event(
            "v2ray_spawn_start",
            level="DEBUG",
            message=f"Starting V2Ray subprocess for {server.label}",
            context={
                "binary": str(self.binary),
                "config_path": str(config_path),
                "socks_port": socks_port,
                "http_port": http_port,
                "server": server.label,
                "protocol": server.protocol,
                "address": server.address,
                "port": server.port,
                "country": server.country,
            },
        )

        creationflags = 0
        start_new_session = True
        if os.name == "nt":
            # Windows: CREATE_NO_WINDOW so a console doesn't pop up.
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            start_new_session = False

        process = subprocess.Popen(
            [str(self.binary), "run", "-c", str(config_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(config_path.parent),
            start_new_session=start_new_session,
            creationflags=creationflags,
            close_fds=True,
        )

        return {
            "process": process,
            "config_path": config_path,
            "socks_port": socks_port,
            "http_port": http_port,
            "pid": process.pid,
            "started_at": time.time(),
            "verified": False,
        }

    @staticmethod
    def _is_process_alive(info: dict) -> bool:
        process: Optional[subprocess.Popen] = info.get("process")
        if process is None:
            return False
        if process.poll() is not None:
            return False
        return True

    def _wait_for_ready(self, socks_port: int, *, timeout: float) -> bool:
        """Poll the SOCKS5 inbound until it accepts connections or timeout."""
        deadline = time.monotonic() + timeout
        last_error: Optional[str] = None
        while time.monotonic() < deadline:
            # If the process died, surface its stderr and bail out fast.
            info = next((i for i in self._active.values() if i.get("socks_port") == socks_port), None)
            if info and not self._is_process_alive(info):
                stderr = self._read_stderr_tail(info, max_lines=20)
                last_error = stderr or "process exited unexpectedly"
                break
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    s.connect((self.bind_host, socks_port))
                    return True
            except OSError as error:
                last_error = str(error)
                time.sleep(0.2)
        log.warning("subprocess on port %d not ready after %.1fs: %s", socks_port, timeout, last_error)
        return False

    @staticmethod
    def _read_stderr_tail(info: dict, *, max_lines: int = 30) -> str:
        """Best-effort non-blocking read of the subprocess's buffered stderr.

        Returns the last ``max_lines`` lines (joined with newlines), or an empty string
        if nothing is buffered or the read fails for any reason. Used only to surface a
        hint when the subprocess fails to become ready — never on the hot path.
        """
        process: Optional[subprocess.Popen] = info.get("process")
        if process is None or process.stderr is None:
            return ""
        try:
            readable, _, _ = select.select([process.stderr], [], [], 0)
            if not readable:
                return ""
            data = process.stderr.read1(8192) if hasattr(process.stderr, "read1") else b""
            if not data:
                return ""
            text = data.decode("utf-8", errors="replace")
            lines = text.splitlines()
            return "\n".join(lines[-max_lines:])
        except Exception:
            return ""

    def _verify_proxy(self, query_key: str, *, max_retries: int = 3) -> None:
        """Confirm the spawned proxy exits in the expected country via get_ip_info."""
        info = self._active.get(query_key)
        if not info:
            return
        proxy_uri = self._build_proxy_uri(info["socks_port"], info["http_port"])
        server: V2RayServer = info["server"]
        expected_country = (server.country or "").upper()

        session = requests.Session()
        try:
            session.proxies = {"http": proxy_uri, "https": proxy_uri}
            last_error: Optional[str] = None
            for attempt in range(max_retries):
                try:
                    ip_info = get_ip_info(session)
                except Exception as error:
                    last_error = str(error)
                    ip_info = None

                if ip_info:
                    actual_country = (ip_info.get("country") or "").upper()
                    info["verified"] = True
                    info["public_ip"] = ip_info.get("ip")
                    info["ip_country"] = actual_country
                    info["ip_city"] = ip_info.get("city")
                    info["ip_org"] = ip_info.get("org")

                    log_event(
                        "v2ray_verify_success",
                        level="INFO",
                        message=f"V2Ray proxy verified for {server.label}",
                        context={
                            "query": query_key,
                            "server": server.label,
                            "expected_country": expected_country or None,
                            "actual_country": actual_country,
                            "ip": ip_info.get("ip"),
                            "city": ip_info.get("city"),
                            "org": ip_info.get("org"),
                            "attempts": attempt + 1,
                        },
                    )

                    # If we have an expected country and it doesn't match, log a warning
                    # but don't raise — the proxy may still be functional for the user's
                    # purpose (e.g. CDN-based geofencing that doesn't strictly match the
                    # exit IP's country).
                    if expected_country and actual_country and actual_country != expected_country:
                        log.warning(
                            "country mismatch for %s — expected %s, got %s (IP: %s). "
                            "The proxy is still returned; check your subscription if this is unexpected.",
                            server.label, expected_country, actual_country, ip_info.get("ip"),
                        )
                    return

                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
        finally:
            try:
                session.close()
            except Exception:
                pass

        log_event(
            "v2ray_verify_failed",
            level="WARNING",
            message=f"V2Ray IP verification failed for {server.label} after {max_retries} attempts",
            context={
                "query": query_key,
                "server": server.label,
                "max_retries": max_retries,
                "last_error": last_error,
            },
        )
        # Don't raise — verification is best-effort. The proxy may still work.

    def _build_proxy_uri(self, socks_port: int, http_port: int) -> str:
        """Build the proxy URI returned to the caller, based on ``proxy_scheme``."""
        if self.proxy_scheme in ("http", "https"):
            return f"{self.proxy_scheme}://{self.bind_host}:{http_port}"
        return f"{self.proxy_scheme}://{self.bind_host}:{socks_port}"

    def _kill_process(self, info: dict) -> None:
        """Terminate the subprocess, close its stdout/stderr pipes, and unlink the config file.

        The config file is overwritten with zeros before unlinking (best-effort, like
        Gluetun's env-file handling) because it contains server credentials.
        """
        process: Optional[subprocess.Popen] = info.get("process")
        if process and process.poll() is None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            except Exception as error:
                log.debug("error killing process %s: %s", info.get("pid"), error)

        # Close stdout/stderr pipes so we don't leak FDs. The pipe handles live on the
        # Popen object (set via stdout=PIPE / stderr=PIPE in _spawn_process), not on the
        # info dict.
        for pipe in (process.stdout, process.stderr) if process else (None, None):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    pass

        config_path: Optional[Path] = info.get("config_path")
        if config_path and config_path.is_file():
            try:
                # Best-effort secure delete: overwrite then unlink, like Gluetun does for
                # its env files, since the config contains server credentials.
                try:
                    with open(config_path, "r+b") as f:
                        f.seek(0, os.SEEK_END)
                        length = f.tell()
                        f.seek(0)
                        if length > 0:
                            f.write(b"\x00" * length)
                            f.flush()
                            os.fsync(f.fileno())
                except Exception:
                    pass
                config_path.unlink()
            except Exception:
                pass

    def cleanup(self) -> None:
        """Stop every subprocess spawned by this provider instance."""
        with self._port_lock:
            items = list(self._active.items())
            self._active.clear()
        count = len(items)
        for query_key, info in items:
            self._kill_process(info)
            log_event(
                "v2ray_process_removed",
                level="DEBUG",
                message=f"Removed V2Ray subprocess for {query_key}",
                context={"query": query_key, "pid": info.get("pid")},
            )
        if count:
            log_event(
                "v2ray_cleanup_complete",
                level="INFO",
                message=f"V2Ray cleanup complete: removed {count} subprocess(es)",
                context={"count": count},
            )

    def __del__(self) -> None:
        if getattr(self, "auto_cleanup", True):
            try:
                self.cleanup()
            except Exception:
                pass

    # -- Introspection (used by tests + useful for debugging) --------------

    @property
    def servers(self) -> list[V2RayServer]:
        """Read-only view of the flat server pool (subscription / config_path / servers)."""
        return list(self._servers)

    @property
    def country_servers(self) -> dict[str, list[V2RayServer]]:
        """Read-only view of the per-country server map (the ``countries:`` YAML block)."""
        return {k: list(v) for k, v in self._country_servers.items()}

    def get_connection_info(self, query: str) -> Optional[dict]:
        """Return the connection info dict for a previously-resolved query, if any."""
        return self._active.get((query or "").strip().lower())
