from __future__ import annotations

import inspect
import logging

import click
import pytest
import yaml

from unshackle.commands.dl import dl
from unshackle.core.api import routes
from unshackle.core.api.handlers import validate_download_parameters
from unshackle.core.dl_options import (
    API_SHAPED_DL_PARAMS,
    DEFAULT_DOWNLOAD_PARAMS,
    DL_OPTION_ALIASES,
    DL_OPTIONS,
    FORWARDED_DL_PARAMS,
    cast_dl_params,
    forwarded_dl_params,
    remote_dl_params,
)
from unshackle.core.tracks import Audio, Subtitle, Video

CLI_ONLY = {DL_OPTION_ALIASES.get(name, name) for name, option in DL_OPTIONS.items() if option is None}


def test_the_table_decides_every_dl_option():
    """A new dl option fails here until DL_OPTIONS says whether the REST API takes it."""
    cli_names = {p.name for p in dl.cli.params}
    assert {DL_OPTION_ALIASES.get(name, name) for name in DL_OPTIONS} == cli_names
    assert set(cast_dl_params(dl.cli, {})) == cli_names - CLI_ONLY


def test_the_openapi_yaml_names_each_api_dl_option_and_no_other():
    """The download request body documents every dl option the API takes, beside its own request keys."""
    schema = yaml.safe_load((routes.download.__doc__ or "").split("---", 1)[1])
    properties = schema["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert DEFAULT_DOWNLOAD_PARAMS.keys() <= properties.keys()
    assert properties.keys() - DEFAULT_DOWNLOAD_PARAMS.keys() == {"service", "title_id", "remote", "client_name", "cdm"}


def test_the_server_reads_back_what_a_remote_client_forwards():
    params = {
        "lang": ["orig", "es-419"],
        "v_lang": [],
        "a_lang": ["en"],
        "s_lang": ["all"],
        "acodec": [Audio.Codec.EC3],
        "forced_subs": True,
        "exact_lang": True,
    }
    assert forwarded_dl_params({"dl_params": remote_dl_params(params)}) == params


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
    cast = cast_dl_params(
        dl.cli,
        {
            "quality": "1080p,720",
            "vcodec": "H265",
            "acodec": ["EC3"],
            "range": ["HDR10+", "dv"],
            "lang": ["en"],
            "slow": "20-40",
            "wanted": ["S01E01"],
            "downloads": 3,
            "sub_format": "srt",
            "output_dir": "",
            "cdm_only": False,
            "sample": "0-60",
            "tvdb_order": "dvd",
        },
    )
    assert cast["quality"] == [1080, 720]
    assert cast["vcodec"] == [Video.Codec.HEVC]
    assert cast["acodec"] == [Audio.Codec.EC3]
    assert cast["range_"] == [Video.Range.HDR10P, Video.Range.DV]
    assert cast["lang"] == ["en"]
    assert (cast["slow"], cast["wanted"]) == ("20-40", ["S01E01"])
    assert (cast["downloads"], cast["sample"], cast["tvdb_order"]) == (3, (0, 60), "dvd")
    assert cast["sub_format"] is Subtitle.Codec.SubRip
    assert (cast["output_dir"], cast["cdm_only"], cast["s_lang"]) == (None, False, ["all"])


def test_every_dl_option_reaches_dl_result():
    """An option outside the dl.__init__ and dl.result signatures is read from ctx.params elsewhere."""
    named = set(inspect.signature(dl.__init__).parameters) | set(inspect.signature(dl.result).parameters)
    assert {p.name for p in dl.cli.params} - named == {
        "server",
        "remote",
        "local_proxy",
        "reset_cache",
        "no_cache",
        "cdm_name",
        "import_file",
    }


def test_the_server_reads_the_forwarded_dl_params(caplog):
    data = {
        "dl_params": {"a_lang": "x", "acodec": ["ddp", "NOPE"], "exact_lang": 1, "forced_subs": True, "lang": ["en"]}
    }
    with caplog.at_level(logging.WARNING, logger="api"):
        assert list(forwarded_dl_params(data).items()) == [
            ("lang", ["en"]),
            ("v_lang", []),
            ("a_lang", []),
            ("s_lang", ["all"]),
            ("acodec", [Audio.Codec.EC3]),
            ("forced_subs", True),
            ("exact_lang", False),
        ]
    assert [(r.name, r.getMessage()) for r in caplog.records] == [
        ("api", "Ignoring dl_params.a_lang: it must be an array of strings"),
        ("api", "Ignoring unknown dl_params.acodec value: NOPE"),
    ]


@pytest.mark.parametrize(
    "parent_params,sent",
    [
        (
            {"lang": ["orig"], "v_lang": None, "a_lang": [], "s_lang": ["all"], "acodec": None, "forced_subs": 0},
            {"lang": ["orig"], "a_lang": [], "s_lang": ["all"], "forced_subs": False},
        ),
        ({"exact_lang": None, "quality": [1080]}, {"exact_lang": False}),
    ],
)
def test_the_client_sends_what_it_has_a_value_for(parent_params, sent):
    assert remote_dl_params(parent_params) == sent


def test_no_forwarded_option_has_an_alias():
    """remote_dl_params reads the API name from the click context, so an aliased option cannot be forwarded."""
    assert not {name for name, _ in FORWARDED_DL_PARAMS} & DL_OPTION_ALIASES.keys()


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
