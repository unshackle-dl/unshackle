"""Unit tests for the V2Ray proxy provider.

Covers:
- URI parsing for every supported protocol (vmess / vless / trojan / shadowsocks, SIP002 + legacy)
- Subscription decoding (base64 + plain)
- Config-file extraction (V2Ray/Xray JSON outbounds)
- Country detection heuristics (flag emoji, full names, UK alias, TLD fallback)
- Server selection (by country, by index, by remark, by alias via server_map)
- V2Ray/Xray config building (inbounds + outbound per protocol)
- Provider lifecycle: query deduplication, port allocation, cleanup, error paths

These are pure-Python unit tests — no real network, no real subprocess, no real IP
verification. End-to-end behaviour against a live V2Ray binary lives elsewhere.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from unshackle.core.proxies.proxy import Proxy
from unshackle.core.proxies.v2ray import (V2Ray, V2RayServer, _b64_decode_loose, _detect_country, build_config,
                                          build_outbound, fetch_subscription, load_config_file, parse_server_uri)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64(s: str) -> str:
    """Standard base64 encoding for payload construction in test fixtures."""
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _make_vmess_uri(
    *,
    address: str = "1.2.3.4",
    port: int = 443,
    uuid: str = "b831381d-6324-4d53-ad4f-8cda48b30811",
    remark: str = "🇺🇸 US - Los Angeles",
    network: str = "ws",
    path: str = "/ray",
    host: str = "example.com",
    tls: str = "tls",
    sni: str = "example.com",
    alter_id: int = 0,
) -> str:
    payload = {
        "v": "2",
        "ps": remark,
        "add": address,
        "port": str(port),
        "id": uuid,
        "aid": alter_id,
        "net": network,
        "type": "none",
        "host": host,
        "path": path,
        "tls": tls,
        "sni": sni,
    }
    return "vmess://" + _b64(json.dumps(payload))


def _make_vless_uri(
    *,
    address: str = "1.2.3.4",
    port: int = 443,
    uuid: str = "b831381d-6324-4d53-ad4f-8cda48b30811",
    remark: str = "🇯🇵 JP - Tokyo",
    network: str = "ws",
    path: str = "/vless",
    host: str = "jp.example.com",
    security: str = "tls",
    sni: str = "jp.example.com",
    flow: str = "",
) -> str:
    query = f"encryption=none&security={security}&type={network}&host={host}&path={path}&sni={sni}"
    if flow:
        query += f"&flow={flow}"
    return f"vless://{uuid}@{address}:{port}?{query}#{remark}"


def _make_trojan_uri(
    *,
    address: str = "1.2.3.4",
    port: int = 443,
    password: str = "secretpass",
    remark: str = "🇬🇧 UK - London",
    network: str = "tcp",
    sni: str = "uk.example.com",
) -> str:
    return f"trojan://{password}@{address}:{port}?security=tls&type={network}&sni={sni}#{remark}"


def _make_ss_sip002_uri(
    *,
    address: str = "1.2.3.4",
    port: int = 8388,
    method: str = "aes-256-gcm",
    password: str = "sspassword",
    remark: str = "🇩🇪 DE - Berlin",
) -> str:
    creds = _b64(f"{method}:{password}")
    return f"ss://{creds}@{address}:{port}#{remark}"


def _make_ss_legacy_uri(
    *,
    address: str = "5.6.7.8",
    port: int = 8388,
    method: str = "aes-256-gcm",
    password: str = "sspassword",
    remark: str = "DE Server",
) -> str:
    payload = _b64(f"{method}:{password}@{address}:{port}")
    return f"ss://{payload}#{remark}"


# ---------------------------------------------------------------------------
# Proxy contract
# ---------------------------------------------------------------------------


def test_v2ray_is_a_proxy_subclass():
    assert issubclass(V2Ray, Proxy)


def test_v2ray_repr_with_no_servers():
    provider = V2Ray(servers=[])
    assert repr(provider) == "0 Countries (0 Servers)"


def test_v2ray_repr_counts_unique_countries_and_servers():
    provider = V2Ray(
        servers=[
            _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1"),
            _make_vmess_uri(remark="🇺🇸 US-East", address="2.2.2.2"),
            _make_vmess_uri(remark="🇯🇵 JP", address="3.3.3.3"),
        ]
    )
    assert repr(provider) == "2 Countries (3 Servers)"


# ---------------------------------------------------------------------------
# URI parsing — VMess
# ---------------------------------------------------------------------------


def test_parse_vmess_basic_fields():
    server = parse_server_uri(_make_vmess_uri())
    assert server is not None
    assert server.protocol == "vmess"
    assert server.address == "1.2.3.4"
    assert server.port == 443
    assert server.settings["id"] == "b831381d-6324-4d53-ad4f-8cda48b30811"
    assert server.settings["alterId"] == 0
    assert server.network == "ws"
    assert server.stream["security"] == "tls"
    assert server.stream["network"] == "ws"
    assert server.stream["wsSettings"]["path"] == "/ray"
    assert server.stream["wsSettings"]["host"] == "example.com"
    assert server.stream["tlsSettings"]["serverName"] == "example.com"
    assert server.country == "us"


def test_parse_vmess_without_tls():
    uri = _make_vmess_uri(tls="", sni="", host="")
    server = parse_server_uri(uri)
    assert server is not None
    assert server.stream["security"] == "none"


def test_parse_vmess_grpc_network():
    uri = _make_vmess_uri(network="grpc", path="GunService", tls="tls")
    server = parse_server_uri(uri)
    assert server is not None
    assert server.stream["grpcSettings"]["serviceName"] == "GunService"


def test_parse_vmess_invalid_base64_returns_none():
    server = parse_server_uri("vmess://!!!not-base64!!!")
    assert server is None


def test_parse_vmess_missing_address_returns_none():
    payload = {"v": "2", "ps": "x", "add": "", "port": "443", "id": "x", "aid": 0, "net": "tcp"}
    server = parse_server_uri("vmess://" + _b64(json.dumps(payload)))
    assert server is None


# ---------------------------------------------------------------------------
# URI parsing — VLESS
# ---------------------------------------------------------------------------


def test_parse_vless_basic_fields():
    server = parse_server_uri(_make_vless_uri())
    assert server is not None
    assert server.protocol == "vless"
    assert server.address == "1.2.3.4"
    assert server.port == 443
    assert server.settings["id"] == "b831381d-6324-4d53-ad4f-8cda48b30811"
    assert server.settings["encryption"] == "none"
    assert server.network == "ws"
    assert server.stream["security"] == "tls"
    assert server.stream["wsSettings"]["path"] == "/vless"
    assert server.stream["tlsSettings"]["serverName"] == "jp.example.com"
    assert server.country == "jp"


def test_parse_vless_reality():
    uri = (
        "vless://b831381d-6324-4d53-ad4f-8cda48b30811@1.2.3.4:443?"
        "encryption=none&security=reality&type=tcp&sni=www.google.com&pbk=ABC&sid=def&fp=chrome"
        "#Reality"
    )
    server = parse_server_uri(uri)
    assert server is not None
    assert server.stream["security"] == "reality"
    assert server.stream["realitySettings"]["publicKey"] == "ABC"
    assert server.stream["realitySettings"]["shortId"] == "def"
    assert server.stream["realitySettings"]["fingerprint"] == "chrome"


def test_parse_vless_with_flow_xtls_vision():
    uri = _make_vless_uri(network="tcp", flow="xtls-rprx-vision")
    server = parse_server_uri(uri)
    assert server is not None
    assert server.settings["flow"] == "xtls-rprx-vision"


def test_parse_vless_missing_user_returns_none():
    server = parse_server_uri("vless://@1.2.3.4:443")
    assert server is None


# ---------------------------------------------------------------------------
# URI parsing — Trojan
# ---------------------------------------------------------------------------


def test_parse_trojan_basic_fields():
    server = parse_server_uri(_make_trojan_uri())
    assert server is not None
    assert server.protocol == "trojan"
    assert server.address == "1.2.3.4"
    assert server.port == 443
    assert server.settings["password"] == "secretpass"
    assert server.stream["security"] == "tls"
    assert server.stream["tlsSettings"]["serverName"] == "uk.example.com"
    assert server.country == "gb"  # UK alias normalised to GB


def test_parse_trojan_default_port_is_443():
    uri = "trojan://pass@host.example#Test"
    server = parse_server_uri(uri)
    assert server is not None
    assert server.port == 443


def test_parse_trojan_ws_network():
    uri = _make_trojan_uri(network="ws", sni="uk.example.com") + ""  # base uri already correct
    # add path/host via query string
    uri = uri.split("?")[0] + "?security=tls&type=ws&path=/trojan&host=uk.example.com&sni=uk.example.com#WS"
    server = parse_server_uri(uri)
    assert server is not None
    assert server.network == "ws"
    assert server.stream["wsSettings"]["path"] == "/trojan"


# ---------------------------------------------------------------------------
# URI parsing — Shadowsocks (SIP002 + legacy)
# ---------------------------------------------------------------------------


def test_parse_shadowsocks_sip002():
    server = parse_server_uri(_make_ss_sip002_uri())
    assert server is not None
    assert server.protocol == "shadowsocks"
    assert server.address == "1.2.3.4"
    assert server.port == 8388
    assert server.settings["method"] == "aes-256-gcm"
    assert server.settings["password"] == "sspassword"
    assert server.country == "de"


def test_parse_shadowsocks_legacy():
    server = parse_server_uri(_make_ss_legacy_uri())
    assert server is not None
    assert server.protocol == "shadowsocks"
    assert server.address == "5.6.7.8"
    assert server.port == 8388
    assert server.settings["method"] == "aes-256-gcm"
    assert server.settings["password"] == "sspassword"


def test_parse_shadowsocks_plaintext_creds():
    uri = "ss://aes-256-gcm:plaintextpass@1.2.3.4:8388#Plain"
    server = parse_server_uri(uri)
    assert server is not None
    assert server.settings["method"] == "aes-256-gcm"
    assert server.settings["password"] == "plaintextpass"


def test_parse_shadowsocks_invalid_returns_none():
    assert parse_server_uri("ss://") is None
    assert parse_server_uri("ss://!!!invalid") is None


# ---------------------------------------------------------------------------
# URI parsing — misc
# ---------------------------------------------------------------------------


def test_parse_unknown_scheme_returns_none():
    assert parse_server_uri("https://example.com") is None
    assert parse_server_uri("") is None
    assert parse_server_uri("garbage") is None


def test_parse_uri_strips_whitespace():
    uri = "  " + _make_vmess_uri() + "  "
    server = parse_server_uri(uri)
    assert server is not None


# ---------------------------------------------------------------------------
# Country detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "remark,expected",
    [
        ("🇺🇸 US - Los Angeles", "us"),
        ("JP - Tokyo", "jp"),
        ("(DE) Berlin", "de"),
        ("United Kingdom - London", "gb"),
        ("Canada East", "ca"),
        ("UK Server 1", "gb"),  # alias
        ("GB Server 1", "gb"),
        ("random-server.example.com", None),  # no country hint
    ],
)
def test_detect_country_from_remark(remark, expected):
    assert _detect_country(remark) == expected


def test_detect_country_tld_fallback():
    assert _detect_country("", "server.de") == "de"
    assert _detect_country("", "server.example.com") is None  # .com is not a country TLD


# ---------------------------------------------------------------------------
# Base64 decoding (lenient)
# ---------------------------------------------------------------------------


def test_b64_decode_standard():
    assert _b64_decode_loose(_b64("hello")) == "hello"


def test_b64_decode_urlsafe_no_padding():
    encoded = base64.urlsafe_b64encode(b"hello").decode("ascii").rstrip("=")
    assert _b64_decode_loose(encoded) == "hello"


def test_b64_decode_invalid_raises():
    with pytest.raises(ValueError):
        _b64_decode_loose("!!!not base64!!!")


# ---------------------------------------------------------------------------
# Subscription fetching
# ---------------------------------------------------------------------------


def _fake_response(*, text: str, status: int = 200):
    response = MagicMock()
    response.status_code = status
    response.text = text
    response.raise_for_status = MagicMock()
    if status >= 400:
        response.raise_for_status.side_effect = Exception(f"HTTP {status}")
    return response


def test_fetch_subscription_plain_uris():
    body = "\n".join(
        [
            _make_vmess_uri(remark="🇺🇸 US"),
            _make_vless_uri(remark="🇯🇵 JP"),
            "# this is a comment",
            "",
            "garbage-line",  # should be skipped, not abort
        ]
    )
    with patch("unshackle.core.proxies.v2ray.requests.get", return_value=_fake_response(text=body)) as mock_get:
        servers = fetch_subscription("https://sub.example/list")
    assert len(servers) == 2
    assert {s.country for s in servers} == {"us", "jp"}
    mock_get.assert_called_once()
    # Verify the User-Agent header is set (browser-like, to dodge naive UA filters).
    _, kwargs = mock_get.call_args
    assert "User-Agent" in kwargs["headers"]


def test_fetch_subscription_base64_encoded():
    body = "\n".join([_make_vmess_uri(remark="🇩🇪 DE"), _make_trojan_uri(remark="🇬🇧 UK")])
    encoded = _b64(body)
    with patch("unshackle.core.proxies.v2ray.requests.get", return_value=_fake_response(text=encoded)):
        servers = fetch_subscription("https://sub.example/b64")
    assert len(servers) == 2
    assert {s.country for s in servers} == {"de", "gb"}


def test_fetch_subscription_empty_body():
    with patch("unshackle.core.proxies.v2ray.requests.get", return_value=_fake_response(text="")):
        servers = fetch_subscription("https://sub.example/empty")
    assert servers == []


def test_fetch_subscription_propagates_http_errors():
    with patch("unshackle.core.proxies.v2ray.requests.get", return_value=_fake_response(text="", status=500)):
        with pytest.raises(Exception):
            fetch_subscription("https://sub.example/err")


# ---------------------------------------------------------------------------
# Config file loading
# ---------------------------------------------------------------------------


def test_load_config_file_extracts_outbounds(tmp_path: Path):
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [],
        "outbounds": [
            {
                "tag": "us-proxy",
                "protocol": "vmess",
                "settings": {
                    "vnext": [
                        {
                            "address": "1.2.3.4",
                            "port": 443,
                            "users": [{"id": "abc", "alterId": 0, "security": "auto"}],
                        }
                    ]
                },
                "streamSettings": {"network": "ws", "security": "tls", "tlsSettings": {"serverName": "x.com"}},
            },
            {
                "tag": "direct",
                "protocol": "freedom",
            },
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    servers = load_config_file(path)
    assert len(servers) == 1
    assert servers[0].protocol == "vmess"
    assert servers[0].address == "1.2.3.4"
    assert servers[0].port == 443
    assert servers[0].remark == "us-proxy"


def test_load_config_file_handles_each_protocol(tmp_path: Path):
    config = {
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {"vnext": [{"address": "h1", "port": 443, "users": [{"id": "u1"}]}]},
                "streamSettings": {"network": "tcp"},
            },
            {
                "protocol": "trojan",
                "settings": {"servers": [{"address": "h2", "port": 443, "password": "p2"}]},
                "streamSettings": {},
            },
            {
                "protocol": "shadowsocks",
                "settings": {"servers": [{"address": "h3", "port": 8388, "method": "aes-256-gcm", "password": "p3"}]},
                "streamSettings": {},
            },
            {"protocol": "freedom"},
            {"protocol": "blackhole"},
        ]
    }
    path = tmp_path / "c.json"
    path.write_text(json.dumps(config))
    servers = load_config_file(path)
    assert [s.protocol for s in servers] == ["vless", "trojan", "shadowsocks"]


def test_load_config_file_missing_outbounds_raises(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="no 'outbounds' list"):
        load_config_file(path)


def test_load_config_file_invalid_json_raises(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    with pytest.raises(ValueError, match="could not read config file"):
        load_config_file(path)


# ---------------------------------------------------------------------------
# Config building
# ---------------------------------------------------------------------------


def test_build_outbound_vmess_shape():
    server = parse_server_uri(_make_vmess_uri())
    outbound = build_outbound(server, tag="proxy")
    assert outbound["tag"] == "proxy"
    assert outbound["protocol"] == "vmess"
    assert outbound["settings"]["vnext"][0]["address"] == "1.2.3.4"
    assert outbound["settings"]["vnext"][0]["port"] == 443
    assert outbound["streamSettings"]["network"] == "ws"
    assert outbound["streamSettings"]["security"] == "tls"


def test_build_outbound_vless_with_flow():
    server = parse_server_uri(_make_vless_uri(network="tcp", flow="xtls-rprx-vision"))
    outbound = build_outbound(server)
    user = outbound["settings"]["vnext"][0]["users"][0]
    assert user["flow"] == "xtls-rprx-vision"


def test_build_outbound_trojan_shape():
    server = parse_server_uri(_make_trojan_uri())
    outbound = build_outbound(server)
    assert outbound["protocol"] == "trojan"
    assert outbound["settings"]["servers"][0]["password"] == "secretpass"


def test_build_outbound_shadowsocks_shape():
    server = parse_server_uri(_make_ss_sip002_uri())
    outbound = build_outbound(server)
    assert outbound["protocol"] == "shadowsocks"
    assert outbound["settings"]["servers"][0]["method"] == "aes-256-gcm"


def test_build_outbound_unsupported_protocol_raises():
    server = V2RayServer(protocol="bogus", address="x", port=1)
    with pytest.raises(ValueError, match="Unsupported V2Ray protocol"):
        build_outbound(server)


def test_build_config_has_socks_and_http_inbounds():
    server = parse_server_uri(_make_vmess_uri())
    config = build_config(server, socks_port=1080, http_port=1081)
    inbound_tags = [i["tag"] for i in config["inbounds"]]
    assert "socks-in" in inbound_tags
    assert "http-in" in inbound_tags
    socks = next(i for i in config["inbounds"] if i["tag"] == "socks-in")
    assert socks["listen"] == "127.0.0.1"
    assert socks["port"] == 1080
    assert socks["protocol"] == "socks"
    # Outbound section has proxy + direct + block
    outbound_tags = [o["tag"] for o in config["outbounds"]]
    assert outbound_tags == ["proxy", "direct", "block"]
    # Routing bypasses private IPs to direct
    assert any(
        rule.get("outboundTag") == "direct" and "geoip:private" in rule.get("ip", [])
        for rule in config["routing"]["rules"]
    )


def test_build_config_without_http_inbound():
    server = parse_server_uri(_make_vmess_uri())
    config = build_config(server, socks_port=1080, http_port=None)
    assert len(config["inbounds"]) == 1
    assert config["inbounds"][0]["tag"] == "socks-in"


# ---------------------------------------------------------------------------
# Provider: construction + server loading
# ---------------------------------------------------------------------------


def test_provider_loads_inline_servers():
    provider = V2Ray(
        servers=[
            _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1"),
            _make_vless_uri(remark="🇯🇵 JP", address="2.2.2.2"),
        ]
    )
    assert len(provider.servers) == 2
    assert {s.country for s in provider.servers} == {"us", "jp"}


def test_provider_dedupes_identical_servers():
    uri = _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")
    provider = V2Ray(servers=[uri, uri, uri])
    assert len(provider.servers) == 1


def test_provider_accepts_pre_parsed_dicts():
    server_dict = {
        "protocol": "vmess",
        "address": "9.9.9.9",
        "port": 443,
        "remark": "🇫🇷 FR",
        "settings": {"id": "x", "alterId": 0, "security": "auto"},
        "network": "tcp",
        "stream": {"network": "tcp", "security": "none"},
        "country": "fr",
    }
    provider = V2Ray(servers=[server_dict])
    assert len(provider.servers) == 1
    assert provider.servers[0].country == "fr"


def test_provider_loads_subscription(tmp_path: Path):
    body = _make_vmess_uri(remark="🇺🇸 US") + "\n" + _make_vless_uri(remark="🇯🇵 JP")
    response = _fake_response(text=body)
    with patch("unshackle.core.proxies.v2ray.requests.get", return_value=response):
        provider = V2Ray(subscription_url="https://sub.example/list")
    assert len(provider.servers) == 2


def test_provider_loads_multiple_subscriptions():
    body1 = _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")
    body2 = _make_vless_uri(remark="🇯🇵 JP", address="2.2.2.2")
    responses = [_fake_response(text=body1), _fake_response(text=body2)]
    with patch("unshackle.core.proxies.v2ray.requests.get", side_effect=responses):
        provider = V2Ray(subscription_url=["https://sub.example/1", "https://sub.example/2"])
    assert len(provider.servers) == 2


def test_provider_subscription_failure_does_not_abort_other_sources():
    body = _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")
    good_response = _fake_response(text=body)
    with patch(
        "unshackle.core.proxies.v2ray.requests.get",
        side_effect=[Exception("network down"), good_response],
    ):
        provider = V2Ray(
            subscription_url=["https://sub.example/broken", "https://sub.example/ok"]
        )
    # The failed subscription is logged + skipped; the second one's server still loaded.
    assert len(provider.servers) == 1


def test_provider_loads_config_file(tmp_path: Path):
    config = {
        "outbounds": [
            {
                "tag": "us-1",
                "protocol": "vmess",
                "settings": {
                    "vnext": [
                        {
                            "address": "1.2.3.4",
                            "port": 443,
                            "users": [{"id": "abc", "alterId": 0, "security": "auto"}],
                        }
                    ]
                },
                "streamSettings": {"network": "ws", "security": "tls"},
            }
        ]
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    provider = V2Ray(config_path=path)
    assert len(provider.servers) == 1
    assert provider.servers[0].address == "1.2.3.4"


def test_provider_invalid_proxy_scheme_raises():
    with pytest.raises(ValueError, match="proxy_scheme must be one of"):
        V2Ray(servers=[], proxy_scheme="bogus")


def test_provider_explicit_binary_must_exist():
    with pytest.raises(FileNotFoundError, match="configured binary not found"):
        V2Ray(servers=[], binary="/no/such/binary")


# ---------------------------------------------------------------------------
# Provider: server selection
# ---------------------------------------------------------------------------


def _make_provider_with_servers() -> V2Ray:
    return V2Ray(
        servers=[
            _make_vmess_uri(remark="🇺🇸 US - Los Angeles", address="1.1.1.1"),
            _make_vmess_uri(remark="🇺🇸 US - New York", address="2.2.2.2"),
            _make_vless_uri(remark="🇯🇵 JP - Tokyo", address="3.3.3.3"),
            _make_trojan_uri(remark="🇬🇧 UK - London", address="4.4.4.4"),
            _make_ss_sip002_uri(remark="🇩🇪 DE - Berlin", address="5.5.5.5"),
        ]
    )


def test_select_by_country_returns_first_match():
    provider = _make_provider_with_servers()
    server = provider._select_server("us")
    assert server is not None
    assert server.country == "us"
    assert server.address == "1.1.1.1"  # first US server


def test_select_by_country_and_index():
    provider = _make_provider_with_servers()
    server = provider._select_server("us:2")
    assert server is not None
    assert server.address == "2.2.2.2"  # second US server


def test_select_by_index_only_returns_first_overall():
    provider = _make_provider_with_servers()
    server = provider._select_server("1")
    assert server is not None
    assert server.address == "1.1.1.1"


def test_select_by_remark_substring():
    provider = _make_provider_with_servers()
    server = provider._select_server("tokyo")
    assert server is not None
    assert server.address == "3.3.3.3"


def test_select_by_country_and_remark():
    provider = _make_provider_with_servers()
    server = provider._select_server("us:new york")
    assert server is not None
    assert server.address == "2.2.2.2"


def test_select_unknown_country_returns_none():
    provider = _make_provider_with_servers()
    assert provider._select_server("zz") is None


def test_select_index_out_of_range_returns_none():
    provider = _make_provider_with_servers()
    assert provider._select_server("us:99") is None


def test_select_uses_server_map_alias():
    provider = _make_provider_with_servers()
    provider.server_map = {"stream-us": "us:2"}
    server = provider._select_server("stream-us")
    assert server is not None
    assert server.address == "2.2.2.2"


def test_select_handles_uk_alias():
    provider = _make_provider_with_servers()
    server = provider._select_server("gb")
    assert server is not None
    assert server.address == "4.4.4.4"


def test_select_with_full_country_name():
    provider = _make_provider_with_servers()
    server = provider._select_server("japan")
    assert server is not None
    assert server.address == "3.3.3.3"


# ---------------------------------------------------------------------------
# Provider: get_proxy lifecycle (mocked subprocess)
# ---------------------------------------------------------------------------


def _make_mock_process():
    """A MagicMock that looks like a running subprocess.Popen."""
    process = MagicMock()
    process.poll.return_value = None  # None == still running
    process.pid = 12345
    process.stderr = MagicMock()
    process.stderr.read1.return_value = b""
    return process


def test_get_proxy_no_binary_raises_environment_error():
    provider = V2Ray(servers=[_make_vmess_uri()])
    provider.binary = None
    with pytest.raises(EnvironmentError, match="V2Ray/Xray binary not found"):
        provider.get_proxy("us")


def test_get_proxy_no_servers_raises_value_error():
    provider = V2Ray(servers=[])
    provider.binary = Path("/fake/xray")  # bypass binary check
    with pytest.raises(ValueError, match="no servers configured"):
        provider.get_proxy("us")


def test_get_proxy_empty_query_raises_value_error():
    provider = V2Ray(servers=[_make_vmess_uri()])
    provider.binary = Path("/fake/xray")
    with pytest.raises(ValueError, match="empty query"):
        provider.get_proxy("")


def test_get_proxy_unknown_query_returns_none():
    provider = V2Ray(servers=[_make_vmess_uri()])
    provider.binary = Path("/fake/xray")
    # Don't actually spawn — patch _spawn_process so we never need a real binary.
    with patch.object(V2Ray, "_spawn_process"), \
         patch.object(V2Ray, "_wait_for_ready", return_value=True), \
         patch.object(V2Ray, "_verify_proxy"), \
         patch.object(V2Ray, "_is_port_in_use", return_value=False):
        # No server matches "zz" -> _select_server returns None -> get_proxy returns None.
        assert provider.get_proxy("zz") is None


def _patch_lifecycle(provider: V2Ray, *, alive: bool = True, ready: bool = True):
    """Patch the subprocess lifecycle so get_proxy works without a real binary."""
    process = _make_mock_process()
    if not alive:
        process.poll.return_value = 1

    def fake_spawn(config, socks_port, http_port, server):
        return {
            "process": process,
            "config_path": Path(f"/tmp/fake-{socks_port}.json"),
            "socks_port": socks_port,
            "http_port": http_port,
            "pid": 12345,
            "started_at": 0.0,
            "verified": False,
            "stdout": MagicMock(),
            "stderr": MagicMock(),
        }

    return (
        patch.object(V2Ray, "_spawn_process", side_effect=fake_spawn),
        patch.object(V2Ray, "_wait_for_ready", return_value=ready),
        patch.object(V2Ray, "_verify_proxy"),
        patch.object(V2Ray, "_is_process_alive", return_value=alive),
        patch.object(V2Ray, "_is_port_in_use", return_value=False),
        patch.object(V2Ray, "_kill_process"),
    )


def test_get_proxy_returns_socks5_uri_by_default():
    provider = V2Ray(servers=[_make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")])
    provider.binary = Path("/fake/xray")
    patches = _patch_lifecycle(provider)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        uri = provider.get_proxy("us")
    assert uri is not None
    assert uri.startswith("socks5://127.0.0.1:")
    port = int(uri.rsplit(":", 1)[1])
    assert port >= 11080


def test_get_proxy_returns_http_uri_when_configured():
    provider = V2Ray(
        servers=[_make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")],
        proxy_scheme="http",
    )
    provider.binary = Path("/fake/xray")
    patches = _patch_lifecycle(provider)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        uri = provider.get_proxy("us")
    assert uri is not None
    assert uri.startswith("http://127.0.0.1:")


def test_get_proxy_reuses_existing_subprocess_for_same_query():
    provider = V2Ray(servers=[_make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")])
    provider.binary = Path("/fake/xray")
    patches = _patch_lifecycle(provider)
    with patches[0] as spawn_patch, patches[1], patches[2], patches[3], patches[4], patches[5]:
        uri1 = provider.get_proxy("us")
        uri2 = provider.get_proxy("us")
    assert uri1 == uri2
    spawn_patch.assert_called_once()  # second call reuses the existing subprocess


def test_get_proxy_startup_failure_raises_runtime_error():
    provider = V2Ray(servers=[_make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")])
    provider.binary = Path("/fake/xray")
    patches = _patch_lifecycle(provider, ready=False)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as kill_patch:
        with pytest.raises(RuntimeError, match="subprocess did not become ready"):
            provider.get_proxy("us")
    # The failed subprocess is killed and removed from the active dict.
    kill_patch.assert_called_once()


def test_get_proxy_socks5h_scheme_supported():
    provider = V2Ray(
        servers=[_make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")],
        proxy_scheme="socks5h",
    )
    provider.binary = Path("/fake/xray")
    patches = _patch_lifecycle(provider)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        uri = provider.get_proxy("us")
    assert uri.startswith("socks5h://127.0.0.1:")


# ---------------------------------------------------------------------------
# Provider: port allocation + cleanup
# ---------------------------------------------------------------------------


def test_allocate_ports_returns_distinct_pair():
    provider = V2Ray(servers=[])
    with patch.object(V2Ray, "_is_port_in_use", return_value=False):
        socks, http = provider._allocate_ports()
    assert socks == 11080
    assert http == 11081
    assert socks != http


def test_allocate_ports_skips_used_ports():
    provider = V2Ray(servers=[])
    provider._active = {
        "k1": {"socks_port": 11080, "http_port": 11081},
    }
    used_ports = {11080, 11081}

    def fake_in_use(port):
        return port in used_ports

    with patch.object(V2Ray, "_is_port_in_use", side_effect=fake_in_use):
        socks, http = provider._allocate_ports()
    assert socks not in used_ports
    assert http not in used_ports
    assert http == socks + 1


def test_cleanup_kills_every_active_subprocess():
    provider = V2Ray(servers=[])
    provider._active = {
        "us": {"process": _make_mock_process(), "config_path": None, "socks_port": 1, "http_port": 2},
        "jp": {"process": _make_mock_process(), "config_path": None, "socks_port": 3, "http_port": 4},
    }
    with patch.object(V2Ray, "_kill_process") as kill_patch:
        provider.cleanup()
    assert kill_patch.call_count == 2
    assert provider._active == {}


def test_cleanup_is_idempotent():
    provider = V2Ray(servers=[])
    with patch.object(V2Ray, "_kill_process"):
        provider.cleanup()
        provider.cleanup()  # second call is a no-op
    assert provider._active == {}


def test_kill_process_terminates_and_unlinks_config(tmp_path: Path):
    provider = V2Ray(servers=[])
    process = _make_mock_process()
    config_path = tmp_path / "config-12345.json"
    config_path.write_text('{"sensitive": "creds"}')
    info = {
        "process": process,
        "config_path": config_path,
        "socks_port": 12345,
        "http_port": 12346,
        "pid": 12345,
    }
    provider._kill_process(info)
    process.terminate.assert_called_once()
    process.wait.assert_called()
    assert not config_path.exists()


def test_kill_process_kills_when_terminate_hangs():
    provider = V2Ray(servers=[])
    process = _make_mock_process()
    process.wait.side_effect = [subprocess.TimeoutExpired("x", 5), None]
    info = {"process": process, "config_path": None, "socks_port": 1, "http_port": 2, "pid": 99}
    provider._kill_process(info)
    process.kill.assert_called_once()


# ---------------------------------------------------------------------------
# Provider: integration with resolve_proxy
# ---------------------------------------------------------------------------


def test_v2ray_registered_in_resolve_initialize():
    """V2Ray must be instantiated when ``proxy_providers.v2ray`` is set in config."""
    import unshackle.core.proxies.v2ray as v2ray_module
    from unshackle.core.proxies import resolve

    captured_kwargs: list = []

    def fake_v2ray_init(**kwargs):
        captured_kwargs.append(kwargs)
        instance = MagicMock(name="V2Ray-instance")
        instance.__class__.__name__ = "V2Ray"
        return instance

    main_config = MagicMock()
    main_config.proxy_providers = {
        "v2ray": {"subscription_url": "https://sub.example/list"},
    }
    with patch.object(v2ray_module, "V2Ray", side_effect=fake_v2ray_init) as V2RayMock, \
         patch("unshackle.core.binaries.HolaProxy", None), \
         patch("unshackle.core.config.config", main_config):
        # Force re-execution of the function-body imports by clearing them, then
        # call initialize_proxy_providers. Easier: just call the function directly
        # since the imports inside the body re-resolve at call time.
        resolve.initialize_proxy_providers()

    assert V2RayMock.called, "V2Ray constructor was never invoked"
    assert V2RayMock.call_args.kwargs == {"subscription_url": "https://sub.example/list"}


# ---------------------------------------------------------------------------
# Provider: basic-style countries map (per-country URI assignment)
# ---------------------------------------------------------------------------


def test_countries_map_loads_single_uri_per_country():
    provider = V2Ray(
        countries={
            "us": _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1"),
            "jp": _make_vless_uri(remark="🇯🇵 JP", address="2.2.2.2"),
        }
    )
    assert set(provider.country_servers.keys()) == {"us", "jp"}
    assert len(provider.country_servers["us"]) == 1
    assert provider.country_servers["us"][0].address == "1.1.1.1"
    assert provider.country_servers["jp"][0].address == "2.2.2.2"


def test_countries_map_loads_list_of_uris_per_country():
    provider = V2Ray(
        countries={
            "us": [
                _make_vmess_uri(remark="🇺🇸 US-1", address="1.1.1.1"),
                _make_vmess_uri(remark="🇺🇸 US-2", address="2.2.2.2"),
                _make_vless_uri(remark="🇺🇸 US-3", address="3.3.3.3"),
            ],
        }
    )
    assert len(provider.country_servers["us"]) == 3
    addresses = [s.address for s in provider.country_servers["us"]]
    assert addresses == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]


def test_countries_map_skips_unparseable_uris():
    provider = V2Ray(
        countries={
            "us": [
                _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1"),
                "garbage-not-a-uri",
                "vmess://!!!invalid-base64!!!",
                _make_vless_uri(remark="🇺🇸 US-2", address="2.2.2.2"),
            ],
        }
    )
    # Only the two valid URIs loaded; bad ones skipped with a warning.
    assert len(provider.country_servers["us"]) == 2
    assert provider.country_servers["us"][0].address == "1.1.1.1"
    assert provider.country_servers["us"][1].address == "2.2.2.2"


def test_countries_map_rejects_non_dict():
    with pytest.raises(TypeError, match="'countries' must be a dict"):
        V2Ray(countries="not a dict")  # type: ignore[arg-type]


def test_countries_map_skips_non_string_non_list_values():
    provider = V2Ray(
        countries={
            "us": _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1"),
            "bad": 12345,  # unsupported type — skipped with warning
            "jp": _make_vless_uri(remark="🇯🇵 JP", address="2.2.2.2"),
        }
    )
    assert set(provider.country_servers.keys()) == {"us", "jp"}


def test_countries_map_forces_country_to_match_key():
    """A URI whose auto-detected country differs from the map key gets overridden."""
    # This vmess URI's remark says "🇺🇸 US" but we assign it to "jp" in the map.
    provider = V2Ray(
        countries={
            "jp": _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1"),
        }
    )
    assert provider.country_servers["jp"][0].country == "jp"


def test_countries_map_supports_arbitrary_alias_keys():
    """Keys don't have to be country codes — any alias works."""
    provider = V2Ray(
        countries={
            "stream-us": _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1"),
            "home": _make_vless_uri(remark="🇯🇵 JP", address="2.2.2.2"),
        }
    )
    assert set(provider.country_servers.keys()) == {"stream-us", "home"}


