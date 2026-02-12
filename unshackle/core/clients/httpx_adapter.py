import os

import httpx
from .base import BaseHttpClient, register
from .exceptions import NetworkError


@register("httpx")
class HttpxAdapter(BaseHttpClient):
    def _build_client(self, config):
        return httpx.Client(
            headers=config.headers,
            proxy=config.proxy,
            verify=os.environ.get('REQUESTS_CA_BUNDLE', None), # honor env var for CA certs
            **config.args,  # pass adapter-specific options
        )
