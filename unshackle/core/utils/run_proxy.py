"""Keep service traffic on the proxy of the run, and warn about service requests that do not use it.

Nothing here blocks a request: a request that does not go through the proxy only logs a warning.
A service marks an HTTP session that goes direct on purpose with ``session.proxy_exempt = True``.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import sys
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, Optional
from urllib.parse import urlsplit

import requests

CORE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__))) + os.sep  # the unshackle package
SESSION_FILE = os.path.join(CORE_DIR, "core", "session.py")


@dataclass
class Watch:
    cls: type
    directory: str
    proxies: list[str] = field(default_factory=list)  # every proxy a run of this service used, newest last


watched: dict[str, Watch] = {}  # service directory -> Watch
proxy_hosts: set[str] = set()
warned: set[tuple[str, str, int]] = set()
hook_installed = False


def pin(session: requests.Session) -> None:
    """Stop HTTP(S)_PROXY and the system proxy from overriding ``session.proxies``.

    requests merges the environment proxies over the proxies of the HTTP session. Turning ``trust_env`` off also drops
    the environment CA bundle, so this copies ``REQUESTS_CA_BUNDLE`` or ``CURL_CA_BUNDLE`` into ``verify``.
    """
    if session.verify is True:
        session.verify = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE") or True
    session.trust_env = False


def proxy_of(session: Any) -> Optional[str]:
    proxies = getattr(session, "proxies", None) or {}
    return proxies.get("all") or proxies.get("https") or proxies.get("http")


def hostname(uri: str) -> str:
    return (urlsplit(uri).hostname or "").lower()


def service_dir(cls: type) -> Optional[str]:
    file = getattr(sys.modules.get(cls.__module__), "__file__", None)
    return os.path.dirname(file) + os.sep if file else None


def watch(cls: type, proxy: Optional[str]) -> None:
    """Watch the requests of the service class ``cls`` in a run with the proxy ``proxy``."""
    global hook_installed
    directory = service_dir(cls)
    if not proxy or not directory:
        return
    entry = watched.get(directory)
    if entry is None:
        entry = watched[directory] = Watch(cls, directory)
    if proxy not in entry.proxies:
        entry.proxies.append(proxy)
    proxy_hosts.add(hostname(proxy))
    if not hook_installed:
        sys.addaudithook(audit)
        hook_installed = True


def find_service(frame: Optional[FrameType], through_core: bool) -> Optional[tuple[FrameType, Watch]]:
    """Return the innermost frame of watched service code and its watch.

    Without ``through_core`` the walk ends at the first unshackle core frame: core sends some requests
    direct on purpose (key vaults, metadata, CDM APIs). A frame of a SOCKS module also ends it, because
    a ``socks5://`` proxy looks up the target host locally and still sends the request through the proxy.
    """
    entries = tuple(watched.values())
    while frame is not None:
        file = frame.f_code.co_filename
        for entry in entries:
            if file.startswith(entry.directory):
                return frame, entry
        if not through_core and (file.endswith("socks.py") or (file.startswith(CORE_DIR) and file != SESSION_FILE)):
            return None
        frame = frame.f_back
    return None


def instance_of(frame: Optional[FrameType], entry: Watch) -> Any:
    """The service instance nearest to ``frame``, which knows the proxy of its own run.

    The walk goes on past helper functions of the service, which have no ``self``.
    """
    while frame is not None:
        inst = frame.f_locals.get("self")
        if isinstance(inst, entry.cls) and getattr(inst, "session", None) is not None:
            return inst
        frame = frame.f_back
    return None


def run_proxy_for(frame: Optional[FrameType]) -> Optional[str]:
    """The proxy of the run of the service code that called into core at ``frame``.

    A download thread has no service frame. With one watched service, as in a CLI run, its proxy applies.
    """
    found = find_service(frame, through_core=True) if watched else None
    if not found:
        entries = tuple(watched.values())
        return entries[0].proxies[-1] if len(entries) == 1 and len(entries[0].proxies) == 1 else None
    service_frame, entry = found
    inst = instance_of(service_frame, entry)
    if inst is not None:
        return proxy_of(getattr(inst, "session", None))
    return entry.proxies[-1] if entry.proxies else None


def fallback_session() -> requests.Session:
    """A new requests session for a core helper that got no session, on the proxy of the calling service."""
    session = requests.Session()
    proxy = run_proxy_for(sys._getframe(1))
    if proxy:
        session.proxies.update({"all": proxy})
        pin(session)
    return session


def cdm_hosts() -> set[str]:
    """The hosts of the remote CDMs in the config: a CDM API of the user, which a service calls direct."""
    from unshackle.core.config import config

    return {hostname(str(x.get("host") or x.get("Host") or "")) for x in config.remote_cdm}


def is_local(host: str) -> bool:
    """True for this machine and the local network, which never see the internet IP of the user."""
    if host in ("localhost", "localhost.", "localhost.localdomain"):
        return True
    if host.endswith((".localhost", ".local", ".lan", ".home.arpa")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_link_local or ip.is_unspecified


def audit(event: str, args: tuple) -> None:
    """Python raises ``socket.getaddrinfo`` for each connection that requests or urllib opens.

    A raw ``socket.connect`` to a host name looks up the name in C with no ``socket.getaddrinfo`` event.
    A connect to an IP address follows a lookup that this hook already saw, so only a host name is checked.
    """
    try:
        if not watched:
            return
        if event == "socket.getaddrinfo":
            check(args[0], sys._getframe(1))
        elif event == "socket.connect" and isinstance(args[1], tuple) and isinstance(args[1][0], str):
            try:
                ipaddress.ip_address(args[1][0].split("%")[0])
            except ValueError:
                check(args[1][0], sys._getframe(1))
    except Exception:  # nosec B110 - an audit hook that raises stops the lookup of the caller
        pass


def check(host: Any, frame: Optional[FrameType]) -> None:
    """Warn when the innermost unshackle frame that sends a request to ``host`` is service code.

    A method of a service instance knows the proxy of its own run, so a run with no proxy never warns.
    """
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    host = str(host or "").lower().strip("[]")
    if not host or is_local(host) or host in proxy_hosts or host in cdm_hosts():
        return
    found = find_service(frame, through_core=False)
    if not found:
        return
    service_frame, entry = found
    while frame is not None and frame is not service_frame:
        # the requests frames between the lookup and the service code hold the HTTP session that sends it
        if getattr(frame.f_locals.get("self"), "proxy_exempt", False) is True:
            return
        frame = frame.f_back
    inst = instance_of(service_frame, entry)
    if inst is not None and not proxy_of(getattr(inst, "session", None)):
        return
    key = (entry.cls.__name__, host, id(inst))
    if key in warned:
        return
    warned.add(key)
    # a service instance logs through self.log, which a remote session mirrors to its client
    log = getattr(inst, "log", None) or logging.getLogger(entry.cls.__name__)
    where = os.path.relpath(service_frame.f_code.co_filename, os.path.dirname(entry.directory[:-1]))
    log.warning(
        f"A request to {host} did not go through the proxy, so {host} sees your own IP "
        f"({where}:{service_frame.f_lineno})"
    )
