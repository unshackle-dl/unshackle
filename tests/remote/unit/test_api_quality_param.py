"""The REST download path converts ``quality`` and passes ``cdm_only`` like the CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import pytest

import unshackle.commands.dl as dl_module
from unshackle.core.api import download_manager, handlers
from unshackle.core.api.handlers import validate_download_parameters
from unshackle.core.services import Services

pytestmark = pytest.mark.unit


class _Captured(Exception):
    pass


@pytest.mark.parametrize("quality", ["1080p", ["1080"], [1080, 720], "1080,720"])
def test_the_api_accepts_the_quality_forms_the_cli_accepts(quality: Any) -> None:
    assert validate_download_parameters({"quality": quality}) is None


def test_a_rest_job_converts_quality_and_passes_cdm_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDl:
        cli = dl_module.dl.cli

        def __init__(self, ctx: click.Context, **kwargs: object) -> None:
            self.ctx = ctx

        def result(self, **kwargs: object) -> None:
            raise _Captured(self.ctx.params["quality"], kwargs["quality"], self.ctx.params["cdm_only"])

    class FakeService:
        cli = click.Command("svc", callback=lambda title: object())

    monkeypatch.setattr(dl_module, "dl", FakeDl)
    monkeypatch.setattr(Services, "get_path", staticmethod(lambda s: tmp_path))
    monkeypatch.setattr(Services, "load", staticmethod(lambda s: FakeService))
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *a: None)

    with pytest.raises(_Captured) as exc:
        download_manager.perform_download("job-1", "EXAMPLE1", "t1", {"quality": "1080p", "cdm_only": True})

    assert exc.value.args == ([1080], [1080], True)


def session_params(monkeypatch: pytest.MonkeyPatch, data: dict) -> dict:
    """The params a remote session's service reads from its parent ctx, with the service machinery stubbed."""
    seen: dict = {}

    def fake_instantiate(parent_ctx: click.Context, *args: object) -> object:
        seen["params"] = parent_ctx.params
        return object()

    monkeypatch.setattr(handlers, "load_service_config", lambda service: {})
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *args: None)
    monkeypatch.setattr(handlers.Services, "load", lambda service: None)
    monkeypatch.setattr(handlers, "instantiate_service", fake_instantiate)
    handlers.create_service_instance("EXAMPLE", "title", data, None, [], None)
    return seen["params"]


@pytest.mark.parametrize(("quality", "heights"), [("1080p", [1080]), (["2160", "342"], [2160, 342]), (None, [])])
def test_a_remote_session_gets_quality_as_heights(monkeypatch: pytest.MonkeyPatch, quality: Any, heights: list) -> None:
    assert session_params(monkeypatch, {"quality": quality})["quality"] == heights


@pytest.mark.parametrize("quality", ["tall", "0"])
def test_a_remote_session_refuses_a_malformed_quality(monkeypatch: pytest.MonkeyPatch, quality: str) -> None:
    with pytest.raises(handlers.APIError) as exc:
        session_params(monkeypatch, {"quality": quality})
    assert exc.value.error_code == handlers.APIErrorCode.INVALID_INPUT
