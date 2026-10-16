from types import SimpleNamespace

import requests
from requests.utils import select_proxy

from unshackle.core import service as service_mod
from unshackle.core.service import Service
from unshackle.core.utils import run_proxy

PROXY = "https://u:p@chosen.example:89"
URL = "https://api.example.com/x"


class Dummy(Service):
    def get_titles(self):  # type: ignore[override]
        raise NotImplementedError

    def get_tracks(self, title):  # type: ignore[override]
        raise NotImplementedError

    def get_chapters(self, title):  # type: ignore[override]
        raise NotImplementedError


def test_chosen_proxy_beats_env_proxy_and_is_watched(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://system.example:8080")
    monkeypatch.setattr(service_mod, "verify_proxy_exit", lambda session: {"country": "fr"})
    monkeypatch.setattr(run_proxy, "watched", {})
    monkeypatch.setattr(run_proxy, "proxy_hosts", set())
    ctx = SimpleNamespace(
        obj=SimpleNamespace(config={}, proxy_providers=[]),
        parent=SimpleNamespace(params={"proxy": PROXY}),
    )
    session = Dummy(ctx).session  # type: ignore[arg-type]
    assert isinstance(session, requests.Session)
    settings = session.merge_environment_settings(URL, {}, None, None, None)
    assert select_proxy(URL, settings["proxies"]) == PROXY
    (watch,) = run_proxy.watched.values()
    assert (watch.cls, watch.proxies) == (Dummy, [PROXY])