def test_select_from_countries_map_by_country():
    provider = V2Ray(
        countries={
            "us": _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1"),
            "jp": _make_vless_uri(remark="🇯🇵 JP", address="2.2.2.2"),
        }
    )
    server = provider._select_server("us")
    assert server is not None
    assert server.address == "1.1.1.1"


def test_select_from_countries_map_by_index():
    provider = V2Ray(
        countries={
            "us": [
                _make_vmess_uri(remark="🇺🇸 US-1", address="1.1.1.1"),
                _make_vmess_uri(remark="🇺🇸 US-2", address="2.2.2.2"),
            ],
        }
    )
    server = provider._select_server("us:2")
    assert server is not None
    assert server.address == "2.2.2.2"


def test_select_from_countries_map_by_alias():
    provider = V2Ray(
        countries={
            "stream-us": _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1"),
        }
    )
    server = provider._select_server("stream-us")
    assert server is not None
    assert server.address == "1.1.1.1"


def test_countries_map_takes_priority_over_flat_list():
    """If both ``servers`` and ``countries`` are configured, the country map wins."""
    provider = V2Ray(
        servers=[_make_vmess_uri(remark="🇺🇸 US-from-flat-list", address="9.9.9.9")],
        countries={
            "us": _make_vmess_uri(remark="🇺🇸 US-from-map", address="1.1.1.1"),
        },
    )
    server = provider._select_server("us")
    assert server is not None
    assert server.address == "1.1.1.1"  # from the map, not the flat list


