"""The search command skips the login for a service that sets ANONYMOUS_SEARCH."""

from types import SimpleNamespace

import pytest

from unshackle.commands import search as search_command
from unshackle.core.search_result import SearchResult


class _SearchService:
    ANONYMOUS_SEARCH = False

    def __init__(self):
        self.calls = []
        self.session = SimpleNamespace(cookies=object())

    def authenticate(self, cookies, credential):
        self.calls.append("authenticate")

    def search(self):
        yield SearchResult(id_="abc", title="A Title")


@pytest.fixture
def login_calls(monkeypatch, tmp_path):
    calls = []

    def record(name, value):
        def call(*args):
            calls.append(name)
            return value

        return call

    dl = search_command.dl
    monkeypatch.setattr(dl, "get_cookie_jar", record("get_cookie_jar", None))
    monkeypatch.setattr(dl, "get_credentials", record("get_credentials", None))
    monkeypatch.setattr(dl, "get_cookie_path", record("get_cookie_path", tmp_path / "cookies.txt"))
    monkeypatch.setattr(dl, "save_cookies", record("save_cookies", None))
    return calls


def test_search_authenticates_and_saves_cookies_by_default(login_calls):
    service = _SearchService()
    search_command.result(service)

    assert service.calls == ["authenticate"]
    assert login_calls == ["get_cookie_jar", "get_credentials", "get_cookie_path", "save_cookies"]


def test_anonymous_search_touches_no_login_material_and_no_cookie_file(login_calls):
    service = _SearchService()
    service.ANONYMOUS_SEARCH = True
    search_command.result(service)

    assert service.calls == []
    assert login_calls == []
