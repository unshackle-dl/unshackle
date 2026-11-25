"""Each builder of a service's launch context, and the launch keys it sets in ``ctx.parent.params``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import click
import pytest

from unshackle.core.launch import LaunchKeys

pytestmark = pytest.mark.unit


class Stop(Exception):
    pass


class ExampleProxy:
    def get_proxy(self, query: str) -> str:
        return f"http://{query}.proxy.example:8080"


def cli_dl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> click.Context:
    from unshackle.commands import dl as dl_module

    monkeypatch.setattr(dl_module.config, "key_vaults", [])
    monkeypatch.setattr(dl_module.config, "output_template", {"movies": "{title}"})
    monkeypatch.setattr(dl_module.dl, "get_cdm", lambda self, *a, **k: None)
    monkeypatch.setattr(dl_module.Services, "get_path", staticmethod(lambda tag: tmp_path))
    # dl.__init__ hangs its result callback on the shared click command
    monkeypatch.setattr(dl_module.dl.cli, "_result_callback", dl_module.dl.cli._result_callback)
    ctx = click.Context(dl_module.dl.cli)
    ctx.invoked_subcommand = "EXAMPLE"
    dl_module.dl(ctx=ctx, no_proxy=False, proxy="us", proxy_providers=[ExampleProxy()])
    return ctx


def cli_search(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> click.Context:
    from unshackle.commands import search as search_module

    monkeypatch.setattr(search_module, "load_proxy_providers", lambda: [ExampleProxy()])
    monkeypatch.setattr(search_module.Services, "get_path", staticmethod(lambda tag: tmp_path))
    ctx = click.Context(search_module.search)
    ctx.invoked_subcommand = "EXAMPLE"
    ctx.params = {"proxy": "us", "no_proxy": False}
    with ctx:
        assert search_module.search.callback
        search_module.search.callback(no_proxy=False, proxy="us")
    return ctx


def rest_handlers(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict]:
    """The handlers module with the service, its config, CDM and proxy stubbed, and the context it builds."""
    from unshackle.core.api import handlers

    seen: dict = {}

    def instantiate(parent_ctx: click.Context, *a: Any) -> Any:
        seen["ctx"] = parent_ctx
        return SimpleNamespace(authenticate=lambda cookies, credential: None)

    monkeypatch.setattr(handlers, "load_service_yaml", lambda tag: {})
    monkeypatch.setattr(handlers, "resolve_handler_proxy", lambda *a: (None, []))
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *a: None)
    monkeypatch.setattr(handlers.Services, "load", lambda tag: SimpleNamespace())
    monkeypatch.setattr(handlers, "instantiate_service", instantiate)
    return handlers, seen


def rest_relay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> click.Context:
    from unshackle.core import remote_service

    handlers, _ = rest_handlers(monkeypatch)
    monkeypatch.setattr(remote_service, "make_remote_service", lambda ctx, *a, **k: ctx)
    return handlers.relay_service({"service": "EXAMPLE"}, "server", None, "t").parent


def rest_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> click.Context:
    handlers, seen = rest_handlers(monkeypatch)
    handlers.setup_list_service({}, "EXAMPLE", None, "t")
    return seen["ctx"]


def rest_search(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> click.Context:
    handlers, seen = rest_handlers(monkeypatch)
    from unshackle.core.api.errors import APIError

    def instantiate(parent_ctx: click.Context, *a: Any) -> Any:
        seen["ctx"] = parent_ctx
        raise Stop

    monkeypatch.setattr(handlers, "instantiate_service", instantiate)
    with pytest.raises(APIError):
        handlers.run_service_search({}, "EXAMPLE", "query")
    return seen["ctx"]


def rest_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> click.Context:
    handlers, seen = rest_handlers(monkeypatch)
    handlers.create_service_instance("EXAMPLE", "t", {}, None, [], None)
    return seen["ctx"]


def rest_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> click.Context:
    from unshackle.commands import dl as dl_module
    from unshackle.core.api import download_manager

    class FakeDl:
        cli = dl_module.dl.cli

        def __init__(self, ctx: click.Context, **kwargs: Any) -> None:
            raise Stop(ctx)

    monkeypatch.setattr(dl_module, "dl", FakeDl)
    with pytest.raises(Stop) as stopped:
        download_manager.perform_download("job-1", "EXAMPLE", "t1", {})
    return stopped.value.args[0]


SERVED_LOGIN: LaunchKeys = {"served": True, "cookies_supplied": False}


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (cli_dl, {"proxy_arg": "us", "proxy_query": "us", "proxy_provider": None}),
        (cli_search, {"proxy_arg": "us", "proxy_query": "us", "proxy_provider": None}),
        (rest_relay, {"served": True, "client_argv": "serve (REST API)"}),
        (rest_list, SERVED_LOGIN),
        (rest_search, SERVED_LOGIN),
        (rest_session, {**SERVED_LOGIN, "client_device": False, "client_identity": False}),
        (rest_job, {"client_argv": "serve (REST API) download job job-1"}),
    ],
    ids=lambda v: v.__name__ if callable(v) else "",
)
@pytest.mark.usefixtures("example_service")
def test_each_builder_sets_its_launch_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    build: Callable[[pytest.MonkeyPatch, Path], click.Context],
    expected: LaunchKeys,
) -> None:
    params = build(monkeypatch, tmp_path).params
    assert {key: params[key] for key in LaunchKeys.__annotations__ if key in params} == expected
