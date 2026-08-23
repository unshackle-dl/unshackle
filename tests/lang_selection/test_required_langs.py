from __future__ import annotations

import inspect

import pytest

from unshackle.commands.dl import dl
from unshackle.core.api.handlers import DEFAULT_DOWNLOAD_PARAMS
from unshackle.core.utilities import missing_required_langs


@pytest.mark.parametrize(
    "required,available,expected",
    [
        # -l en,fr with --require-audio en: fr may be absent, en may not
        (["en"], ["en", "de"], []),
        (["en"], ["fr", "de"], ["en"]),
        (["en", "fr"], ["en"], ["fr"]),
        (["en", "fr"], [], ["en", "fr"]),
        ([], ["de"], []),
        # regional and script variants satisfy the base tag
        (["en"], ["en-US"], []),
        (["es"], ["es-419"], []),
        (["zh"], ["zh-Hans"], []),
        # tracks with no language tag satisfy nothing
        (["en"], [None], ["en"]),
        # a bare exclusion asks for absence, so nothing is required
        (["-fr"], ["en"], []),
    ],
)
def test_missing_required_langs(required, available, expected):
    assert missing_required_langs(required, available) == expected


def test_exclusion_token_is_not_a_required_language():
    # '-fr' must not be read as a language named "-fr", which could never match
    assert missing_required_langs(["en", "-fr"], ["en", "ja"]) == []
    assert missing_required_langs(["en", "-fr"], ["ja"]) == ["en"]


def test_orig_resolves_to_the_title_language():
    assert missing_required_langs(["orig"], ["ja"], "ja") == []
    assert missing_required_langs(["orig"], ["en"], "ja") == ["ja"]
    # an unresolvable 'orig' requires nothing rather than failing every title
    assert missing_required_langs(["orig"], ["en"], None) == []


def test_exact_lang_separates_a_regional_variant_from_its_base():
    assert missing_required_langs(["en-US"], ["en-US"], exact=True) == []
    assert missing_required_langs(["en-US"], ["en-GB"], exact=True) == ["en-US"]
    assert missing_required_langs(["en-US"], ["en-GB"]) == []


def test_all_and_best_tokens_require_nothing():
    # they mean "whatever exists", so they can never be missing
    assert missing_required_langs(["all"], []) == []
    assert missing_required_langs(["best"], []) == []


@pytest.mark.parametrize("flag", ["--require-audio", "--require-video", "--best-available", "--warn-only"])
def test_flag_is_registered_on_the_dl_command(flag):
    assert any(flag in param.opts for param in dl.cli.params)


@pytest.mark.parametrize(
    "flag,expected_param",
    [
        ("--require-audio", "require_audio"),
        ("--require-video", "require_video"),
        ("--warn-only", "best_available"),
        ("--best-available", "best_available"),
    ],
)
def test_flag_maps_to_its_param_name(flag, expected_param):
    param = next(p for p in dl.cli.params if flag in p.opts)
    assert param.name == expected_param


@pytest.mark.parametrize("flag", ["--require-audio", "--require-video"])
def test_require_flags_parse_a_comma_list(flag):
    param = next(p for p in dl.cli.params if flag in p.opts)
    assert param.default == []
    assert param.type.convert("en,ja", param, None) == ["en", "ja"]


@pytest.mark.parametrize("key", ["require_audio", "require_video"])
def test_api_passes_the_flags_through(key):
    # the REST API bypasses click, so the default and the dl.result param must both exist
    assert DEFAULT_DOWNLOAD_PARAMS[key] == []
    assert key in inspect.signature(dl.result).parameters


def test_api_accepts_the_require_flags():
    from unshackle.core.api.handlers import validate_download_parameters

    # unlike require_subs, these carry no conflict with a language selection
    assert (
        validate_download_parameters(
            {"service": "EXAMPLE", "title": "x", "require_audio": ["en"], "require_video": ["en"], "lang": ["en", "fr"]}
        )
        is None
    )


@pytest.mark.parametrize("key", ["require_audio", "require_video"])
def test_api_reaches_dl_result(key, monkeypatch):
    # download_manager builds the dl.result kwargs by hand, so a missing key is a silent drop
    import inspect as _inspect

    from unshackle.core.api import download_manager

    source = _inspect.getsource(download_manager)
    assert f'{key}=params.get("{key}", [])' in source


@pytest.mark.parametrize(
    "value",
    ["en,fr", "en", {"lang": "en"}, [1], ["en", None]],
)
@pytest.mark.parametrize("key", ["require_audio", "require_video"])
def test_api_rejects_a_non_array_require_value(key, value):
    # a JSON string iterates character by character and blows up in langcodes, so it must 400 first
    from unshackle.core.api.handlers import validate_download_parameters

    error = validate_download_parameters({"service": "EXAMPLE", "title": "x", key: value})
    assert error == f"{key} must be an array of language strings"


def test_require_subs_no_longer_conflicts_with_s_lang():
    # gate and selection are separate concerns: -sl decides what to keep, --require-subs only gates
    from unshackle.core.api.handlers import validate_download_parameters

    assert (
        validate_download_parameters(
            {"service": "EXAMPLE", "title": "x", "s_lang": ["en", "ko", "ja"], "require_subs": ["en"]}
        )
        is None
    )


def test_all_three_require_flags_share_one_gate():
    import inspect as _inspect

    from unshackle.commands import dl as dl_module

    source = _inspect.getsource(dl_module.dl.result)
    # the sub gate must not short-circuit the s_lang branch any more
    assert "downloading all available subtitles" not in source
    for kind in ("audio", "video", "subtitle"):
        assert f'"{kind}",' in source
    assert "missing_required_langs(required, available, title.language, exact=exact_lang)" in source
