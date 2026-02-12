import base64
from typing import Dict, Type, Any
from urllib.parse import urlparse

from .config import HttpClientConfig, load_config
from .retry import build_retry
from .exceptions import NetworkError, NetworkHTTPError
from ..utils.collections import merge_dict_case_insensitive


class BaseHttpClient:
    def __init__(self, config: HttpClientConfig):
        if config.proxy:
            proxy_parse = urlparse(config.proxy)
            if proxy_parse.username and proxy_parse.password:
                merge_dict_case_insensitive(
                    {
                        "Proxy-Authorization": base64.b64encode(
                            f"{proxy_parse.username}:{proxy_parse.password}".encode("utf8")
                        ).decode()
                    },
                    config.headers
                )
        self._config = config
        self._retry_config = config.retry
        self._retry = build_retry(self._retry_config)
        self._retry_methods = set(self._retry_config.retry_methods)
        self._retry_statuses = set(self._retry_config.retry_statuses)

        self._client = self._build_client(config)

    def update_config(self, config: HttpClientConfig):
        if config.type != self._config.type:
            raise RuntimeError('Cannot change client type, create new client')
        if config.proxy != self._config.proxy:
            raise RuntimeError('Cannot change proxy, create new client')
        if (len(config.args)>0) and (config.args != self._config.args):
            raise RuntimeError('Cannot change constructor args, create new client')
        self._client.headers.update(config.headers)

    # --------------------
    # Adapter must implement
    # --------------------

    def _build_client(self, config: HttpClientConfig):
        raise NotImplementedError

    def _wrap_exception(self, exc: Exception) -> Exception:
        raise NotImplementedError

    # --------------------
    # Core execution
    # --------------------

    def _execute(self, method: str, url: str, **kwargs: Any):
        method = method.upper()

        def raw_call():
            try:
                response = self._client.request(method, url, **kwargs)
            except Exception as exc:
                raise NetworkError(str(exc)) from exc
            if (
                method in self._retry_methods
                and response.status_code in self._retry_statuses
            ):
                raise NetworkHTTPError(response.status_code)
            return response

        if method in self._retry_methods:
            response = self._retry(raw_call)()
        else:
            response = raw_call()
        response.raise_for_status()
        return response

    # --------------------
    # Public API
    # --------------------

    def get(self, url: str, **kwargs: Any):
        return self._execute("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self._execute("POST", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any):
        return self._execute(method, url, **kwargs)

    def close(self):
        self._client.close()

    def get_proxy(self):
        return next(iter(self.proxies.values()), None)

    def set_client_proxy(self, proxy):
        raise NotImplementedError()

    def get_config(self):
        return self._config

    # Forward everything else
    def __getattr__(self, item):
        return getattr(self._client, item)


_REGISTRY: Dict[str, Type[BaseHttpClient]] = {}


def register(name: str):
    def decorator(cls: Type[BaseHttpClient]):
        _REGISTRY[name] = cls
        return cls
    return decorator
