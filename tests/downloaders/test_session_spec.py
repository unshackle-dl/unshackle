"""Session spec fidelity across the multiprocess spawn boundary.

``build_session_spec``/``rebuild_session`` are how a spawned child gets the parent's session
state. The cross-process half needs a live CDN to observe, so these tests pin the two halves
that can be checked deterministically: the spec is picklable (spawn requires it), and a
rebuilt session carries the same cookie domains as the parent. A spec that flattens cookies
to name/value rebuilds every cookie scoped to localhost, so a cookie-authed CDN sees none of
them and 403s only when ``--download-processes`` is above 1.
"""

import pickle

from requests import Session

from unshackle.core.downloaders.requests import build_session_spec, rebuild_session
from unshackle.core.session import RnetSession
from unshackle.core.session import session as make_session


def rnet_session_with_cookies() -> RnetSession:
    """RnetSession carrying two domain-scoped cookies plus one set from a plain dict.

    The dict-set cookie lands in the adapter's flat map only, never in its domain map, so it
    is the case a domain-only serialization would silently drop.
    """
    ns = make_session("Chrome131")
    ns.cookies.set("cdn_token", "abc", domain="cdn.example.com")
    ns.cookies.set("api_token", "def", domain="api.example.com")
    ns.cookies.update({"loose": "ghi"})
    return ns


def test_rnet_spec_round_trips_cookie_domains() -> None:
    ns = rnet_session_with_cookies()

    spec = build_session_spec(ns)
    assert spec is not None
    rebuilt = rebuild_session(pickle.loads(pickle.dumps(spec)), 1)
    assert rebuilt is not None

    assert rebuilt.cookies.get_dict_by_domain() == ns.cookies.get_dict_by_domain()
    assert rebuilt.cookies.get_dict() == ns.cookies.get_dict()
    assert rebuilt.cookies.get("cdn_token", domain="cdn.example.com") == "abc"
    assert rebuilt.cookies.get("api_token", domain="api.example.com") == "def"
    assert rebuilt.cookies.get_dict(domain="localhost") == {}


def test_rnet_spec_keeps_dict_set_cookies() -> None:
    ns = rnet_session_with_cookies()

    spec = build_session_spec(ns)
    assert spec is not None
    rebuilt = rebuild_session(spec, 1)
    assert rebuilt is not None

    assert rebuilt.cookies["loose"] == "ghi"


def test_rnet_spec_cookies_carry_domain_information() -> None:
    ns = rnet_session_with_cookies()

    spec = build_session_spec(ns)
    assert spec is not None

    cookies = spec["cookies"]
    assert set(cookies) == {"cdn.example.com", "api.example.com", ""}
    assert all(isinstance(jar, dict) for jar in cookies.values())
    assert cookies["cdn.example.com"] == {"cdn_token": "abc"}


def test_rnet_spec_is_picklable() -> None:
    spec = build_session_spec(rnet_session_with_cookies())
    assert spec is not None

    assert pickle.loads(pickle.dumps(spec)) == spec


def test_rnet_spec_without_impersonate_is_not_rebuildable() -> None:
    assert build_session_spec(RnetSession()) is None


def test_rebuilt_requests_session_pool_is_sized_to_worker_count() -> None:
    """A child rebuilding a requests session must size its pool, not inherit urllib3's default 10."""
    spec = build_session_spec(Session())
    assert spec is not None

    rebuilt = rebuild_session(spec, 24)
    assert rebuilt is not None

    for scheme in ("https://example.com", "http://example.com"):
        adapter = rebuilt.get_adapter(scheme)
        assert adapter._pool_maxsize == 24
        assert adapter._pool_connections == 24
        assert adapter._pool_block is True
        assert adapter.poolmanager.connection_pool_kw["maxsize"] == 24


def test_rebuilt_rnet_session_has_no_adapters() -> None:
    """rnet pools internally; the sizing fix must not reach into that path."""
    spec = build_session_spec(rnet_session_with_cookies())
    assert spec is not None

    rebuilt = rebuild_session(spec, 24)
    assert rebuilt is not None

    assert not isinstance(rebuilt, Session)
    assert not hasattr(rebuilt, "adapters")


def test_unrebuildable_spec_stays_none() -> None:
    assert rebuild_session({"kind": "none"}, 24) is None


def test_requests_spec_round_trips_cookie_domains() -> None:
    rs = Session()
    rs.cookies.set("cdn_token", "abc", domain="cdn.example.com")
    rs.cookies.set("api_token", "def", domain="api.example.com")

    spec = build_session_spec(rs)
    assert spec is not None
    rebuilt = rebuild_session(pickle.loads(pickle.dumps(spec)), 1)
    assert rebuilt is not None

    assert rebuilt.cookies.get_dict(domain="cdn.example.com") == {"cdn_token": "abc"}
    assert rebuilt.cookies.get_dict(domain="api.example.com") == {"api_token": "def"}
