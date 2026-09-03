"""Shared proxy provider initialization and resolution.

Used by both the REST API handlers and the remote service client.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

from unshackle.core.utils.redact import mask_proxy

log = logging.getLogger("proxies")


def initialize_proxy_providers(raise_errors: bool = False, quiet: bool = False) -> List[Any]:
    """Initialise and return available proxy providers from config.

    A provider that fails to build is logged and skipped, unless *raise_errors* is set.
    *quiet* drops the per-provider summary lines: rendering a provider asks some of them for
    their server catalog over the network, which a repeated caller must not pay for.
    """
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
        if hasattr(binaries, "HolaProxy") and binaries.HolaProxy:
            proxy_providers.append(Hola())

        if not quiet:
            for provider in proxy_providers:
                log.info(f"Loaded {provider.__class__.__name__}: {provider}")

            if not proxy_providers:
                log.warning("No proxy providers were loaded. Check your proxy provider configuration in unshackle.yaml")

    except Exception as e:
        if raise_errors:
            raise
        log.warning(f"Failed to initialize some proxy providers: {e}")

    return proxy_providers


def resolve_proxy(proxy: str, proxy_providers: List[Any]) -> Optional[str]:
    """Change a proxy parameter to a real proxy URI.

    Accepts:
      - Direct URI: "https://...", "socks5://..."
      - Country code: "us", "uk"
      - Provider:country: "nordvpn:us"
    """
    if not proxy:
        return None

    if re.match(r"^(https?://|socks)", proxy):
        return proxy

    requested_provider = None
    query = proxy
    if re.match(r"^[a-z]+:.+$", proxy, re.IGNORECASE):
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
