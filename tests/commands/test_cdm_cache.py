"""``dl.get_cdm`` builds each CDM once per run.

Regression: the callers ask for a CDM once per title, and the dedupe guard at the
call site compares object identity. No CDM class defines ``__eq__``, so the guard
was always true and every title paid a fresh build. A remote device costs an RSA
key pair and a blocking request to its host.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import pytest

import unshackle.core.cdm as cdm_mod
from unshackle.commands.dl import dl
from unshackle.core.cdm import loader
from unshackle.core.config import config


class StubCdm:
    """A distinct object per build, so a reused CDM is visible by identity."""

    def __init__(self, cdm_name: str, service_name: str) -> None:
        self.cdm_name = cdm_name
        self.service_name = service_name


@pytest.fixture
def builds(monkeypatch) -> list[tuple[str, str]]:
    """Record every ``load_cdm`` call and hand back a fresh stub for each one."""
    calls: list[tuple[str, str]] = []

    def fake_load_cdm(cdm_name: str, *, service_name: str = "", vaults: Optional[Any] = None) -> StubCdm:
        calls.append((cdm_name, service_name))
        return StubCdm(cdm_name, service_name)

    # The package resolves ``load_cdm`` lazily, so a ``setattr`` here would write the real
    # function into the package dict on undo and shadow that lookup for the whole process.
    monkeypatch.setitem(cdm_mod.__dict__, "load_cdm", fake_load_cdm)
    return calls


def make_dl() -> dl:
    # __new__ skips the CLI-driven __init__; get_cdm only needs these three.
    instance = dl.__new__(dl)
    instance.log = logging.getLogger("test_cdm_cache")
    instance.cdm_override = None
    instance.vaults = None
    return instance


def test_repeated_calls_build_the_cdm_once(builds, monkeypatch) -> None:
    monkeypatch.setattr(config, "cdm", {"EXAMPLE": "device_a"})
    runner = make_dl()

    first = runner.get_cdm("EXAMPLE", "default")
    for _ in range(4):
        assert runner.get_cdm("EXAMPLE", "default") is first

    assert builds == [("device_a", "EXAMPLE")]


def test_the_title_loop_guard_sees_the_same_object(builds, monkeypatch) -> None:
    """The call site keeps its CDM when ``get_cdm`` returns what it already holds."""
    monkeypatch.setattr(config, "cdm", {"EXAMPLE": "device_a"})
    runner = make_dl()

    held = runner.get_cdm("EXAMPLE", "default")
    for _ in range(3):
        quality_based_cdm = runner.get_cdm("EXAMPLE", "default", drm="widevine", quality=2160)
        assert not (quality_based_cdm and quality_based_cdm != held)

    assert len(builds) == 1


def test_a_different_service_gets_its_own_cdm(builds, monkeypatch) -> None:
    """A CDM is built with its ``service_name``, so two services must not share one."""
    monkeypatch.setattr(config, "cdm", {"EXAMPLE": "device_a", "OTHER": "device_a"})
    runner = make_dl()

    example = runner.get_cdm("EXAMPLE", "default")
    other = runner.get_cdm("OTHER", "default")

    assert example is not other
    assert builds == [("device_a", "EXAMPLE"), ("device_a", "OTHER")]


def test_a_different_profile_gets_its_own_cdm(builds, monkeypatch) -> None:
    monkeypatch.setattr(config, "cdm", {"EXAMPLE": {"one": "device_one", "two": "device_two"}})
    runner = make_dl()

    one = runner.get_cdm("EXAMPLE", "one")
    two = runner.get_cdm("EXAMPLE", "two")

    assert one is not two
    assert runner.get_cdm("EXAMPLE", "one") is one
    assert builds == [("device_one", "EXAMPLE"), ("device_two", "EXAMPLE")]


def test_quality_selection_reuses_the_device_it_resolves_to(builds, monkeypatch) -> None:
    """Two spellings of the same device share one CDM; a different device does not."""
    monkeypatch.setattr(config, "cdm", {"EXAMPLE": {">=1080": "device_hd", "<1080": "device_sd"}})
    runner = make_dl()

    hd = runner.get_cdm("EXAMPLE", "default", quality=2160)
    assert runner.get_cdm("EXAMPLE", "default", quality=1080) is hd
    sd = runner.get_cdm("EXAMPLE", "default", quality=720)

    assert sd is not hd
    assert builds == [("device_hd", "EXAMPLE"), ("device_sd", "EXAMPLE")]


def test_an_override_is_cached_under_the_name_it_resolves_to(builds, monkeypatch) -> None:
    monkeypatch.setattr(config, "cdm", {"EXAMPLE": "device_a"})
    runner = make_dl()
    runner.cdm_override = "device_b"

    override = runner.get_cdm("EXAMPLE", "default")

    assert runner.get_cdm("EXAMPLE", "default") is override
    assert builds == [("device_b", "EXAMPLE")]


@pytest.fixture
def leaked_shadow(monkeypatch) -> None:
    """Reproduce the residue that a ``setattr`` on the CDM package leaves behind.

    ``load_cdm`` reaches the package only through its lazy ``__getattr__``. A ``setattr`` on
    the package writes the real function into the package dict when it undoes, which shadows
    ``__getattr__`` for the rest of the process. Two remote tests do exactly that.
    """
    monkeypatch.setitem(cdm_mod.__dict__, "load_cdm", loader.load_cdm)


def test_a_shadowed_package_attribute_still_reaches_the_stub(leaked_shadow, builds, monkeypatch) -> None:
    """The stub intercepts ``get_cdm`` whatever an earlier test left on the CDM package."""
    monkeypatch.setattr(config, "cdm", {"EXAMPLE": "device_a"})
    runner = make_dl()

    cdm = runner.get_cdm("EXAMPLE", "default")

    assert isinstance(cdm, StubCdm)
    assert builds == [("device_a", "EXAMPLE")]
