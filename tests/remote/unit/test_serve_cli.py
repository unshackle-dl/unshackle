"""Unit tests for the `unshackle serve` Click command flag surface."""

from __future__ import annotations

import re

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
        "--quiet",
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
    # strip ANSI: rich-click forces color under GITHUB_ACTIONS and styles --flags mid-sentence
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.output or str(result.exception))
    assert "Cannot use --api-only" in output


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


async def test_api_only_accepts_serve_users_keys(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, aiohttp_client
) -> None:
    """In api-only keyed mode the accepted keys are api_secret plus every `serve.users` key."""
    from aiohttp import web

    captured: dict = {}

    def capture_app(app, **kwargs):
        captured["app"] = app

    monkeypatch.setattr(web, "run_app", capture_app)
    from unshackle.core.config import config as cfg

    monkeypatch.setattr(
        cfg,
        "serve",
        {"api_secret": "master-key", "users": {"user-key": {"username": "alice", "services": ["EXAMPLE"]}}},
    )

    result = runner.invoke(serve, ["--api-only", "--remote-only"])
    assert result.exit_code == 0, result.output

    app = captured["app"]
    assert set(app["config"]["users"]) == {"master-key", "user-key"}
    assert app["config"]["users"]["user-key"]["username"] == "alice"

    client = await aiohttp_client(app)
    # an unrouted path still passes through the auth middleware first
    assert (await client.get("/api/unknown", headers={"X-Secret-Key": "user-key"})).status == 404
    assert (await client.get("/api/unknown", headers={"X-Secret-Key": "master-key"})).status == 404
    assert (await client.get("/api/unknown", headers={"X-Secret-Key": "nope"})).status == 401


async def test_api_only_coerces_non_str_user_keys(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, aiohttp_client
) -> None:
    """A yaml users key that parses as int must still authenticate (compare_digest needs str)."""
    from aiohttp import web

    captured: dict = {}
    monkeypatch.setattr(web, "run_app", lambda app, **kwargs: captured.update(app=app))
    from unshackle.core.config import config as cfg

    monkeypatch.setattr(cfg, "serve", {"api_secret": "master-key", "users": {123456: {"username": "bob"}}})

    result = runner.invoke(serve, ["--api-only", "--remote-only"])
    assert result.exit_code == 0, result.output
    assert "123456" in captured["app"]["config"]["users"]

    client = await aiohttp_client(captured["app"])
    assert (await client.get("/api/unknown", headers={"X-Secret-Key": "123456"})).status == 404
    assert (await client.get("/api/unknown", headers={"X-Secret-Key": "master-key"})).status == 404


def test_serve_without_no_key_requires_api_secret(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.config import config as cfg

    monkeypatch.setattr(cfg, "serve", {})  # no api_secret configured

    result = runner.invoke(serve, ["--api-only"])
    assert result.exit_code != 0
    assert "api_secret" in (result.output or "").lower() or "api_secret" in str(result.exception).lower()


def start_serve(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, serve_cfg: dict) -> tuple[bool, str]:
    """Start the command far enough to run the config checks.

    Returns whether it reached ``web.run_app`` and its plain-text output.
    """
    from aiohttp import web

    from unshackle.core.config import config as cfg

    started = []
    monkeypatch.setattr(web, "run_app", lambda *a, **kw: started.append(True))
    monkeypatch.setattr(cfg, "serve", dict(serve_cfg))
    result = runner.invoke(serve, ["--api-only", "--no-key"])
    return bool(started), re.sub(r"\x1b\[[0-9;]*m", "", result.output or str(result.exception))


@pytest.mark.parametrize(
    "serve_cfg, expected",
    [
        ({"tiers": ["bot"]}, "serve.tiers must be a mapping"),
        ({"tiers": {"bot": 600}}, "serve.tiers.bot must be a mapping"),
        ({"tiers": {"bot": {"rate_limit": 0}}}, "serve.tiers.bot.rate_limit"),
        ({"tiers": {"bot": {"rate_limit": True}}}, "serve.tiers.bot.rate_limit"),
        ({"users": {"k": {"username": "bot", "rate_limit": -1}}}, "serve.users.bot.rate_limit"),
        ({"users": {"k": {"username": "bot", "tier": "gone"}}}, "no such tier 'gone'"),
    ],
)
def test_bad_rate_limit_config_stops_the_server(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, serve_cfg: dict, expected: str
) -> None:
    """A tier or rate limit the reader would silently treat as "no limit" must fail at startup."""
    started, output = start_serve(runner, monkeypatch, serve_cfg)
    assert not started and expected in output


def test_good_rate_limit_config_starts(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    started, output = start_serve(
        runner,
        monkeypatch,
        {"tiers": {"bot": {"rate_limit": 600}}, "users": {"k": {"username": "bot", "tier": "bot"}}},
    )
    assert started, output
