from typing import Dict, Type, Optional, Union
from .base import BaseHttpClient, _REGISTRY
from .httpx_adapter import HttpxAdapter
from .requests_adapter import RequestsAdapter
from .curl_cffi_adapter import CurlCffiAdapter
from .config import load_config, HttpClientConfig


class HttpClientFactory:

    def __init__(self):
        self.proxy = None
        self.clients = {}

    def get(self, name:str = 'default',  config: Optional[Union[HttpClientConfig,dict]] = None) -> BaseHttpClient:
        try:
            return self.clients[name]
        except:
            pass
        # TODO manage config, e.g. session.headers.update(config.headers)
        if isinstance(config, dict):
            config = load_config(config)
        cls = _REGISTRY[config.type]
        self.clients[name] = cls(config)
        return self.clients[name]

    def __getitem__(self, name: str) -> BaseHttpClient:
        return self.get(name)

    def set_proxy(self, proxy):
        self.proxy = proxy

http_unshackle = HttpClientFactory()



