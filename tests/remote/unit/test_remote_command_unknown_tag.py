"""make_remote_command resolves names against the remote server's list and reports the failures."""

from __future__ import annotations

from unittest.mock import patch

import click
import pytest

from unshackle.core.services import Services

SERVED = [
    {"tag": "EXAMPLE", "url": "https://example.com", "help": "Example service.", "cli_params": [], "aliases": []},
    {
        "tag": "DEMO",
        "url": "https://example.com",
        "help": "Demo service.",
        "cli_params": [],
        "aliases": ["ALT", "alt-demo"],
    },
]


def _make(name: str) -> click.Command:
    return Services.make_remote_command(name, ctx=click.Context(click.Command("dl")))


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
