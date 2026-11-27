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
