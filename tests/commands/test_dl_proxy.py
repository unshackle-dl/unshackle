"""The dl --proxy block resolves each form through the shared proxy entry and codes its errors."""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from unshackle.core.remote_protocol import APIErrorCode

pytestmark = pytest.mark.unit


def run_dl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, proxy: str, providers: list | None = None, remote: bool = False
) -> click.Context:
    from unshackle.commands import dl as dl_module

    monkeypatch.setattr(dl_module.config, "key_vaults", [])
    monkeypatch.setattr(dl_module.config, "output_template", {"movies": "{title}"})
    monkeypatch.setattr(dl_module.dl, "get_cdm", lambda self, *a, **k: None)
    monkeypatch.setattr(dl_module.Services, "get_path", staticmethod(lambda tag: tmp_path))
    monkeypatch.setattr(dl_module.dl.cli, "_result_callback", dl_module.dl.cli._result_callback)
    ctx = click.Context(dl_module.dl.cli)
    ctx.invoked_subcommand = "EXAMPLE"
    ctx.params["remote"] = remote
    dl_module.dl(ctx=ctx, no_proxy=False, proxy=proxy, proxy_providers=providers or [])
    return ctx


@pytest.mark.usefixtures("example_service")
def test_dl_starts_a_forwarder_for_a_control_d_resolver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from unshackle.core.proxies import controld

    monkeypatch.setattr(controld, "local_proxy", lambda uri: "http://127.0.0.1:4000")
    params = run_dl(monkeypatch, tmp_path, "controld://efgh456@dns.controld.com").params
    assert params["proxy"] == "http://127.0.0.1:4000"
    assert params["proxy_query"] is None


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


class Recorder:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def get_proxy(self, query: str) -> str:
        self.asked.append(query)
        return f"http://{query}.proxy.example:8080"


@pytest.mark.usefixtures("example_service")
@pytest.mark.parametrize(("remote", "asked"), [(False, ["ca"]), (True, ["us"])])
def test_dl_resolves_a_mapped_query_once_and_only_on_a_local_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: bool, asked: list
) -> None:
    from unshackle.commands import dl as dl_module

    monkeypatch.setattr(dl_module.config, "services", {"example": {"proxy_map": {"us": "ca"}}})
    recorder = Recorder()
    params = run_dl(monkeypatch, tmp_path, "us", [recorder], remote).params
    assert (recorder.asked, params["proxy"]) == (asked, f"http://{asked[0]}.proxy.example:8080")
