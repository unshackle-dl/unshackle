import os

import requests
from .base import BaseHttpClient, register
from .exceptions import NetworkError
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context


class CustomHttpAdapter(HTTPAdapter):
    def __init__(self, ca_bundle=None, *args, **kwargs):
        self.ca_bundle = ca_bundle
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        if self.ca_bundle:
            kwargs['ssl_context'] = create_urllib3_context()
            kwargs['ssl_context'].load_verify_locations(self.ca_bundle)
        return super().init_poolmanager(*args, **kwargs)


@register("requests")
class RequestsAdapter(BaseHttpClient):
    def _build_client(self, config):
        if len(config.args) > 0:
            raise RuntimeError('Requests client does not support custom args')
        session = requests.Session()
        session.headers.update(config.headers)
        if config.proxy:
            session.proxies.update({"all": config.proxy})
        ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or requests.certs.where()
        cha = CustomHttpAdapter(
            ca_bundle=ca_bundle,
            # max_retries= ,
            # pool_connections= ,
            # pool_maxsize= ,
        )
        session.mount("https://", cha)
        session.mount("http://", cha)
        return session

    def _wrap_exception(self, exc: Exception):
        if isinstance(exc, requests.RequestException):
            return NetworkError(str(exc))
        return exc
