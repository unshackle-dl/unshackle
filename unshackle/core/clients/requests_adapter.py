import requests
from .base import BaseHttpClient, register
from .exceptions import NetworkError


@register("requests")
class RequestsAdapter(BaseHttpClient):
    def _build_client(self, config):
        session = requests.Session()
        session.headers.update(config.headers)

        # Adapter-specific options (optional)
        timeout = config.adapter_options.get("timeout")
        if timeout:
            session.timeout = timeout  # custom usage if desired

        return session

    def _wrap_exception(self, exc: Exception):
        if isinstance(exc, requests.RequestException):
            return NetworkError(str(exc))
        return exc
