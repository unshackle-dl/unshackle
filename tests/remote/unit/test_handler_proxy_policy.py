"""The server spends its own proxy subscriptions only for a key that opts in."""

from types import SimpleNamespace

import pytest

from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError, APIErrorCode
from unshackle.core.utils import ip_info

pytestmark = pytest.mark.unit

USERS = {
    "plainkey": {"username": "plain"},
    "proxykey": {"username": "proxy", "server_proxy": True},
    "strkey": {"username": "str", "server_proxy": "true"},
}


@pytest.fixture(autouse=True)
def serve_users(monkeypatch):
    monkeypatch.setattr(handlers.config, "serve", {"users": USERS}, raising=False)
    monkeypatch.setattr(handlers, "request_secret_key", lambda request: request.headers.get("X-Secret-Key"))


def request(key):
    return SimpleNamespace(headers={"X-Secret-Key": key} if key else {})


def patch_server_region(monkeypatch, region):
    monkeypatch.setattr(ip_info, "get_ip_info", lambda session=None, cached=False: region and {"country": region})


def patch_providers(monkeypatch, proxy):
    """Give the server one provider that answers every country code with ``proxy``."""
    provider = SimpleNamespace(get_proxy=lambda query: proxy)
    monkeypatch.setattr(handlers, "initialize_proxy_providers", lambda: [provider])
    return provider


def test_region_mismatch_raises(monkeypatch):
    patch_server_region(monkeypatch, "IN")
    with pytest.raises(APIError) as exc_info:
        handlers.resolve_handler_proxy({"client_region": "ca"}, "EXAMPLE", request("plainkey"))
    assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY
    assert "IN" not in exc_info.value.message
    # The details must not leak the server region either.
    assert exc_info.value.details == {"service": "EXAMPLE"}


def test_region_match_no_proxy(monkeypatch):
    patch_server_region(monkeypatch, "CA")
    assert handlers.resolve_handler_proxy({"client_region": "ca"}, "EXAMPLE", request("plainkey")) == (None, [])


def test_region_unknown_no_error(monkeypatch):
    patch_server_region(monkeypatch, None)
    assert handlers.resolve_handler_proxy({"client_region": "ca"}, "EXAMPLE", request("plainkey")) == (None, [])


def test_direct_uri_passes_through(monkeypatch):
    patch_server_region(monkeypatch, "IN")
    proxy, providers = handlers.resolve_handler_proxy(
        {"proxy": "socks5://user:pass@host:1080"}, "EXAMPLE", request("plainkey")
    )
    assert proxy == "socks5://user:pass@host:1080"
    assert providers == []


def test_country_code_rejected(monkeypatch):
    patch_server_region(monkeypatch, "IN")
    with pytest.raises(APIError) as exc_info:
        handlers.resolve_handler_proxy({"proxy": "us"}, "EXAMPLE", request("plainkey"))
    assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY
    assert "us" not in exc_info.value.details.values()


def test_provider_country_rejected(monkeypatch):
    patch_server_region(monkeypatch, "IN")
    with pytest.raises(APIError) as exc_info:
        handlers.resolve_handler_proxy({"proxy": "nordvpn:us"}, "EXAMPLE", request("plainkey"))
    assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY
    assert "nordvpn" not in exc_info.value.message.lower()
    assert exc_info.value.details == {"service": "EXAMPLE"}


def test_no_proxy_short_circuits(monkeypatch):
    """no_proxy skips resolution and never loads providers, even with a proxy param set."""
    monkeypatch.setattr(handlers, "initialize_proxy_providers", lambda: pytest.fail("providers loaded"))
    _, providers = handlers.resolve_handler_proxy(
        {"proxy": "us", "no_proxy": True, "client_region": "ca"}, "EXAMPLE", request("proxykey")
    )
    assert providers == []


def test_server_proxy_allowed_entitlements():
    assert handlers.server_proxy_allowed(request("proxykey")) is True
    assert handlers.server_proxy_allowed(request("plainkey")) is False
    # yaml string "true" is not the boolean opt-in
    assert handlers.server_proxy_allowed(request("strkey")) is False
    # no implicit admin access: a key absent from serve.users is denied
    assert handlers.server_proxy_allowed(request("adminsecret")) is False
    # no request (an internal call, or a handler that dropped it) fails closed
    assert handlers.server_proxy_allowed(None) is False


