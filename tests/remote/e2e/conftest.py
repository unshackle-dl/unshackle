"""E2E-specific fixtures.

These tests are skipped unless ``--live`` is passed on the pytest CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _load_scenarios(config: pytest.Config) -> dict:
    cached = getattr(config, "_e2e_scenarios_cache", None)
    if cached is not None:
        return cached

    import yaml

    path = Path(__file__).parent / "fixtures" / "fixtures.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    selected = (config.getoption("--services") or "").strip()
    services = (data or {}).get("services", {}) or {}
    if selected:
        wanted = {s.strip().upper() for s in selected.split(",") if s.strip()}
        services = {k: v for k, v in services.items() if k.upper() in wanted}
    cached = {"services": services}
    config._e2e_scenarios_cache = cached  # type: ignore[attr-defined]
    return cached


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "service_case" in metafunc.fixturenames:
        scenarios = _load_scenarios(metafunc.config)
        params = [pytest.param((tag, conf), id=tag) for tag, conf in (scenarios.get("services") or {}).items()]
        if not params:
            params = [
                pytest.param(("__none__", {}), id="no-services", marks=pytest.mark.skip(reason="no e2e fixtures"))
            ]
        metafunc.parametrize("service_case", params)
