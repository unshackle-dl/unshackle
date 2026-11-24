"""pywidevine and pyplayready remote CDMs without the upstream `Server` header probe.

Upstream sends a HEAD request to the host and reads the `Server` header: pywidevine refuses a host
that does not name pywidevine serve, and pyplayready logs a warning. A reverse proxy or CDN in front
of the server strips or rewrites that header, so a working server fails. A host that is not a serve
API now fails at the first API call instead.
"""

from __future__ import annotations

import functools
from typing import Any, Optional

import requests
from Crypto.PublicKey import RSA
from pyplayready.cdm import Cdm as PlayReadyCdm
from pyplayready.remote.remotecdm import RemoteCdm as PlayReadyRemoteCdm
from pywidevine.cdm import Cdm as WidevineCdm
from pywidevine.license_protocol_pb2 import ClientIdentification
from pywidevine.remotecdm import RemoteCdm as WidevineRemoteCdm


class SecretSession(requests.Session):
    """Drop the serve API secret on a redirect where requests would drop `Authorization`."""

    def rebuild_auth(self, prepared_request: requests.PreparedRequest, response: requests.Response) -> None:
        super().rebuild_auth(prepared_request, response)
        if self.should_strip_auth(response.request.url, prepared_request.url):
            prepared_request.headers.pop("X-Secret-Key", None)


def _connect(cdm: Any, drm: str, host: Optional[str], secret: Optional[str], device_name: Optional[str]) -> None:
    """Set what upstream `__init__` sets after the probe, on the name-mangled upstream session."""
    if not (host and secret and device_name):
        raise ValueError(f"A {drm} remote CDM needs host, secret and device_name")
    cdm.host = host
    cdm.device_name = device_name
    cdm._RemoteCdm__session = SecretSession()
    cdm._RemoteCdm__session.headers["X-Secret-Key"] = secret


@functools.cache
def _placeholder_key() -> RSA.RsaKey:
    """An RSA private key only to satisfy `Cdm.__init__`; the remote CDM never uses it."""
    return RSA.generate(2048)


class UnprobedWidevineRemoteCdm(WidevineRemoteCdm):
    def __init__(
        self,
        device_type: Any,
        system_id: int,
        security_level: int,
        host: Optional[str],
        secret: Optional[str],
        device_name: Optional[str],
    ) -> None:
        _connect(self, "Widevine", host, secret, device_name)
        WidevineCdm.__init__(self, device_type, system_id, security_level, ClientIdentification(), _placeholder_key())


class UnprobedPlayReadyRemoteCdm(PlayReadyRemoteCdm):
    def __init__(
        self, security_level: int, host: Optional[str], secret: Optional[str], device_name: Optional[str]
    ) -> None:
        if not isinstance(security_level, int):
            raise TypeError(f"A PlayReady remote CDM needs an int security_level, not {security_level!r}")
        _connect(self, "PlayReady", host, secret, device_name)
        PlayReadyCdm.__init__(self, security_level, None, None, None)
