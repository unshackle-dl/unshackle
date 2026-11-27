"""The search command resolves --proxy with the same grammar as dl."""

from __future__ import annotations

from pathlib import Path

import click
import pytest

pytestmark = pytest.mark.unit

SENT = "controld://efgh456@dns.controld.com"


@pytest.mark.usefixtures("example_service")
def test_search_starts_a_forwarder_for_a_control_d_resolver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from unshackle.commands import search as search_module
    from unshackle.core.proxies import controld

    monkeypatch.setattr(controld, "local_proxy", lambda uri: "http://127.0.0.1:4000")
    monkeypatch.setattr(search_module, "load_proxy_providers", lambda: [])
    monkeypatch.setattr(search_module.Services, "get_path", staticmethod(lambda tag: tmp_path))
    ctx = click.Context(search_module.search)
    ctx.invoked_subcommand = "EXAMPLE"
    ctx.params = {"proxy": SENT, "no_proxy": False}
    with ctx:
        assert search_module.search.callback
        search_module.search.callback(no_proxy=False, proxy=SENT)

    assert ctx.params["proxy"] == "http://127.0.0.1:4000"
    assert ctx.params["proxy_arg"] == SENT
    assert ctx.params.get("proxy_query") is None


class Recorder:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def get_proxy(self, query: str) -> str:
        self.asked.append(query)
        return f"http://{query}.proxy.example:8080"


@pytest.mark.usefixtures("example_service")
@pytest.mark.parametrize(("remote", "asked"), [(False, ["ca"]), (True, ["us"])])
def test_search_resolves_a_mapped_query_once_and_only_on_a_local_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: bool, asked: list
) -> None:
    from unshackle.commands import search as search_module

    recorder = Recorder()
    monkeypatch.setattr(search_module.config, "services", {"example": {"proxy_map": {"us": "ca"}}})
    monkeypatch.setattr(search_module, "load_proxy_providers", lambda: [recorder])
    monkeypatch.setattr(search_module.Services, "get_path", staticmethod(lambda tag: tmp_path))
    ctx = click.Context(search_module.search)
    ctx.invoked_subcommand = "EXAMPLE"
    ctx.params = {"proxy": "us", "no_proxy": False, "remote": remote}
    with ctx:
        assert search_module.search.callback
        search_module.search.callback(no_proxy=False, proxy="us", remote=remote)

    assert (recorder.asked, ctx.params["proxy"]) == (asked, f"http://{asked[0]}.proxy.example:8080")
