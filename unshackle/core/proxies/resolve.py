"""Shared proxy provider initialization and resolution.

Used by both the REST API handlers and the remote service client.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

log = logging.getLogger("proxies")


def initialize_proxy_providers() -> List[Any]:
    """Initialize and return available proxy providers from config."""
    proxy_providers: list = []
    try:
        from unshackle.core import binaries
        from unshackle.core.config import config as main_config
        from unshackle.core.proxies.basic import Basic
        from unshackle.core.proxies.expressvpn import ExpressVPN
        from unshackle.core.proxies.hola import Hola
        from unshackle.core.proxies.nordvpn import NordVPN
        from unshackle.core.proxies.proton import ProtonVPN
        from unshackle.core.proxies.surfsharkvpn import SurfsharkVPN
        from unshackle.core.proxies.v2ray import V2Ray

        proxy_config = getattr(main_config, "proxy_providers", {})

        if proxy_config.get("basic"):
            proxy_providers.append(Basic(**proxy_config["basic"]))
        # ExpressVPN/ProtonVPN auto-load when their default cookie file exists (no yaml needed)
        expressvpn = ExpressVPN(**(proxy_config.get("expressvpn") or {}))
        if proxy_config.get("expressvpn") or expressvpn.cache_path.is_file():
            proxy_providers.append(expressvpn)
        if proxy_config.get("nordvpn"):
            proxy_providers.append(NordVPN(**proxy_config["nordvpn"]))
        proton = ProtonVPN(**(proxy_config.get("protonvpn") or {}))
        if proxy_config.get("protonvpn") or proton.cookie_path.is_file():
            proxy_providers.append(proton)
        if proxy_config.get("surfsharkvpn"):
            proxy_providers.append(SurfsharkVPN(**proxy_config["surfsharkvpn"]))
        # V2Ray is config-gated: a ``v2ray:`` block (even an empty one) is required to
        # opt in. This avoids instantiating a provider for users who have xray installed
        # for unrelated reasons. Direct-URI mode (``--proxy v2ray:vmess://...``) works
        # with an empty ``v2ray: {}`` block.
        if proxy_config.get("v2ray") is not None:
            proxy_providers.append(V2Ray(**proxy_config["v2ray"]))
        if hasattr(binaries, "HolaProxy") and binaries.HolaProxy:
            proxy_providers.append(Hola())

        for provider in proxy_providers:
            log.info(f"Loaded {provider.__class__.__name__}: {provider}")

        if not proxy_providers:
            log.warning("No proxy providers were loaded. Check your proxy provider configuration in unshackle.yaml")

    except Exception as e:
        log.warning(f"Failed to initialize some proxy providers: {e}")

    return proxy_providers


def resolve_proxy(proxy: str, proxy_providers: List[Any]) -> Optional[str]:
    """Resolve a proxy parameter to an actual proxy URI.

    Accepts:
      - Direct URI: "https://...", "socks5://..."
      - Country code: "us", "uk"
      - Provider:country: "nordvpn:us"
      - Provider:URI: "v2ray:vmess://..." (V2Ray direct-URI mode)
    """
    if not proxy:
        return None

    if re.match(r"^(https?://|socks)", proxy):
        return proxy

    requested_provider = None
    query = proxy
    # Provider prefix is a name starting with a letter, followed by letters/digits, then ":".
    # The digit allowance is required so provider names like "v2ray" (which contains a "2")
    # are recognised.
    if re.match(r"^[a-z][a-z0-9]*:.+$", proxy, re.IGNORECASE):
        requested_provider, query = proxy.split(":", maxsplit=1)

    if requested_provider:
        provider = next(
            (x for x in proxy_providers if x.__class__.__name__.lower() == requested_provider.lower()),
            None,
        )
        if not provider:
            available = [x.__class__.__name__ for x in proxy_providers]
            raise ValueError(f"Proxy provider '{requested_provider}' not found. Available: {available}")
        proxy_uri = provider.get_proxy(query)
        if not proxy_uri:
            raise ValueError(f"Proxy provider {requested_provider} had no proxy for {query}")
        log.info(f"Using {provider.__class__.__name__} Proxy: {proxy_uri}")
        return proxy_uri

    for provider in proxy_providers:
        proxy_uri = provider.get_proxy(query)
        if proxy_uri:
            log.info(f"Using {provider.__class__.__name__} Proxy: {proxy_uri}")
            return proxy_uri

    raise ValueError(f"No proxy provider had a proxy for {proxy}")
