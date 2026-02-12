from typing import Dict, Type, Any
from .config import HttpClientConfig, load_config
from .retry import build_retry
from .exceptions import NetworkError, NetworkHTTPError


class BaseHttpClient:
    def __init__(self, config: HttpClientConfig):
        self._config = config
        self._retry_config = config.retry
        self._retry = build_retry(self._retry_config)
        self._retry_methods = set(self._retry_config.retry_methods)
        self._retry_statuses = set(self._retry_config.retry_statuses)

        self._client = self._build_client(config)

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

    # Forward everything else
    def __getattr__(self, item):
        return getattr(self._client, item)


_REGISTRY: Dict[str, Type[BaseHttpClient]] = {}


def register(name: str):
    def decorator(cls: Type[BaseHttpClient]):
        _REGISTRY[name] = cls
        return cls
    return decorator
