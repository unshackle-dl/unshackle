"""Tests for ``apply_service_dl_overrides`` — precedence: CLI/env > service dl > global dl > defaults."""

from __future__ import annotations

import logging
from typing import Any, Optional

import click
import pytest
from click.core import ParameterSource

from unshackle.commands.dl import apply_service_dl_overrides, normalize_dl_config
from unshackle.core.tracks import Video
from unshackle.core.utils.click_types import LANGUAGE_RANGE, MultipleChoice

log = logging.getLogger("test_service_dl_overrides")


def make_ctx(args: Optional[list[str]] = None, default_map: Optional[dict[str, Any]] = None) -> click.Context:
    """Build a parsed context for a mirror of the dl group's option shapes."""

    @click.group("dl", invoke_without_command=True)
    @click.option("-vl", "--v-lang", type=LANGUAGE_RANGE, default=[])
    @click.option(
        "-r", "--range", "range_", type=MultipleChoice(Video.Range, case_sensitive=False), default=[Video.Range.SDR]
    )
    @click.option("--repack", is_flag=True, default=False)
    @click.option("--workers", type=int, default=None, envvar="TEST_DL_WORKERS")
    def cmd(**__: Any) -> None:
        pass

    return cmd.make_context("dl", args or [], default_map=default_map)


def test_service_only_key_applies():
    """The reported bug: key set only under services.<TAG>.dl, not under global dl:."""
    ctx = make_ctx()
    assert ctx.params["v_lang"] == []
    apply_service_dl_overrides(ctx, {"v_lang": ["en"]}, log)
    assert ctx.params["v_lang"] == ["en"]


def test_cli_value_beats_service_config():
    ctx = make_ctx(["-vl", "fr"])
    apply_service_dl_overrides(ctx, {"v_lang": ["en"]}, log)
    assert ctx.params["v_lang"] == ["fr"]


def test_env_value_beats_service_config(monkeypatch):
    monkeypatch.setenv("TEST_DL_WORKERS", "5")
    ctx = make_ctx()
    apply_service_dl_overrides(ctx, {"workers": 9}, log)
    assert ctx.params["workers"] == 5


def test_service_config_beats_global_default_map():
    ctx = make_ctx(default_map={"v_lang": "de"})
    assert ctx.params["v_lang"] == ["de"]
    apply_service_dl_overrides(ctx, {"v_lang": ["en"]}, log)
    assert ctx.params["v_lang"] == ["en"]


def test_flag_global_true_service_false():
    ctx = make_ctx(default_map={"repack": True})
    assert ctx.params["repack"] is True
    apply_service_dl_overrides(ctx, {"repack": False}, log)
    assert ctx.params["repack"] is False


def test_range_scalar_converts_to_enum_list():
    ctx = make_ctx()
    apply_service_dl_overrides(ctx, {"range_": "HDR10"}, log)
    assert ctx.params["range_"] == [Video.Range.HDR10]


def test_range_alias_applies():
    """`range` in config is an accepted alias for the `range_` parameter."""
    ctx = make_ctx()
    apply_service_dl_overrides(ctx, {"range": "HDR10"}, log)
    assert ctx.params["range_"] == [Video.Range.HDR10]


def test_normalize_dl_config_maps_aliases():
    assert normalize_dl_config({"range": "SDR", "list": True, "v_lang": ["en"]}) == {
        "range_": "SDR",
        "list_": True,
        "v_lang": ["en"],
    }


def test_unknown_key_warns(caplog):
    ctx = make_ctx()
    with caplog.at_level(logging.WARNING, logger=log.name):
        apply_service_dl_overrides(ctx, {"bogus": 1}, log)
    assert any("unknown dl option 'bogus'" in r.message for r in caplog.records)


def test_none_value_is_skipped():
    ctx = make_ctx()
    apply_service_dl_overrides(ctx, {"v_lang": None}, log)
    assert ctx.params["v_lang"] == []


def test_invalid_value_warns_and_keeps_current(caplog):
    ctx = make_ctx()
    with caplog.at_level(logging.WARNING, logger=log.name):
        apply_service_dl_overrides(ctx, {"workers": "abc"}, log)
    assert ctx.params["workers"] is None
    assert any("Failed to apply service dl override 'workers'" in r.message for r in caplog.records)


def hand_built_ctx(body: dict[str, Any]) -> click.Context:
    """Mirror the serve path: ctx.params set directly, sources recorded manually."""
    parsed = make_ctx()
    ctx = click.Context(parsed.command)
    ctx.params = {"repack": body.get("repack", False)}
    for name in ctx.params:
        ctx.set_parameter_source(
            name, ParameterSource.COMMANDLINE if name in body else ParameterSource.DEFAULT
        )
    return ctx


def test_hand_built_ctx_default_gets_override():
    ctx = hand_built_ctx({})
    apply_service_dl_overrides(ctx, {"repack": True}, log)
    assert ctx.params["repack"] is True


def test_hand_built_ctx_client_value_wins():
    ctx = hand_built_ctx({"repack": False})
    apply_service_dl_overrides(ctx, {"repack": True}, log)
    assert ctx.params["repack"] is False


def test_hand_built_ctx_missing_param_is_skipped():
    """Known dl option absent from a hand-built context must not raise or be injected."""
    ctx = hand_built_ctx({})
    apply_service_dl_overrides(ctx, {"v_lang": ["en"]}, log)
    assert "v_lang" not in ctx.params


@pytest.mark.parametrize(
    "value,expected",
    [
        (["en", "de"], ["en", "de"]),
        ("en,de", ["en", "de"]),
        ("en", ["en"]),
    ],
)
def test_language_range_yaml_shapes(value, expected):
    ctx = make_ctx()
    apply_service_dl_overrides(ctx, {"v_lang": value}, log)
    assert ctx.params["v_lang"] == expected