def test_opted_in_country_code_resolves(monkeypatch):
    patch_server_region(monkeypatch, "IN")
    provider = patch_providers(monkeypatch, "http://us.proxy:8080")
    proxy, providers = handlers.resolve_handler_proxy({"proxy": "us"}, "EXAMPLE", request("proxykey"))
    assert proxy == "http://us.proxy:8080"
    assert providers == [provider]


def test_opted_in_region_mismatch_auto_proxies(monkeypatch):
    patch_server_region(monkeypatch, "IN")
    patch_providers(monkeypatch, "http://ca.proxy:8080")
    proxy, providers = handlers.resolve_handler_proxy({"client_region": "ca"}, "EXAMPLE", request("proxykey"))
    assert proxy == "http://ca.proxy:8080"
    assert providers


def test_absent_key_denied(monkeypatch):
    patch_server_region(monkeypatch, "IN")
    patch_providers(monkeypatch, "http://ca.proxy:8080")
    with pytest.raises(APIError) as exc_info:
        handlers.resolve_handler_proxy({"client_region": "ca"}, "EXAMPLE", request("adminsecret"))
    assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY


def test_user_without_server_proxy_denied(monkeypatch):
    patch_server_region(monkeypatch, "IN")
    patch_providers(monkeypatch, "http://ca.proxy:8080")
    with pytest.raises(APIError) as exc_info:
        handlers.resolve_handler_proxy({"client_region": "ca"}, "EXAMPLE", request("plainkey"))
    assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY


def test_no_region_sent_proceeds(monkeypatch):
    """An out-of-date client that reports no region is unknown and never blocked."""
    patch_server_region(monkeypatch, "IN")
    monkeypatch.setattr(handlers, "initialize_proxy_providers", lambda: [])
    assert handlers.resolve_handler_proxy({}, "EXAMPLE", request("plainkey")) == (None, [])
    assert handlers.resolve_handler_proxy({}, "EXAMPLE", request("proxykey")) == (None, [])


@pytest.mark.parametrize("key", ["plainkey", "proxykey"])
def test_non_string_client_region_rejected(monkeypatch, key):
    patch_providers(monkeypatch, "http://ca.proxy:8080")
    with pytest.raises(APIError) as exc_info:
        handlers.resolve_handler_proxy({"client_region": 1}, "EXAMPLE", request(key))
    assert exc_info.value.error_code == APIErrorCode.INVALID_INPUT


def test_setup_list_service_threads_request(monkeypatch):
    """A regression that stops passing the request through would re-open the proxy gate."""
    seen = {}

    def fake_resolve(data, service, request=None):
        seen["request"] = request
        return None, []

    class FakeService:
        def authenticate(self, cookies, credential):
            seen["authenticated"] = True

    monkeypatch.setattr(handlers, "load_service_yaml", lambda service: {})
    monkeypatch.setattr(handlers, "resolve_handler_proxy", fake_resolve)
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *args: None)
    monkeypatch.setattr(handlers, "build_parent_ctx", lambda *args: None)
    monkeypatch.setattr(handlers.Services, "load", lambda service: None)
    monkeypatch.setattr(handlers, "instantiate_service", lambda *args: FakeService())
    monkeypatch.setattr("unshackle.commands.dl.dl.get_cookie_jar", staticmethod(lambda service, profile: None))
    monkeypatch.setattr("unshackle.commands.dl.dl.get_credentials", staticmethod(lambda service, profile: None))

    req = request("plainkey")
    handlers.setup_list_service({}, "EXAMPLE", None, "some-title", req)
    assert seen["request"] is req
    assert seen["authenticated"]


