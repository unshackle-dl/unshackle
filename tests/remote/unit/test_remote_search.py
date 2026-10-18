"""RemoteService.search: the body ``search --remote`` posts, and the results it yields."""

from __future__ import annotations

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