def test_countries_map_repr_counts_both_pools():
    provider = V2Ray(
        servers=[_make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")],
        countries={
            "jp": _make_vless_uri(remark="🇯🇵 JP", address="2.2.2.2"),
            "de": _make_ss_sip002_uri(remark="🇩🇪 DE", address="3.3.3.3"),
        },
    )
    # 3 countries (us from flat list + jp + de from map), 3 servers total.
    assert repr(provider) == "3 Countries (3 Servers)"


def test_get_proxy_with_countries_map_returns_socks5_uri():
    provider = V2Ray(
        countries={
            "us": _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1"),
        }
    )
    provider.binary = Path("/fake/xray")
    patches = _patch_lifecycle(provider)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        uri = provider.get_proxy("us")
    assert uri is not None
    assert uri.startswith("socks5://127.0.0.1:")


# ---------------------------------------------------------------------------
# Provider: direct-URI mode (--proxy v2ray:vmess://...)
# ---------------------------------------------------------------------------


def test_is_v2ray_uri_detects_all_schemes():
    from unshackle.core.proxies.v2ray import _is_v2ray_uri

    assert _is_v2ray_uri("vmess://abc")
    assert _is_v2ray_uri("vless://abc@host:443")
    assert _is_v2ray_uri("trojan://pass@host:443")
    assert _is_v2ray_uri("ss://abc")
    assert _is_v2ray_uri("  vmess://abc  ")  # whitespace is stripped
    assert not _is_v2ray_uri("us")
    assert not _is_v2ray_uri("us:1")
    assert not _is_v2ray_uri("https://example.com")
    assert not _is_v2ray_uri("socks5://127.0.0.1:1080")
    assert not _is_v2ray_uri("")


def test_get_proxy_with_direct_vmess_uri_spawns_subprocess():
    """``--proxy v2ray:vmess://...`` spawns a one-shot subprocess for that URI."""
    provider = V2Ray(servers=[])  # no preloaded servers — direct-URI mode still works
    provider.binary = Path("/fake/xray")
    uri = _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")
    patches = _patch_lifecycle(provider)
    with patches[0] as spawn_patch, patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = provider.get_proxy(uri)
    assert result is not None
    assert result.startswith("socks5://127.0.0.1:")
    spawn_patch.assert_called_once()
    # Verify the spawned server matches the URI we passed in.
    spawned_server = spawn_patch.call_args.args[3]  # 4th positional arg is `server`
    assert spawned_server.protocol == "vmess"
    assert spawned_server.address == "1.1.1.1"


def test_get_proxy_with_direct_vless_uri_spawns_subprocess():
    provider = V2Ray(servers=[])
    provider.binary = Path("/fake/xray")
    uri = _make_vless_uri(remark="🇯🇵 JP", address="2.2.2.2")
    patches = _patch_lifecycle(provider)
    with patches[0] as spawn_patch, patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = provider.get_proxy(uri)
    assert result is not None
    spawned_server = spawn_patch.call_args.args[3]
    assert spawned_server.protocol == "vless"
    assert spawned_server.address == "2.2.2.2"


def test_get_proxy_with_direct_trojan_uri_spawns_subprocess():
    provider = V2Ray(servers=[])
    provider.binary = Path("/fake/xray")
    uri = _make_trojan_uri(remark="🇬🇧 UK", address="3.3.3.3")
    patches = _patch_lifecycle(provider)
    with patches[0] as spawn_patch, patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = provider.get_proxy(uri)
    assert result is not None
    spawned_server = spawn_patch.call_args.args[3]
    assert spawned_server.protocol == "trojan"
    assert spawned_server.address == "3.3.3.3"


def test_get_proxy_with_direct_ss_uri_spawns_subprocess():
    provider = V2Ray(servers=[])
    provider.binary = Path("/fake/xray")
    uri = _make_ss_sip002_uri(remark="🇩🇪 DE", address="4.4.4.4")
    patches = _patch_lifecycle(provider)
    with patches[0] as spawn_patch, patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = provider.get_proxy(uri)
    assert result is not None
    spawned_server = spawn_patch.call_args.args[3]
    assert spawned_server.protocol == "shadowsocks"
    assert spawned_server.address == "4.4.4.4"


def test_get_proxy_direct_uri_works_without_any_servers_configured():
    """Direct-URI mode doesn't require subscription_url / config_path / servers."""
    provider = V2Ray()  # completely empty config
    provider.binary = Path("/fake/xray")
    uri = _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")
    patches = _patch_lifecycle(provider)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = provider.get_proxy(uri)
    assert result is not None


def test_get_proxy_direct_uri_reuses_subprocess_for_same_uri():
    """Passing the same URI twice reuses the existing subprocess."""
    provider = V2Ray(servers=[])
    provider.binary = Path("/fake/xray")
    uri = _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")
    patches = _patch_lifecycle(provider)
    with patches[0] as spawn_patch, patches[1], patches[2], patches[3], patches[4], patches[5]:
        result1 = provider.get_proxy(uri)
        result2 = provider.get_proxy(uri)
    assert result1 == result2
    spawn_patch.assert_called_once()  # second call reuses


def test_get_proxy_direct_uri_different_uris_get_different_subprocesses():
    provider = V2Ray(servers=[])
    provider.binary = Path("/fake/xray")
    uri1 = _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")
    uri2 = _make_vless_uri(remark="🇯🇵 JP", address="2.2.2.2")
    patches = _patch_lifecycle(provider)
    with patches[0] as spawn_patch, patches[1], patches[2], patches[3], patches[4], patches[5]:
        result1 = provider.get_proxy(uri1)
        result2 = provider.get_proxy(uri2)
    assert result1 != result2  # different ports
    assert spawn_patch.call_count == 2


def test_get_proxy_direct_unparseable_uri_raises_value_error():
    provider = V2Ray(servers=[])
    provider.binary = Path("/fake/xray")
    with pytest.raises(ValueError, match="could not be parsed"):
        provider.get_proxy("vmess://!!!not-base64!!!")


def test_get_proxy_country_query_still_works_alongside_direct_uri_mode():
    """Both modes coexist: country queries use the server pool, URIs use one-shot spawn."""
    provider = V2Ray(
        servers=[_make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")],
    )
    provider.binary = Path("/fake/xray")
    patches = _patch_lifecycle(provider)
    direct_uri = _make_vless_uri(remark="🇯🇵 JP", address="2.2.2.2")
    with patches[0] as spawn_patch, patches[1], patches[2], patches[3], patches[4], patches[5]:
        country_result = provider.get_proxy("us")
        direct_result = provider.get_proxy(direct_uri)
    assert country_result is not None
    assert direct_result is not None
    assert country_result != direct_result  # different subprocesses / ports
    assert spawn_patch.call_count == 2


# ---------------------------------------------------------------------------
# resolve_proxy: v2ray: prefix routing (including the digit-in-name fix)
# ---------------------------------------------------------------------------


def test_resolve_proxy_routes_v2ray_prefix_to_v2ray_provider():
    """``--proxy v2ray:us`` must route to the V2Ray provider, not fall through.

    This is a regression test for the provider-prefix regex: the old ``[a-z]+``
    pattern didn't match ``v2ray`` (because of the digit ``2``), so the query
    silently fell through to the try-every-provider loop and raised a confusing
    "No proxy provider had a proxy" error.
    """
    from unshackle.core.proxies.resolve import resolve_proxy

    v2ray_provider = MagicMock()
    v2ray_provider.__class__.__name__ = "V2Ray"
    v2ray_provider.get_proxy.return_value = "socks5://127.0.0.1:11080"

    result = resolve_proxy("v2ray:us", [v2ray_provider])
    assert result == "socks5://127.0.0.1:11080"
    v2ray_provider.get_proxy.assert_called_once_with("us")


def test_resolve_proxy_routes_v2ray_vmess_uri_to_v2ray_provider():
    """``--proxy v2ray:vmess://...`` routes the URI to the V2Ray provider."""
    from unshackle.core.proxies.resolve import resolve_proxy

    v2ray_provider = MagicMock()
    v2ray_provider.__class__.__name__ = "V2Ray"
    v2ray_provider.get_proxy.return_value = "socks5://127.0.0.1:11080"

    uri = _make_vmess_uri(remark="🇺🇸 US", address="1.1.1.1")
    result = resolve_proxy(f"v2ray:{uri}", [v2ray_provider])
    assert result == "socks5://127.0.0.1:11080"
    v2ray_provider.get_proxy.assert_called_once_with(uri)


def test_resolve_proxy_routes_v2ray_vless_uri_to_v2ray_provider():
    from unshackle.core.proxies.resolve import resolve_proxy

    v2ray_provider = MagicMock()
    v2ray_provider.__class__.__name__ = "V2Ray"
    v2ray_provider.get_proxy.return_value = "socks5://127.0.0.1:11080"

    uri = _make_vless_uri(remark="🇯🇵 JP", address="2.2.2.2")
    result = resolve_proxy(f"v2ray:{uri}", [v2ray_provider])
    assert result == "socks5://127.0.0.1:11080"
    v2ray_provider.get_proxy.assert_called_once_with(uri)


def test_resolve_proxy_routes_v2ray_ss_uri_to_v2ray_provider():
    from unshackle.core.proxies.resolve import resolve_proxy

    v2ray_provider = MagicMock()
    v2ray_provider.__class__.__name__ = "V2Ray"
    v2ray_provider.get_proxy.return_value = "socks5://127.0.0.1:11080"

    uri = _make_ss_sip002_uri(remark="🇩🇪 DE", address="3.3.3.3")
    result = resolve_proxy(f"v2ray:{uri}", [v2ray_provider])
    assert result == "socks5://127.0.0.1:11080"
    v2ray_provider.get_proxy.assert_called_once_with(uri)


def test_resolve_proxy_v2ray_not_found_lists_available_providers():
    from unshackle.core.proxies.resolve import resolve_proxy

    nordvpn = MagicMock()
    nordvpn.__class__.__name__ = "NordVPN"

    with pytest.raises(ValueError, match="Proxy provider 'v2ray' not found"):
        resolve_proxy("v2ray:us", [nordvpn])


def test_resolve_proxy_still_routes_all_letter_provider_names():
    """The regex fix must not break existing providers (all-letter names)."""
    from unshackle.core.proxies.resolve import resolve_proxy

    nordvpn = MagicMock()
    nordvpn.__class__.__name__ = "NordVPN"
    nordvpn.get_proxy.return_value = "https://user:pass@us.proxy.nordvpn.com:89"

    result = resolve_proxy("nordvpn:us", [nordvpn])
    assert "nordvpn.com" in result
    nordvpn.get_proxy.assert_called_once_with("us")


def test_resolve_proxy_country_only_does_not_match_v2ray_prefix():
    """``--proxy us`` (no prefix) must NOT be routed to the V2Ray provider.

    It should fall through to the try-every-provider loop so Basic / NordVPN / etc.
    can handle the bare country code.
    """
    from unshackle.core.proxies.resolve import resolve_proxy

    v2ray_provider = MagicMock()
    v2ray_provider.__class__.__name__ = "V2Ray"
    v2ray_provider.get_proxy.return_value = None  # V2Ray has no opinion on bare "us"

    basic_provider = MagicMock()
    basic_provider.__class__.__name__ = "Basic"
    basic_provider.get_proxy.return_value = "http://us-proxy.example:8080"

    result = resolve_proxy("us", [v2ray_provider, basic_provider])
    assert result == "http://us-proxy.example:8080"
    # Both providers are tried (V2Ray first, then Basic which matches).
    v2ray_provider.get_proxy.assert_called_once_with("us")
    basic_provider.get_proxy.assert_called_once_with("us")
