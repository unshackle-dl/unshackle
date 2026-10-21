"""RemoteService.search: the body ``search --remote`` posts, and the results it yields."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from unshackle.core import remote_service
from unshackle.core.remote_service import RemoteService

pytestmark = pytest.mark.unit


class FakeClient:
    server_url = "https://server.example"

    def __init__(self) -> None:
        self.sent: dict[str, Any] = {}
        self.tunnel: Any = None

    def post(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        self.sent = {"endpoint": endpoint, **data}
        return {"results": [{"id": "t1", "title": "Show", "label": "2019"}], "cache": {"token": "x"}}


def make_service(params: dict[str, Any], server_accounts: Any = None) -> tuple[RemoteService, FakeClient, list]:
    svc = RemoteService.__new__(RemoteService)
    client = FakeClient()
    saved: list = []
    svc.service_tag = "TEST"
    svc.title_id = "query"
    svc.client = client  # type: ignore[assignment]
    svc.ctx = SimpleNamespace(parent=SimpleNamespace(params=params), obj=None)  # type: ignore[assignment]
    svc._server_accounts = server_accounts
    svc._anonymous_search = False
    svc.load_cache_files = lambda profile=None: {"token": "old"}  # type: ignore[method-assign]
    svc.save_returned_cache = lambda data, profile=None: saved.append(data)  # type: ignore[method-assign]
    return svc, client, saved


def test_search_sends_client_login_and_yields_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_service, "load_credentials_for_transport", lambda tag, profile: {"username": "u"})
    monkeypatch.setattr(remote_service, "load_cookies_for_transport", lambda tag, profile: None)
    svc, client, saved = make_service({"profile": "p1", "no_proxy": True})

    results = list(svc.search())

    assert client.sent == {
        "endpoint": "/api/search",
        "service": "TEST",
        "query": "query",
        "profile": "p1",
        "credentials": {"username": "u"},
        "cache": {"token": "old"},
        "no_proxy": True,
    }
    assert [(r.id, r.title, r.label) for r in results] == [("t1", "Show", "2019")]
    assert saved == [{"token": "x"}]


@pytest.mark.parametrize(
    ("server_accounts", "anonymous"), [({"global": True}, False), (None, True)], ids=["server-account", "anonymous"]
)
def test_search_sends_no_login(server_accounts: Any, anonymous: bool) -> None:
    svc, client, _ = make_service({}, server_accounts)
    svc._anonymous_search = anonymous

    list(svc.search())

    assert client.sent == {"endpoint": "/api/search", "service": "TEST", "query": "query"}


def test_local_proxy_sends_the_server_a_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeTunnel:
        def __init__(self, server_url: str, headers: dict) -> None:
            self.id = "abc"

        def start(self) -> str:
            return self.id

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(remote_service, "TunnelClient", FakeTunnel)
    svc, client, _ = make_service({"local_proxy": True})
    svc.log = logging.getLogger("test")
    client.session = SimpleNamespace(headers={"X-Secret-Key": "k"})  # type: ignore[attr-defined]

    list(svc.search())

    assert client.sent["proxy"] == "tunnel:abc"
    assert client.tunnel.stopped


def test_a_loopback_proxy_points_to_local_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    import click

    monkeypatch.setattr(remote_service, "resolve_remote_proxy_arg", lambda arg, providers=None: arg)
    svc, _, _ = make_service({"proxy": "http://127.0.0.1:8888"}, {"global": True})

    with pytest.raises(click.ClickException, match="--local-proxy without --proxy"):
        list(svc.search())


def test_local_proxy_is_refused_for_a_server_account_service() -> None:
    import click

    svc, _, _ = make_service({"local_proxy": True}, {"global": True})

    with pytest.raises(click.ClickException, match="its own account"):
        list(svc.search())
