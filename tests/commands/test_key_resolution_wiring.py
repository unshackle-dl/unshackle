"""dl builds its content-key resolution from the run's flags and installs the verified decrypt as the hook."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unshackle.commands.dl import dl
from unshackle.core.drm import verify

pytestmark = pytest.mark.unit


def runner(written: list[tuple[Any, ...]]) -> dl:
    cmd = dl.__new__(dl)
    cmd.vaults = SimpleNamespace()
    cmd.service = "EXAMPLE"
    cmd.debug_logger = None
    cmd._remote_service = SimpleNamespace()
    cmd.write_export = lambda *args: written.append(args)  # type: ignore[method-assign]
    return cmd


@pytest.mark.parametrize("cdm_only, vaults_only, best_available", [(True, False, False), (False, True, True)])
def test_the_resolver_takes_the_run_flags_and_becomes_the_decrypt_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cdm_only: bool, vaults_only: bool, best_available: bool
) -> None:
    monkeypatch.setattr(verify, "DECRYPT_HOOK", None)
    written: list[tuple[Any, ...]] = []
    cmd = runner(written)
    service = SimpleNamespace()

    cmd.start_key_resolution(
        service,
        cdm_only=cdm_only,
        vaults_only=vaults_only,
        best_available=best_available,
        export=tmp_path / "export.json",
    )

    keys = cmd.keys
    assert verify.DECRYPT_HOOK == keys.decrypt_verified
    assert (keys.cdm_only, keys.vaults_only, keys.best_available) == (cdm_only, vaults_only, best_available)
    assert keys.source is cmd._remote_service and keys.service is service and keys.cdms is cmd
    assert keys.vaults is cmd.vaults and keys.tag == "EXAMPLE"
    assert keys.export is not None
    keys.export("title", "track", "drm")
    assert written == [(tmp_path / "export.json", "title", "track", "drm")]


def test_no_export_path_gives_no_export_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify, "DECRYPT_HOOK", None)
    cmd = runner([])

    cmd.start_key_resolution(SimpleNamespace(), cdm_only=False, vaults_only=True, best_available=False, export=None)

    assert cmd.keys.export is None
