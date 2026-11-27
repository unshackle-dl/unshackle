"""The dl --proxy block resolves each form through the shared proxy entry and codes its errors."""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from unshackle.core.remote_protocol import APIErrorCode

pytestmark = pytest.mark.unit


def run_dl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, proxy: str) -> click.Context:
    from unshackle.commands import dl as dl_module

    monkeypatch.setattr(dl_module.config, "key_vaults", [])
    monkeypatch.setattr(dl_module.config, "output_template", {"movies": "{title}"})
    monkeypatch.setattr(dl_module.dl, "get_cdm", lambda self, *a, **k: None)
    monkeypatch.setattr(dl_module.Services, "get_path", staticmethod(lambda tag: tmp_path))
    monkeypatch.setattr(dl_module.dl.cli, "_result_callback", dl_module.dl.cli._result_callback)
    ctx = click.Context(dl_module.dl.cli)
    ctx.invoked_subcommand = "EXAMPLE"
    dl_module.dl(ctx=ctx, no_proxy=False, proxy=proxy, proxy_providers=[])
    return ctx


@pytest.mark.usefixtures("example_service")
def test_dl_starts_a_forwarder_for_a_control_d_resolver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from unshackle.core.proxies import controld

    monkeypatch.setattr(controld, "local_proxy", lambda uri: "http://127.0.0.1:4000")
    params = run_dl(monkeypatch, tmp_path, "controld://efgh456@dns.controld.com").params
    assert params["proxy"] == "http://127.0.0.1:4000"
    assert (params["proxy_query"], params["proxy_provider"]) == (None, None)


@pytest.mark.usefixtures("example_service")
def test_dl_keeps_a_host_and_port_as_given(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert run_dl(monkeypatch, tmp_path, "nas:3128").params["proxy"] == "nas:3128"


@pytest.mark.usefixtures("example_service")
@pytest.mark.parametrize("proxy", ["nope:us", "us", "controld://bad id@dns.controld.com"])
def test_a_dl_proxy_error_is_a_proxy_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, proxy: str) -> None:
    from unshackle.core.run_plan import PlanError

    with pytest.raises(PlanError) as raised:
        run_dl(monkeypatch, tmp_path, proxy)
    assert getattr(raised.value, "error_code", None) == APIErrorCode.INVALID_PROXY
