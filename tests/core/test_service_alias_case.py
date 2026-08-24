"""Service tags resolve from an alias in any case, and a real tag beats another service's alias."""

from pathlib import Path

import pytest


@pytest.fixture
def two_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """SVCA claims "SVCB" as an alias, but SVCB is a service in its own right."""
    from unshackle.core import services

    monkeypatch.setattr(services, "SERVICES", [Path("SVCA") / "__init__.py", Path("SVCB") / "__init__.py"])
    monkeypatch.setattr(services, "ALIASES", {"SVCA": ("SVCX", "SVCB"), "SVCB": ()})


@pytest.mark.parametrize("value", ["SVCX", "svcx", "SvCx"])
def test_alias_resolves_in_any_case(two_services: None, value: str) -> None:
    from unshackle.core.services import Services

    assert Services.get_tag(value) == "SVCA"


@pytest.mark.parametrize("value", ["SVCB", "svcb", "SvCb"])
def test_real_tag_beats_another_services_alias(two_services: None, value: str) -> None:
    from unshackle.core.services import Services

    assert Services.get_tag(value) == "SVCB"


def test_unknown_value_is_returned_unchanged(two_services: None) -> None:
    from unshackle.core.services import Services

    assert Services.get_tag("NOPE") == "NOPE"
