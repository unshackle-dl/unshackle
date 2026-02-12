import base64
from dataclasses import asdict
from typing import Dict, Type, Optional, Union
from urllib.parse import urlparse

from .base import BaseHttpClient, _REGISTRY
from .httpx_adapter import HttpxAdapter
from .requests_adapter import RequestsAdapter
from .curl_cffi_adapter import CurlCffiAdapter
from .config import load_config, HttpClientConfig
from ..config import config as config_unshackle
from ..utils.collections import merge_dict_case_insensitive


class HttpClientFactory:

    def __init__(self):
        self.proxy = None
        self.clients: Dict[str, BaseHttpClient] = {}

    def session(self, name: str = 'default', config: Optional[Union[HttpClientConfig,dict]] = None) -> BaseHttpClient:
        if isinstance(config, HttpClientConfig):
            config = asdict(config)
        final_config_dict = {'proxy': self.proxy}
        merge_dict_case_insensitive(config_unshackle.http.get('default', {}), final_config_dict)
        if name != 'default':
            merge_dict_case_insensitive(config_unshackle.http.get(name, {}), final_config_dict)
        merge_dict_case_insensitive(config, final_config_dict)
        config_obj = load_config(final_config_dict)
        if name in self.clients:
            self.clients[name].update_config(config_obj)
            return self.clients[name]
        cls = _REGISTRY[config_obj.type]
        client = cls(config_obj)
        self.clients[name] = client
        return client

    def get(self, name: str = 'default'):
        return self.clients[name]

    def __getitem__(self, name: str) -> BaseHttpClient:
        return self.get(name)

    def set_default_proxy(self, proxy):
        self.proxy = proxy

http_unshackle = HttpClientFactory()



