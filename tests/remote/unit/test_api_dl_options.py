from __future__ import annotations

import click
import pytest

from unshackle.commands.dl import dl
from unshackle.core.api.handlers import (
    API_SHAPED_DL_PARAMS,
    DEFAULT_DOWNLOAD_PARAMS,
    cast_dl_params,
    validate_download_parameters,
)
from unshackle.core.tracks import Video

CLI_ONLY = {
    "select_titles",
    "list_",
    "list_titles",
    "import_file",
    "cdm_name",
    "remote",
    "server",
    "local_proxy",
    "postscript",
    "no_postscript",
    "merge_video",
    "real_video_bitrate",
    "real_audio_bitrate",
    "speed_limit",
}


def test_every_dl_option_is_an_api_param_or_cli_only():
    """A new dl option fails here until it is added to DEFAULT_DOWNLOAD_PARAMS or to CLI_ONLY."""
    api_names = set(cast_dl_params(dl.cli, {}))
    cli_names = {p.name for p in dl.cli.params}
    assert api_names - cli_names == set()
    assert cli_names - api_names == CLI_ONLY


def test_api_defaults_cast_to_the_dl_defaults():
    """An API-shaped option keeps its own empty default, which perform_download normalizes."""
    ctx = click.Context(dl.cli)
    expected = {
        p.name: p.type_cast_value(ctx, p.get_default(ctx))
        for p in dl.cli.params
        if p.name not in CLI_ONLY | API_SHAPED_DL_PARAMS
    }
    cast = cast_dl_params(dl.cli, {})
    assert {k: v for k, v in cast.items() if k not in API_SHAPED_DL_PARAMS} == expected


def test_cli_only_options_never_reach_dl():
    cast = cast_dl_params(dl.cli, {"postscript": ["rm -rf /"], "speed_limit": "1M", "list_": True})
    assert not CLI_ONLY & cast.keys()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("HDR10+", [Video.Range.HDR10P]),
        (["dv", "HDR10+"], [Video.Range.DV, Video.Range.HDR10P]),
        ([], [Video.Range.SDR]),
        (None, [Video.Range.SDR]),
    ],
)
def test_range_takes_the_api_spellings(value, expected):
    assert cast_dl_params(dl.cli, {"range": value})["range_"] == expected


def test_values_are_cast_by_the_dl_click_types():
    cast = cast_dl_params(dl.cli, {"quality": "1080p,720", "vcodec": "H265", "acodec": ["EC3"], "sample": True})
    assert cast["quality"] == [1080, 720]
    assert cast["vcodec"] == [Video.Codec.HEVC]
    assert [c.name for c in cast["acodec"]] == ["EC3"]
    assert cast["sample"] is not None


@pytest.mark.parametrize("key", ["lang", "v_lang", "a_lang", "s_lang", "forced_s_lang"])
@pytest.mark.parametrize("value", ["en", "en,fr", [1], {"x": "en"}])
def test_api_rejects_a_non_array_language_value(key, value):
    """A string would reach dl.result and split into one-character language codes."""
    assert validate_download_parameters({key: value}) == f"{key} must be an array of language strings"


@pytest.mark.parametrize(
    "data,error",
    [
        ({"tvdb_order": "nope"}, "Invalid tvdb_order"),
        ({"tag": ["a", "b"]}, "Invalid tag: expected a single value"),
        ({"proxy": {"host": "x"}}, "Invalid proxy: expected a single value"),
    ],
)
def test_api_refuses_a_value_the_dl_option_cannot_take(data, error):
    assert (validate_download_parameters(data) or "").startswith(error)


def test_no_dl_option_needs_a_parse_step_the_cast_skips():
    """type_cast_value runs no callback and no required check, so neither may appear on a dl option."""
    assert not [p.name for p in dl.cli.params if p.callback or p.required]


def test_null_takes_the_api_default():
    assert cast_dl_params(dl.cli, {key: None for key in DEFAULT_DOWNLOAD_PARAMS}) == cast_dl_params(dl.cli, {})


@pytest.mark.parametrize("key", ["downloads", "workers", "channels", "latest_episodes"])
def test_a_boolean_is_not_a_number(key):
    with pytest.raises(click.BadParameter, match=f"Invalid {key}: expected a number"):
        cast_dl_params(dl.cli, {key: True})


@pytest.mark.parametrize("value", [[1], [None], {"a": 1}])
def test_a_non_string_range_is_a_validation_error(value):
    assert (validate_download_parameters({"range": value}) or "").startswith("Invalid range")


def test_the_worker_keeps_the_code_of_an_api_error(tmp_path, monkeypatch):
    import json

    from unshackle.core.api import download_worker
    from unshackle.core.api.errors import APIError, APIErrorCode

    def refuse(*args, **kwargs):
        raise APIError(APIErrorCode.INVALID_PARAMETERS, "Invalid workers: expected a number, not a boolean")

    monkeypatch.setattr(download_worker, "perform_download", refuse)
    monkeypatch.setattr(download_worker, "relay_prompts", lambda callback: None)
    payload, result = tmp_path / "payload.json", tmp_path / "result.json"
    payload.write_text(json.dumps({"job_id": "j", "service": "EXAMPLE", "title_id": "t", "parameters": {}}))
    download_worker.main(["download_worker", str(payload), str(result)])
    assert json.loads(result.read_text())["error_code"] == "INVALID_PARAMETERS"
