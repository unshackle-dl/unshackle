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
    monkeypatch.setattr(handlers, "initialize_proxy_providers", lambda **_: [provider])
    return provider


def test_region_mismatch_raises(monkeypatch):
    patch_server_region(monkeypatch, "IN")
    with pytest.raises(APIError) as exc_info:
        handlers.resolve_handler_proxy({"client_region": "ca"}, "EXAMPLE", request("plainkey"))
    assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY
    assert "IN" not in exc_info.value.message
    # The details must not leak the server region either.
    assert exc_info.value.details == {"service": "EXAMPLE"}


def test_geoblocked_client_region_raises(monkeypatch):
    patch_server_region(monkeypatch, "CA")
    service_class = handlers.Services.load("EXAMPLE")
    monkeypatch.setattr(service_class, "GEOBLOCK", ("GB",), raising=False)
    with pytest.raises(APIError) as exc_info:
        handlers.resolve_handler_proxy({"client_region": "gb"}, "EXAMPLE", request("plainkey"))
    assert exc_info.value.error_code == APIErrorCode.GEOFENCE
    assert exc_info.value.details == {"service": "EXAMPLE"}


def test_region_match_no_proxy(monkeypatch):
    patch_server_region(monkeypatch, "CA")
    assert handlers.resolve_handler_proxy({"client_region": "ca"}, "EXAMPLE", request("plainkey")) == (None, [])


def test_relayed_tag_not_installed_here_skips_the_local_geoblock(monkeypatch):
    patch_server_region(monkeypatch, "CA")
    assert handlers.resolve_handler_proxy({"client_region": "ca"}, "NOTHERE", request("plainkey")) == (None, [])


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
    monkeypatch.setattr(handlers, "initialize_proxy_providers", lambda **_: pytest.fail("providers loaded"))
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
    monkeypatch.setattr(handlers, "initialize_proxy_providers", lambda **_: [])
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
    monkeypatch.setattr(handlers, "build_parent_ctx", lambda *args, **kwargs: None)
    monkeypatch.setattr(handlers.Services, "load", lambda service: None)
    monkeypatch.setattr(handlers, "instantiate_service", lambda *args: FakeService())
    monkeypatch.setattr("unshackle.commands.dl.dl.get_cookie_jar", staticmethod(lambda service, profile: None))
    monkeypatch.setattr("unshackle.commands.dl.dl.get_credentials", staticmethod(lambda service, profile: None))

    req = request("plainkey")
    handlers.setup_list_service({}, "EXAMPLE", None, "some-title", req)
    assert seen["request"] is req
    assert seen["authenticated"]


def test_search_reports_failed_proxy_check_as_invalid_proxy(monkeypatch):
    def failed_check(*args):
        raise ConnectionError("Proxy check failed: no IP lookup got through the proxy.")

    monkeypatch.setattr(handlers, "load_service_yaml", lambda service: {})
    monkeypatch.setattr(handlers, "resolve_handler_proxy", lambda *args: ("http://127.0.0.1:9", []))
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *args: None)
    monkeypatch.setattr(handlers, "build_parent_ctx", lambda *args, **kwargs: None)
    monkeypatch.setattr(handlers.Services, "load", lambda service: None)
    monkeypatch.setattr(handlers, "instantiate_service", failed_check)
    monkeypatch.setattr("unshackle.commands.dl.dl.get_cookie_jar", staticmethod(lambda service, profile: None))
    monkeypatch.setattr("unshackle.commands.dl.dl.get_credentials", staticmethod(lambda service, profile: None))

    with pytest.raises(APIError) as exc_info:
        handlers.run_service_search({}, "EXAMPLE", "query", request("plainkey"))
    assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY
    assert exc_info.value.retryable is False


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
        monkeypatch.setattr(handlers, "initialize_proxy_providers", lambda **_: pytest.fail("providers loaded"))
        handlers.enforce_download_gates({"service": "EXAMPLE", "proxy": "us"}, request("proxykey"))

    def test_region_mismatch_rejected_at_submit(self, monkeypatch):
        """A queued job reaches the same region verdict, at submission."""
        patch_server_region(monkeypatch, "IN")
        with pytest.raises(APIError) as exc_info:
            handlers.enforce_download_gates({"service": "EXAMPLE", "client_region": "gb"}, request("plainkey"))
        assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY

    def test_download_proxy_on_local_network_refused(self):
        with pytest.raises(APIError) as exc_info:
            handlers.enforce_download_gates(
                {"service": "EXAMPLE", "proxy_download": "http://127.0.0.1:9"}, request("plainkey")
            )
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

        from unshackle.commands import dl as dl_module
        from unshackle.core.api import download_manager as dm

        seen = {}

        class Stop(Exception):
            pass

        class FakeDl:
            cli = dl_module.dl.cli

            def __init__(self, **kwargs):
                seen.update(kwargs)
                raise Stop

        monkeypatch.setattr(dl_module, "dl", FakeDl)
        monkeypatch.setattr("unshackle.core.services.Services.get_path", staticmethod(lambda service: tmp_path))
        monkeypatch.setattr(handlers, "load_full_cdm", lambda *args: None)

        with pytest.raises(Stop):
            dm.perform_download("job1", "EXAMPLE", "t1", dict(params))
        assert seen["proxy_providers"] == expected