class TestDownloadGates:
    """/api/download submissions and retries obey the same proxy policy as sessions."""

    @pytest.fixture(autouse=True)
    def cdm_gate_open(self, monkeypatch):
        monkeypatch.setattr(handlers, "server_cdm_allowed", lambda request=None, service=None: True)

    def test_country_code_rejected_when_denied(self, monkeypatch):
        with pytest.raises(APIError) as exc_info:
            handlers.enforce_download_gates({"service": "EXAMPLE", "proxy": "us"}, request("plainkey"))
        assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY

    def test_provider_country_rejected_when_denied(self, monkeypatch):
        with pytest.raises(APIError) as exc_info:
            handlers.enforce_download_gates({"service": "EXAMPLE", "proxy": "nordvpn:us"}, request("plainkey"))
        assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY

    def test_full_uri_passes_when_denied(self):
        handlers.enforce_download_gates(
            {"service": "EXAMPLE", "proxy": "socks5://user:pass@host:1080"}, request("plainkey")
        )

    def test_no_proxy_param_passes_when_denied(self):
        handlers.enforce_download_gates({"service": "EXAMPLE"}, request("plainkey"))

    def test_country_code_passes_when_opted_in(self, monkeypatch):
        # The gate must not even try to resolve for an opted-in key: dl resolves at run time,
        # exactly as before the gate existed.
        monkeypatch.setattr(handlers, "initialize_proxy_providers", lambda: pytest.fail("providers loaded"))
        handlers.enforce_download_gates({"service": "EXAMPLE", "proxy": "us"}, request("proxykey"))

    def test_region_mismatch_rejected_at_submit(self, monkeypatch):
        """A queued job reaches the same region verdict, at submission."""
        patch_server_region(monkeypatch, "IN")
        with pytest.raises(APIError) as exc_info:
            handlers.enforce_download_gates({"service": "EXAMPLE", "client_region": "gb"}, request("plainkey"))
        assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY


class TestDownloadEnforcement:
    """The server_proxy stamp survives from submission to the dl construction."""

    @pytest.fixture
    def created_params(self, monkeypatch):
        """Drive download_handler with the job machinery faked out; capture create_job params."""
        import asyncio
        from datetime import datetime, timezone

        from unshackle.core.api import download_manager as dm

        created = {}

        class FakeManager:
            async def start_workers(self):
                pass

            def create_job(self, service, title_id, owner_key=None, **params):
                created.update(params)
                return SimpleNamespace(
                    job_id="job1", status=SimpleNamespace(value="queued"), created_time=datetime.now(timezone.utc)
                )

        monkeypatch.setattr(dm, "get_download_manager", lambda: FakeManager())
        monkeypatch.setattr(handlers, "validate_service", lambda service, request=None: "EXAMPLE")
        monkeypatch.setattr(handlers, "enforce_download_gates", lambda params, request=None: None)
        monkeypatch.setattr(handlers.Services, "load", lambda service: SimpleNamespace())

        def run(body, req):
            response = asyncio.run(handlers.download_handler(body, req))
            assert response.status == 202
            return created

        return run

    def test_client_sent_stamp_is_overwritten(self, created_params):
        created = created_params({"service": "EXAMPLE", "title_id": "t1", "server_proxy": True}, request("plainkey"))
        assert created["server_proxy"] is False

    def test_opted_in_key_is_stamped_true(self, created_params):
        created = created_params({"service": "EXAMPLE", "title_id": "t1"}, request("proxykey"))
        assert created["server_proxy"] is True

    @pytest.mark.parametrize(("params", "expected"), [({}, []), ({"server_proxy": True}, None)])
    def test_perform_download_maps_stamp_to_dl_providers(self, monkeypatch, tmp_path, params, expected):
        """An unstamped or denied job hands dl an empty provider list; a stamped one loads config."""
        import click

        from unshackle.commands import dl as dl_module
        from unshackle.core.api import download_manager as dm

        seen = {}

        class Stop(Exception):
            pass

        class FakeDl:
            cli = click.Group("dl")

            def __init__(self, **kwargs):
                seen.update(kwargs)
                raise Stop

        monkeypatch.setattr(dl_module, "dl", FakeDl)
        monkeypatch.setattr("unshackle.core.services.Services.get_path", staticmethod(lambda service: tmp_path))
        monkeypatch.setattr(handlers, "load_full_cdm", lambda *args: None)

        with pytest.raises(Stop):
            dm.perform_download("job1", "EXAMPLE", "t1", dict(params))
        assert seen["proxy_providers"] == expected
