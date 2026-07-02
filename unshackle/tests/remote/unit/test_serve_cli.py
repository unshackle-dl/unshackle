"""Unit tests for the `unshackle serve` Click command flag surface."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from unshackle.commands.serve import serve

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_serve_help_lists_documented_flags(runner: CliRunner) -> None:
    result = runner.invoke(serve, ["--help"])
    assert result.exit_code == 0
    out = result.output
    for flag in (
        "--host",
        "--port",
        "--caddy",
        "--api-only",
        "--no-widevine",
        "--no-playready",
        "--no-key",
        "--debug-api",
        "--debug",
        "--remote-only",
    ):
        assert flag in out, f"missing flag in --help: {flag}"


def test_serve_api_only_with_no_widevine_rejected(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--api-only` is mutually exclusive with `--no-widevine`/`--no-playready`."""
    monkeypatch.setenv("UNSHACKLE_NO_RUN", "1")  # belt-and-braces; not currently checked

    # Stub web.run_app to avoid actually starting the server if validation passes.
    from aiohttp import web

    monkeypatch.setattr(web, "run_app", lambda *a, **kw: None)

    # Force a clean config.serve so no_key path doesn't blow up loading wvds.
    from unshackle.core.config import config as cfg

    monkeypatch.setattr(cfg, "serve", {"api_secret": "x"})

    result = runner.invoke(serve, ["--api-only", "--no-widevine", "--no-key"])
    assert result.exit_code != 0
    assert "Cannot use --api-only" in (result.output or str(result.exception))


def test_serve_no_key_without_api_secret_does_not_require_secret(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --no-key, the missing api_secret check is bypassed."""
    from aiohttp import web

    monkeypatch.setattr(web, "run_app", lambda *a, **kw: None)
    from unshackle.core.config import config as cfg

    monkeypatch.setattr(cfg, "serve", {})

    result = runner.invoke(serve, ["--api-only", "--no-key", "--remote-only"])
    # No exception should escape, exit code 0 means startup proceeded then run_app stub returned.
    assert result.exit_code == 0, result.output


def test_serve_without_no_key_requires_api_secret(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.config import config as cfg

    monkeypatch.setattr(cfg, "serve", {})  # no api_secret configured

    result = runner.invoke(serve, ["--api-only"])
    assert result.exit_code != 0
    assert "api_secret" in (result.output or "").lower() or "api_secret" in str(result.exception).lower()