def test_session_create_builds_service_off_the_event_loop(monkeypatch):
    import asyncio
    import json
    import logging
    import threading

    seen: dict = {}

    class RecordingBuffer(handlers.SessionLogBuffer):
        def __init__(self):
            super().__init__()
            seen["buffer"] = self

    def failed_check(*args, **kwargs):
        seen["thread"] = threading.get_ident()
        logging.getLogger("SlowProxySvc").warning("checking proxy")
        raise ConnectionError("Proxy check failed: no IP lookup got through the proxy.")

    monkeypatch.setattr(handlers, "validate_service", lambda service, request=None: service)
    monkeypatch.setattr(handlers, "resolve_handler_proxy", lambda *args: ("http://127.0.0.1:9", []))
    monkeypatch.setattr(handlers, "server_account_for", lambda *args: False)
    monkeypatch.setattr(handlers.Services, "load", lambda service: type("SlowProxySvc", (), {}))
    monkeypatch.setattr(handlers, "SessionLogBuffer", RecordingBuffer)
    monkeypatch.setattr(handlers, "create_service_instance", failed_check)

    response = asyncio.run(handlers.session_create_handler({"service": "EXAMPLE", "title_id": "t"}))

    assert seen["thread"] != threading.get_ident()
    assert [r["message"] for r in seen["buffer"].since(0)] == ["checking proxy"]
    assert json.loads(response.body)["error_code"] == "INVALID_PROXY"


@pytest.mark.parametrize(
    "proxy",
    [
        "http://127.0.0.1:8080",
        "http://localhost:3128",
        "socks5://user:pass@10.0.0.5:1080",
        "https://[::1]:8443",
        "http://169.254.169.254",
        "socks5h://192.168.1.1:1080",
        "http://198.18.0.1:8080",
        "http://[2002:7f00:1::]:8080",
        "http://foo.localhost:8080",
        "http://127.0.0.1:8080\\@example.com",
        "http://%31%32%37.0.0.1:8080",
    ],
)
def test_local_proxy_uri_refused_without_server_proxy(proxy):
    with pytest.raises(APIError) as exc_info:
        handlers.resolve_handler_proxy({"proxy": proxy}, "EXAMPLE", request("plainkey"))
    assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY
    assert "pass" not in exc_info.value.message  # credentials never echo back


@pytest.mark.parametrize("key", ["proxykey", "adminkey"])  # adminkey has no serve.users entry
def test_local_proxy_uri_allowed_with_server_proxy_or_admin(monkeypatch, key):
    monkeypatch.setattr(handlers, "initialize_proxy_providers", lambda **_: [])
    proxy, _ = handlers.resolve_handler_proxy({"proxy": "http://127.0.0.1:8080"}, "EXAMPLE", request(key))
    assert proxy == "http://127.0.0.1:8080"


def test_hostname_resolving_to_a_private_address_refused(monkeypatch):
    monkeypatch.setattr(handlers.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("10.0.0.7", 0))])
    with pytest.raises(APIError) as exc_info:
        handlers.resolve_handler_proxy({"proxy": "http://proxy.lan:3128"}, "EXAMPLE", request("plainkey"))
    assert exc_info.value.error_code == APIErrorCode.INVALID_PROXY


def test_local_proxy_uri_ignored_under_no_proxy():
    handlers.resolve_handler_proxy({"proxy": "http://127.0.0.1:8080", "no_proxy": True}, "EXAMPLE", request("plainkey"))


def test_server_built_loopback_proxies_pass_without_server_proxy(monkeypatch):
    """The check reads the client's value, so the loopback URIs the server builds for tunnel: and controld:// pass."""
    monkeypatch.setattr(handlers, "tunnel_proxy", lambda tunnel_id, key: "http://u:p@127.0.0.1:5000")
    proxy, _ = handlers.resolve_handler_proxy({"proxy": "tunnel:abc"}, "EXAMPLE", request("plainkey"))
    assert proxy == "http://u:p@127.0.0.1:5000"

    monkeypatch.setattr(handlers, "resolve_proxy", lambda proxy, providers: "http://127.0.0.1:5001")
    proxy, _ = handlers.resolve_handler_proxy(
        {"proxy": "controld://abc@dns.controld.com"}, "EXAMPLE", request("plainkey")
    )
    assert proxy == "http://127.0.0.1:5001"
