"""make_remote_command resolves names against the remote server's list and reports the failures."""

from __future__ import annotations

from unittest.mock import patch

import click
import pytest

from unshackle.core.services import Services

SERVED = [
    {"tag": "AMZN", "url": "https://example.com", "help": "Amazon.", "cli_params": [], "aliases": []},
    {"tag": "HMAX", "url": "https://example.com", "help": "Max.", "cli_params": [], "aliases": ["MAX", "hbomax"]},
]


def _make(name: str) -> click.Command:
    return Services.make_remote_command(name, ctx=click.Context(click.Command("dl")))


def test_unknown_name_raises_with_typed_name_and_available_list() -> None:
    with patch.object(Services, "fetch_remote_services", return_value=SERVED):
        with pytest.raises(click.ClickException, match=r"does not offer a service named 'BBC'.*AMZN, HMAX"):
            _make("BBC")


def test_served_tag_builds_full_command() -> None:
    with patch.object(Services, "fetch_remote_services", return_value=SERVED):
        cmd = _make("AMZN")
    assert cmd.help == "Amazon."


def test_server_alias_resolves_to_server_tag() -> None:
    with patch.object(Services, "fetch_remote_services", return_value=SERVED):
        cmd = _make("max")
    assert cmd.name == "HMAX"
    assert cmd.help == "Max."


def test_empty_service_list_raises_no_services_message() -> None:
    with patch.object(Services, "fetch_remote_services", return_value=[]):
        with pytest.raises(click.ClickException, match=r"offers no services to your API key"):
            _make("AMZN")


def test_fetch_failure_keeps_stub_instead_of_raising() -> None:
    with patch.object(Services, "fetch_remote_services", return_value=None):
        cmd = _make("AMZN")
    assert cmd.name == "AMZN"
    assert cmd.help is None
