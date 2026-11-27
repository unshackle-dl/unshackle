"""Unit tests for the shared proxy dispatch that dl, search, the REST API and the remote client use: the
provider:query split, the named or bare provider lookup, and the line that reports which proxy provider answered."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from unshackle.core.proxies import Basic
from unshackle.core.proxies.proxy import Proxy
from unshackle.core.proxies.resolve import (
    ResolvedProxy,
    Unavailable,
    describe_proxy,
    pick_proxy,
    resolve_proxy,
    resolve_proxy_value,
    split_proxy_query,
)

pytestmark = pytest.mark.unit

HINT = r"A city query needs a proxy provider prefix"


class Alpha(Proxy):
    def __init__(self, **answers: str) -> None:
        self.answers = answers

    def __repr__(self) -> str:
        return "Alpha"

    def get_proxy(self, query: str) -> str | None:
        return self.answers.get(query)


class Beta(Alpha):
    def last_connection_display(self) -> str | None:
        return "via Dallas (server 12)"


class Tunnel(Alpha):
    def __init__(self, info: dict | None = None, **answers: str) -> None:
        super().__init__(**answers)
        self.info = info
        self.asked: list[str] = []

    def get_connection_info(self, query: str) -> dict | None:
        self.asked.append(query)
        return self.info


@pytest.fixture
def providers() -> list:
    return [
        Unavailable("Gamma", RuntimeError("no route")),
        Alpha(us="http://a:b@alpha.example:80"),
        Beta(us="http://c:d@beta.example:80", ca="http://c:d@beta-ca.example:80"),
    ]


@pytest.fixture(autouse=True)
def info_level() -> Iterator[None]:
    """mask_proxy shows the full URI while DEBUG is active, so pin the level the masking tests expect."""
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.INFO)
    yield
    root.setLevel(level)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("us", (None, "us")),
        ("alpha:us", ("alpha", "us")),
        ("Beta:us:seattle", ("Beta", "us:seattle")),
        ("http://user:pass@proxy.example:8080", (None, "http://user:pass@proxy.example:8080")),
        ("socks5://proxy.example:1080", (None, "socks5://proxy.example:1080")),
        ("localhost:8080", (None, "localhost:8080")),
    ],
)
def test_split_proxy_query(providers: list, value: str, expected: tuple) -> None:
    assert split_proxy_query(value, providers) == expected


@pytest.mark.parametrize("value", ["us:seattle", "us:new-york"])
def test_a_city_query_without_a_provider_prefix_fails_with_a_hint(providers: list, value: str) -> None:
    with pytest.raises(ValueError, match=rf"'us' was not found. Available: \['Alpha', 'Beta'\]. {HINT}"):
        split_proxy_query(value, providers)
    with pytest.raises(ValueError, match=HINT):
        resolve_proxy(value, providers)


def test_a_prefix_naming_a_failed_provider_reports_why(providers: list) -> None:
    with pytest.raises(ValueError, match="Gamma could not load: no route"):
        split_proxy_query("gamma:us", providers)


def test_named_query_asks_only_that_provider(providers: list) -> None:
    provider, uri = pick_proxy(providers, "ca", "beta")
    assert isinstance(provider, Beta) and uri == "http://c:d@beta-ca.example:80"


def test_named_query_without_an_answer_raises(providers: list) -> None:
    with pytest.raises(ValueError, match="The proxy provider alpha had no proxy for ca"):
        pick_proxy(providers, "ca", "alpha")


def test_bare_query_takes_the_first_provider_that_answers(providers: list) -> None:
    provider, uri = pick_proxy(providers, "ca")
    assert isinstance(provider, Beta) and uri == "http://c:d@beta-ca.example:80"
    provider, uri = pick_proxy(providers, "us")
    assert type(provider) is Alpha and uri == "http://a:b@alpha.example:80"


def test_bare_query_without_an_answer_raises(providers: list) -> None:
    with pytest.raises(ValueError, match="No proxy provider had a proxy for de"):
        pick_proxy(providers, "de")


def test_describe_masks_the_credentials() -> None:
    line = describe_proxy(Alpha(), "http://user:pass@alpha.example:80", "us")
    assert line == "Using Alpha Proxy: http://xxxxx:xxxxx@alpha.example:80"


def test_describe_masks_the_host_of_a_basic_proxy() -> None:
    line = describe_proxy(Basic(us="http://user:pass@mine.example:3128"), "http://user:pass@mine.example:3128", "us")
    assert line == "Using Basic Proxy: http://xxxxx:xxxxx@xxxxx:3128"


def test_describe_prefers_the_provider_display() -> None:
    assert describe_proxy(Beta(), "http://c:d@beta.example:80", "us") == "Using Beta Proxy via Dallas (server 12)"


def test_describe_reports_a_vpn_connection() -> None:
    tunnel = Tunnel({"public_ip": "203.0.113.9", "city": "Toronto", "country": "Canada"})
    assert describe_proxy(tunnel, "http://127.0.0.1:8888", "alpha:ca") == "VPN Connected: 203.0.113.9 (Toronto, Canada)"
    assert tunnel.asked == ["alpha:ca"]


def test_describe_falls_back_when_the_vpn_has_no_connection_info() -> None:
    assert describe_proxy(Tunnel(None), "http://127.0.0.1:8888", "ca") == "Using Tunnel Proxy: http://127.0.0.1:8888"


def test_describe_shows_the_full_uri_in_debug_only_when_allowed() -> None:
    logging.getLogger().setLevel(logging.DEBUG)
    uri = "http://user:pass@alpha.example:80"
    assert describe_proxy(Alpha(), uri, "us") == f"Using Alpha Proxy: {uri}"
    assert "user:pass" not in describe_proxy(Alpha(), uri, "us", allow_debug=False)


@pytest.mark.parametrize(
    "uri", ["http://user:pass@proxy.example:8080", "https://proxy.example:443", "socks5h://proxy.example:1080"]
)
def test_resolve_proxy_passes_a_uri_through(providers: list, uri: str) -> None:
    assert resolve_proxy(uri, providers) == uri


def test_resolve_proxy_logs_the_provider_without_credentials(providers: list, caplog: pytest.LogCaptureFixture) -> None:
    logging.getLogger().setLevel(logging.DEBUG)
    with caplog.at_level(logging.INFO, logger="proxies"):
        assert resolve_proxy("beta:ca", providers) == "http://c:d@beta-ca.example:80"
        assert resolve_proxy("us", providers) == "http://a:b@alpha.example:80"
    assert "Using Beta Proxy via Dallas (server 12)" in caplog.text
    assert "Using Alpha Proxy: http://xxxxx:xxxxx@alpha.example:80" in caplog.text


def test_resolve_proxy_raises_when_no_provider_answers(providers: list) -> None:
    with pytest.raises(ValueError, match="No proxy provider had a proxy for de"):
        resolve_proxy("de", providers)


def test_bare_query_skips_a_provider_that_raises(caplog: pytest.LogCaptureFixture) -> None:
    class Broken:
        def get_proxy(self, query: str) -> str:
            raise ConnectionError("api down")

    class Working:
        def get_proxy(self, query: str) -> str:
            return f"http://working/{query}"

    with caplog.at_level(logging.WARNING):
        provider, uri = pick_proxy([Broken(), Working()], "us")
    assert uri == "http://working/us" and isinstance(provider, Working)
    assert "Broken proxy provider failed for us" in caplog.text
    with pytest.raises(ValueError, match=r"No proxy provider had a proxy for us \(Broken: api down\)"):
        pick_proxy([Broken()], "us")


def test_named_query_passes_the_provider_error_on_as_a_value_error() -> None:
    class Broken:
        def get_proxy(self, query: str) -> str:
            raise ConnectionError("api down")

    with pytest.raises(ValueError, match="The proxy provider broken failed for us: api down") as raised:
        pick_proxy([Broken()], "us", "broken")
    assert isinstance(raised.value.__cause__, ConnectionError)


def test_named_query_names_the_provider_for_a_bad_catalogue_response() -> None:
    class Broken:
        def get_proxy(self, query: str) -> str:
            raise json.JSONDecodeError("Expecting value", "", 0)

    with pytest.raises(ValueError, match="The proxy provider broken failed for us: Expecting value"):
        pick_proxy([Broken()], "us", "broken")


def test_named_query_passes_a_query_error_on_unchanged() -> None:
    class Strict:
        def get_proxy(self, query: str) -> str:
            raise ValueError("no server for us150")

    with pytest.raises(ValueError, match="^no server for us150$"):
        pick_proxy([Strict()], "us150", "strict")


class Gluetun(Alpha):
    pass


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "US",
            ResolvedProxy(
                "http://a:b@alpha.example:80", "us", "Using Alpha Proxy: http://xxxxx:xxxxx@alpha.example:80"
            ),
        ),
        (
            "Beta:CA",
            ResolvedProxy("http://c:d@beta-ca.example:80", "ca", "Using Beta Proxy via Dallas (server 12)"),
        ),
        (
            "alpha:beta:ca",
            ResolvedProxy(
                "http://a:b@alpha-beta.example:80",
                "beta:ca",
                "Using Alpha Proxy: http://xxxxx:xxxxx@alpha-beta.example:80",
            ),
        ),
        (
            "http://user:pass@proxy.example:8080",
            ResolvedProxy(
                "http://user:pass@proxy.example:8080",
                None,
                "Using explicit Proxy: http://xxxxx:xxxxx@proxy.example:8080",
            ),
        ),
        ("localhost:8080", ResolvedProxy("localhost:8080", None, "Using a local proxy on port 8080")),
        ("1.2.3.4:3128", ResolvedProxy("1.2.3.4:3128", None, "Using explicit Proxy: 1.2.3.4:3128")),
        ("us:1080", ResolvedProxy("us:1080", None, "Using explicit Proxy: us:1080")),
    ],
)
def test_resolve_proxy_value_reads_each_form(providers: list, value: str, expected: ResolvedProxy) -> None:
    providers[1].answers["beta:ca"] = "http://a:b@alpha-beta.example:80"
    assert resolve_proxy_value(value, providers) == expected


def test_resolve_proxy_value_starts_a_forwarder_for_a_control_d_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.proxies import controld

    monkeypatch.setattr(controld, "local_proxy", lambda uri: "http://127.0.0.1:4000")
    resolved = resolve_proxy_value("controld://efgh456@dns.controld.com", [])
    assert resolved == ResolvedProxy("http://127.0.0.1:4000", None, "Using a local proxy on port 4000")


@pytest.mark.parametrize(
    ("value", "message"), [("us", "Getting a Proxy to us..."), ("gluetun:ca", "Connecting to VPN (ca)...")]
)
def test_the_status_wraps_only_the_provider_lookup(providers: list, value: str, message: str) -> None:
    shown: list[str] = []

    @contextmanager
    def status(text: str) -> Iterator[None]:
        shown.append(text)
        yield

    resolve_proxy_value(value, [*providers, Gluetun(ca="http://g.example:8888")], status=status)
    resolve_proxy_value("http://proxy.example:8080", providers, status=status)
    assert shown == [message]


@pytest.mark.parametrize("value", ["localhost:8080", "1.2.3.4:3128"])
def test_resolve_proxy_passes_a_host_and_port_through(providers: list, value: str) -> None:
    assert resolve_proxy(value, providers) == value


class Recorder(Alpha):
    def __init__(self, **answers: str) -> None:
        super().__init__(**answers)
        self.asked: list[str] = []

    def get_proxy(self, query: str) -> str | None:
        self.asked.append(query)
        return super().get_proxy(query)


@pytest.mark.parametrize(
    ("value", "asked", "uri"),
    [
        ("us", ["ca"], "http://ca.example:8080"),
        ("recorder:us", ["uk"], "http://uk.example:8080"),
        ("US", ["ca"], "http://ca.example:8080"),
        ("Recorder:US", ["uk"], "http://uk.example:8080"),
        ("fr", ["zz", "fr"], "http://fr.example:8080"),
        ("de", ["de"], "http://de.example:8080"),
    ],
)
def test_a_proxy_map_swaps_the_query_and_falls_back_to_it(value: str, asked: list, uri: str) -> None:
    recorder = Recorder(**{x: f"http://{x}.example:8080" for x in ("ca", "uk", "fr", "de")})
    resolved = resolve_proxy_value(value, [recorder], proxy_map={"US": "ca", "recorder:us": "uk", "fr": "zz"})
    assert (recorder.asked, resolved.uri) == (asked, uri)
