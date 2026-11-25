"""The run plan: one check of the dl options for the CLI and the REST download job."""

from __future__ import annotations

from typing import Any

import click
import pytest
from click.core import ParameterSource

from unshackle.commands.dl import dl
from unshackle.core.api.errors import categorize_exception
from unshackle.core.remote_protocol import APIErrorCode
from unshackle.core.run_plan import PlanError, api_plan, bitrate_range, check_options
from unshackle.core.tracks import Video


def cli_params(*args: str) -> dict[str, Any]:
    """The dl params a CLI parse of ``args`` gives, with the service subcommand left out."""
    ctx = dl.cli.make_context("dl", [*args, "EXAMPLE"], resilient_parsing=True)
    return ctx.params


@pytest.mark.parametrize(
    ("args", "data", "message"),
    [
        (["--sample", "--include-sample"], {"sample": True, "include_sample": True}, "--sample and --include-sample"),
        (["--vbitrate", "5000", "--vbitrate-range", "1-2"], {"vbitrate": 5000, "vbitrate_range": "1-2"}, "--vbitrate"),
        (["--abitrate-range", "1-2-3"], {"abitrate_range": "1-2-3"}, "Invalid --abitrate-range"),
        (["-l", "all,-all"], {"lang": ["all", "-all"]}, "'all' cannot be excluded"),
        (["--require-audio", "-en"], {"require_audio": ["-en"]}, "--require-audio: en cannot be required"),
        (["-ado", "--no-audio"], {"audio_description_only": True, "no_audio": True}, "--audio-description-only"),
        (["--enrich"], {"enrich": True}, "--enrich requires"),
        (["-S", "--no-subs"], {"subs_only": True, "no_subs": True}, "--subs-only cannot be used with --no-subs"),
        (["-V", "-A"], {"video_only": True, "audio_only": True}, None),
        (["--tvdb-order", "official"], {"tvdb_order": "absolute", "tvdb_id": 1}, None),
    ],
)
def test_the_cli_and_the_api_refuse_the_same_run(args: list[str], data: dict, message: str | None) -> None:
    """Both front doors go through check_options, so a run dl refuses is a submit-time refusal over the API."""
    if message is None:
        check_options(cli_params(*args))
        return
    with pytest.raises(PlanError, match=message):
        check_options(cli_params(*args))
    with pytest.raises(PlanError, match=message):
        api_plan(data)


def test_select_titles_and_wanted_are_refused() -> None:
    with pytest.raises(PlanError, match="--select-titles"):
        check_options({"select_titles": True, "wanted": ["1x1"]})


def test_hybrid_needs_dovi_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    import unshackle.core.binaries as binaries

    monkeypatch.setattr(binaries, "DoviTool", None)
    with pytest.raises(PlanError, match="dovi_tool"):
        check_options({"range_": [Video.Range.HDR10], "generate_dv": True})
    check_options({"range_": [Video.Range.HDR10]})


def test_a_plan_error_exits_1_without_a_usage_block() -> None:
    assert PlanError("x").exit_code == 1
    assert not isinstance(PlanError("x"), click.UsageError)


def test_a_plan_error_is_an_invalid_parameters_api_error() -> None:
    error = categorize_exception(PlanError("--sample and --include-sample cannot be used together."))
    assert error.error_code is APIErrorCode.INVALID_PARAMETERS
    assert error.message == "--sample and --include-sample cannot be used together."


@pytest.mark.parametrize(("value", "expected"), [("6000-7000", (6000, 7000)), ("7000-6000", (6000, 7000)), ("", None)])
def test_bitrate_range_orders_its_ends(value: str, expected: Any) -> None:
    assert bitrate_range(value, "--vbitrate-range", "6000-7000") == expected


def test_the_api_plan_shapes_wanted_and_slow() -> None:
    plan = api_plan({"wanted": "S01E01, S01E02", "slow": [20, 40]})
    assert sorted(plan.params["wanted"]) == ["1x1", "1x2"]
    assert plan.params["slow"] == (20, 40)
    assert api_plan({"slow": True}).params["slow"] == (60, 120)
    with pytest.raises(PlanError, match="slow"):
        api_plan({"slow": [1, 2]})
    with pytest.raises(PlanError, match="wanted"):
        api_plan({"wanted": [1]})


def test_the_api_plan_records_where_each_value_came_from() -> None:
    plan = api_plan({"workers": 4, "quality": [1080], "cdm": "dev", "remote": "box"}, client_keys={"workers", "cdm"})
    assert plan.sources["workers"] is ParameterSource.COMMANDLINE
    assert plan.sources["cdm_name"] is ParameterSource.COMMANDLINE
    assert plan.sources["quality"] is ParameterSource.DEFAULT
    assert plan.sources["server"] is ParameterSource.DEFAULT
    assert (plan.params["cdm_name"], plan.params["remote"], plan.params["server"]) == ("dev", True, "box")
    ctx = plan.context(dl.cli, "EXAMPLE")
    assert ctx.invoked_subcommand == "EXAMPLE"
    assert ctx.get_parameter_source("workers") is ParameterSource.COMMANDLINE
    assert ctx.params == plan.params


def test_dl_checks_the_plan_before_the_service_loads() -> None:
    with pytest.raises(PlanError, match="--sample and --include-sample"):
        dl.cli.main(["--sample", "--include-sample", "EXAMPLE", "t"], standalone_mode=False)
