"""Shared proxy provider loading, lookup and resolution for dl, search, the REST API and the remote client."""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any, List, Optional
from urllib.parse import urlparse

from unshackle.core.proxies.proxy import Proxy
from unshackle.core.utils.redact import mask_proxy

log = logging.getLogger("proxies")

REGION = r"[a-z]{2,4}(?:_[a-z0-9]{2,4})?(?:-[a-z0-9]+|:[a-z][a-z0-9]*)*\d*"


class Unavailable(Proxy):
    """Stands in for a proxy provider that failed to load: a bare query skips it, a query that names it fails."""

    def __init__(self, name: str, error: Exception) -> None:
        self.name = name
        # The message only: the exception's traceback would keep the failed build's frames alive.
        self.error = str(error)

    def __repr__(self) -> str:
        return f"failed to load ({self.error})"

    def get_proxy(self, query: str) -> Optional[str]:
        return None


def find_provider(proxy_providers: List[Any], name: str) -> Any:
    """
    Find a proxy provider by its class name, case-insensitive.

    Raises ValueError when no proxy provider has that name, or when it failed to load.
    """
    for provider in proxy_providers:
        if isinstance(provider, Unavailable):
            if provider.name.lower() == name.lower():
                raise ValueError(f"The proxy provider {provider.name} could not load: {provider.error}")
        elif provider.__class__.__name__.lower() == name.lower():
            return provider
    available = [x.__class__.__name__ for x in proxy_providers if not isinstance(x, Unavailable)]
    raise ValueError(f"The proxy provider '{name}' was not found. Available: {available}")


def load_proxy_providers(exclude: tuple[str, ...] = (), raise_errors: bool = False, quiet: bool = False) -> List[Any]:
    """
    Build the proxy providers set in config, in the order a bare query tries them.

    Each proxy provider loads on its own. One that fails is logged and kept as Unavailable, so a bare
    query skips it and a query that names it reports why. *raise_errors* raises the failure instead.
    *exclude* holds lowercase class names to leave out.
    """
    from unshackle.core import binaries
    from unshackle.core.config import config
    from unshackle.core.proxies import (
        Basic,
        ControlD,
        ExpressVPN,
        Gluetun,
        Hola,
        NordVPN,
        ProtonVPN,
        SurfsharkVPN,
        WindscribeVPN,
    )

    settings = config.proxy_providers
    # Control D is last: a bare region reaches it only when nothing else answers, since it changes the account.
    classes = (Basic, ExpressVPN, NordVPN, ProtonVPN, SurfsharkVPN, WindscribeVPN, Gluetun, Hola, ControlD)

    def build(cls: type) -> Any:
        key = cls.__name__.lower()
        if cls is Hola:
            return Hola() if binaries.HolaProxy else None
        if cls is ExpressVPN or cls is ProtonVPN:
            # These also load with no config when their default cookie file exists.
            provider = cls(**(settings.get(key) or {}))
            cookie = provider.cache_path if cls is ExpressVPN else provider.cookie_path
            return provider if settings.get(key) or cookie.is_file() else None
        return cls(**settings[key]) if settings.get(key) else None

    proxy_providers: list = []
    for cls in classes:
        name = cls.__name__
        if name.lower() in exclude:
            continue
        try:
            provider = build(cls)
            if provider is None:
                continue
            if not quiet:
                log.info(f"Loaded {name}: {provider}")
        except Exception as e:
            if raise_errors:
                raise
            log.warning(f"The {name} proxy provider could not load, so a bare --proxy query skips it: {e}")
            provider = Unavailable(name, e)
        proxy_providers.append(provider)

    return proxy_providers


def initialize_proxy_providers(raise_errors: bool = False, quiet: bool = False) -> List[Any]:
    """
    Load the proxy providers for the REST API and the remote service client.

    WindscribeVPN and Gluetun are not offered on this path. *quiet* drops the per-provider summary
    lines: rendering a proxy provider asks some of them for their server catalogue over the network,
    which a repeated caller must not pay for.
    """
    proxy_providers = load_proxy_providers(("windscribevpn", "gluetun"), raise_errors, quiet)
    if not quiet and not proxy_providers:
        log.warning("No proxy providers were loaded. Check your proxy provider configuration in unshackle.yaml")
    return proxy_providers


def resolve_proxy(proxy: str, proxy_providers: List[Any]) -> Optional[str]:
    """Change a proxy parameter to a real proxy URI.

    Accepts:
      - Direct URI: "https://...", "socks5://..."
      - Country code: "us", "uk"
      - Provider:country: "nordvpn:us"
      - A Control D resolver sent by a client: "controld://<resolver>@dns.controld.com"
    """
    if not proxy:
        return None

    if proxy.startswith("controld://"):
        from unshackle.core.proxies.controld import local_proxy

        return local_proxy(proxy)

    if re.match(r"^(https?://|socks)", proxy):
        return proxy

    requested_provider = None
    query = proxy
    if re.match(r"^[a-z]+:.+$", proxy, re.IGNORECASE):
        requested_provider, query = proxy.split(":", maxsplit=1)

    if requested_provider:
        provider = find_provider(proxy_providers, requested_provider)
        proxy_uri = provider.get_proxy(query)
        if not proxy_uri:
            raise ValueError(f"Proxy provider {requested_provider} had no proxy for {query}")
        log.info(
            f"Using {provider.__class__.__name__} Proxy: "
            f"{mask_proxy(proxy_uri, provider.__class__.__name__ == 'Basic', allow_debug=False)}"
        )
        return proxy_uri

    for provider in proxy_providers:
        proxy_uri = provider.get_proxy(query)
        if proxy_uri:
            log.info(
                f"Using {provider.__class__.__name__} Proxy: "
                f"{mask_proxy(proxy_uri, provider.__class__.__name__ == 'Basic', allow_debug=False)}"
            )
            return proxy_uri

    raise ValueError(f"No proxy provider had a proxy for {proxy}")


def is_loopback(uri: str) -> bool:
    """Whether a proxy or server URI points at this machine."""
    parsed = urlparse(uri if "//" in uri else f"//{uri}")
    host = parsed.hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
