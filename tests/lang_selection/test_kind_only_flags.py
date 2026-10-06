"""The kind-only flags: --audio-description-only and --forced-subs-only.

The filters in dl.result are not extractable without executing the whole download loop, so
their wiring is pinned against the source, the same way test_required_langs_gate does it.
"""

from __future__ import annotations

import inspect

import pytest

from unshackle.commands.dl import dl
from unshackle.core.api.handlers import validate_download_parameters
from unshackle.core.utilities import kind_only_conflict

KEYS = ["audio_description_only", "forced_subs_only"]
BASE = {"service": "EXAMPLE", "title": "x"}


@pytest.mark.parametrize(
    "args,only_key,base_key,expected",
    [
        (["-ado"], "audio_description_only", "audio_description", (True, False)),
        (["-ad"], "audio_description_only", "audio_description", (False, True)),
        ([], "audio_description_only", "audio_description", (False, False)),
        (["-fso"], "forced_subs_only", "forced_subs", (True, False)),
        (["-fs"], "forced_subs_only", "forced_subs", (False, True)),
    ],
)
def test_only_flag_does_not_parse_as_its_base_flag(args, only_key, base_key, expected):
    params = dl.cli.make_context("dl", args, resilient_parsing=True).params
    assert (params[only_key], params[base_key]) == expected


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"audio_description_only": True, "audio_only": True}, None),
        ({"audio_description_only": True}, None),
        ({"audio_description_only": True, "no_audio": True}, ("audio_description_only", "no_audio")),
        ({"audio_description_only": True, "video_only": True}, ("audio_description_only", "video_only")),
        ({"forced_subs_only": True, "subs_only": True}, None),
        ({"forced_subs_only": True, "no_subs": True}, ("forced_subs_only", "no_subs")),
        ({"forced_subs_only": True, "audio_only": True}, ("forced_subs_only", "audio_only")),
        ({"no_audio": True, "video_only": True}, None),
        # the *_only track-type flags combine: -A -S keeps audio, so -ado is not dropped
        ({"audio_description_only": True, "audio_only": True, "subs_only": True}, None),
        ({"forced_subs_only": True, "subs_only": True, "audio_only": True}, None),
        ({"audio_description_only": True, "subs_only": True}, ("audio_description_only", "subs_only")),
    ],
)
def test_kind_only_conflict(params, expected):
    assert kind_only_conflict(params) == expected


def test_api_rejects_a_conflict():
    assert validate_download_parameters({**BASE, "audio_description_only": True, "no_audio": True}) == (
        "Cannot use both audio_description_only and no_audio"
    )
    assert validate_download_parameters({**BASE, "audio_description_only": True, "audio_only": True}) is None


@pytest.mark.parametrize("key", [*KEYS, "audio_description", "audio_only"])
def test_api_rejects_a_non_boolean_flag(key):
    # the string "false" is truthy in dl.result, so it would turn the flag on
    assert validate_download_parameters({**BASE, key: "false"}) == f"{key} must be a boolean"
    assert validate_download_parameters({**BASE, key: None}) is None


class TestFilterWiring:
    source = inspect.getsource(dl.result)

    def test_ado_runs_when_the_title_has_no_separate_audio(self):
        # embedded audio is standard audio: the filter must sit before the `len(...) > 0` guard
        ado = self.source.index("if keep_audio and audio_description_only:")
        guard = self.source.index("if keep_audio and len(title.tracks.audio) > 0:")
        assert ado < guard

    def test_ado_does_not_count_embedded_audio(self):
        # both the --require-audio gate and the missing-language check
        assert self.source.count("keep_videos and not audio_description_only") == 2

    def test_only_filters_keep_one_kind(self):
        assert "title.tracks.select_audio(lambda x: x.descriptive)" in self.source
        assert "title.tracks.select_subtitles(lambda x: x.forced)" in self.source

    def test_fso_implies_forced_subs(self):
        # services read forced_subs from the ctx params to fetch forced tracks at all
        assert "if forced_s_lang or fsl_excl or forced_subs_only:" in self.source
