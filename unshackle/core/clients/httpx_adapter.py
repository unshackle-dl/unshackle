import httpx
from .base import BaseHttpClient, register
from .exceptions import NetworkError


@register("httpx")
class HttpxAdapter(BaseHttpClient):
    def _build_client(self, config):
        return httpx.Client(
            headers=config.headers,
            **config.adapter_options,  # pass adapter-specific options
        )
