"""make_remote_command resolves names against the remote server's list, reports the failures and keeps served option names."""

from __future__ import annotations

from unittest.mock import patch

import click
import pytest

from unshackle.core.services import Services

SERVED = [
    {
        "tag": "EXAMPLE",
        "url": "https://example.com",
        "help": "Example service.",
        "cli_params": [
            {"kind": "option", "name": "zone_code", "opts": ["--zone"], "is_flag": False, "default": None},
            {"kind": "option", "name": "flavour_name", "opts": ["--flavour"], "is_flag": False, "choices": ["a", "b"]},
            {"kind": "option", "name": "loud_mode", "opts": ["--loud"], "is_flag": True, "default": False},
        ],
        "aliases": [],
    },
    {
        "tag": "DEMO",
        "url": "https://example.com",
        "help": "Demo service.",
        "cli_params": [],
        "aliases": ["ALT", "alt-demo"],
    },
]


def _make(name: str) -> click.Command:
    return Services.make_remote_command(name, None)


def test_unknown_name_raises_with_typed_name_and_available_list() -> None:
    with patch.object(Services, "fetch_remote_services", return_value=SERVED):
        with pytest.raises(click.ClickException, match=r"does not offer a service named 'NOPE'.*DEMO, EXAMPLE"):
            _make("NOPE")


def test_served_tag_builds_full_command() -> None:
    with patch.object(Services, "fetch_remote_services", return_value=SERVED):
        cmd = _make("EXAMPLE")
    assert cmd.help == "Example service."


def test_server_alias_resolves_to_server_tag() -> None:
    with patch.object(Services, "fetch_remote_services", return_value=SERVED):
        cmd = _make("alt")
    assert cmd.name == "DEMO"
    assert cmd.help == "Demo service."


def test_empty_service_list_raises_no_services_message() -> None:
    with patch.object(Services, "fetch_remote_services", return_value=[]):
        with pytest.raises(click.ClickException, match=r"offers no services to your API key"):
            _make("EXAMPLE")


def test_fetch_failure_keeps_stub_instead_of_raising() -> None:
    with patch.object(Services, "fetch_remote_services", return_value=None):
        cmd = _make("EXAMPLE")
    assert cmd.name == "EXAMPLE"
    assert cmd.help is None


def test_options_keep_the_served_name() -> None:
    with patch.object(Services, "fetch_remote_services", return_value=SERVED):
        cmd = _make("EXAMPLE")
    ctx = cmd.make_context("EXAMPLE", ["--zone", "eu", "--flavour", "b", "--loud", "an-id"])
    assert ctx.params == {"title": "an-id", "zone_code": "eu", "flavour_name": "b", "loud_mode": True}
