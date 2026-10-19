"""``services.<TAG>.dl`` ``remote`` and ``server`` route a service without ``--remote``. Explicit CLI values win."""

from __future__ import annotations

from typing import Any, Optional, cast

import click
import pytest

from unshackle.commands.dl import dl
from unshackle.commands.search import search
from unshackle.core.config import config
from unshackle.core.services import Services


def make_ctx(
    command: click.Command, args: Optional[list[str]] = None, default_map: Optional[dict[str, Any]] = None
) -> click.Context:
    """Parse the group options of a real ``Services`` command, without running its callback."""
    return command.make_context(
        command.name or "dl", [*(args or []), "EXAMPLE", "title"], default_map=default_map or {}
    )


def group(ctx: click.Context) -> Services:
    return cast(Services, ctx.command)


@pytest.fixture
def routed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Optional[str]]]:
    """Record the path get_command takes and the server it passes, without loading a service or calling a server."""
    calls: list[tuple[str, Optional[str]]] = []

    def remote(name: str, server_name: Optional[str]) -> click.Command:
        calls.append(("remote", server_name))
        return click.Command(name)

    def local(tag: str) -> Any:
        calls.append(("local", None))
        return type("FakeService", (), {"cli": click.Command(tag)})

    monkeypatch.setattr(Services, "make_remote_command", staticmethod(remote))
    monkeypatch.setattr(Services, "load", staticmethod(local))
    return calls


@pytest.mark.parametrize("command", [dl.cli, search])
def test_service_remote_routes_to_remote_command(
    monkeypatch: pytest.MonkeyPatch, routed: list[tuple[str, Optional[str]]], command: click.Command
) -> None:
    """The typed name matches the config key in any case, as a service only the server has keeps the typed case."""
    monkeypatch.setattr(config, "services", {"EXAMPLE": {"dl": {"remote": True, "server": "box"}}})
    ctx = make_ctx(command)
    group(ctx).resolve_command(ctx, ["example", "title"])
    assert routed == [("remote", "box")]
    assert ctx.params["remote"] is True
    assert ctx.params["server"] == "box"


@pytest.mark.parametrize(
    ("services", "args", "default_map", "expected"),
    [
        ({"OTHER": {"dl": {"remote": True}}}, [], None, ("local", None)),
        ({"EXAMPLE": {"dl": {"remote": True, "server": "box"}}}, ["--server", "other"], None, ("remote", "other")),
        ({"EXAMPLE": {"dl": {"remote": False}}}, [], {"remote": True}, ("local", None)),
        ({"EXAMPLE": {"dl": {"remote": "false"}}}, [], None, ("local", None)),
    ],
    ids=["other-service", "cli-server-wins", "service-false-beats-global", "quoted-false-is-cast"],
)
def test_routing_precedence(
    monkeypatch: pytest.MonkeyPatch,
    routed: list,
    services: dict,
    args: list[str],
    default_map: Optional[dict[str, Any]],
    expected: tuple[str, Optional[str]],
) -> None:
    monkeypatch.setattr(config, "services", services)
    ctx = make_ctx(dl.cli, args, default_map)
    group(ctx).get_command(ctx, "EXAMPLE")
    assert routed == [expected]


def test_invalid_value_names_the_config_key(monkeypatch: pytest.MonkeyPatch, routed: list) -> None:
    monkeypatch.setattr(config, "services", {"EXAMPLE": {"dl": {"remote": "maybe"}}})
    ctx = make_ctx(dl.cli)
    with pytest.raises(click.ClickException, match=r"services\.EXAMPLE\.dl\.remote"):
        group(ctx).get_command(ctx, "EXAMPLE")


def test_resolved_service_does_not_route_the_next(monkeypatch: pytest.MonkeyPatch, routed: list) -> None:
    """Help and shell completion call get_command for every service on the context that resolved one."""
    monkeypatch.setattr(config, "services", {"EXAMPLE": {"dl": {"remote": True}}})
    ctx = make_ctx(dl.cli)
    group(ctx).resolve_command(ctx, ["EXAMPLE", "title"])
    assert ctx.params["remote"] is True
    group(ctx).get_command(ctx, "OTHER")
    assert routed == [("remote", None), ("local", None)]


def test_import_ignores_service_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "services", {"EXAMPLE": {"dl": {"remote": True}}})
    monkeypatch.setattr(Services, "make_import_command", staticmethod(lambda tag, ctx: click.Command(tag)))
    ctx = make_ctx(dl.cli, ["--import", "export.json"])
    group(ctx).resolve_command(ctx, ["EXAMPLE", "title"])
    assert ctx.params["remote"] is False
