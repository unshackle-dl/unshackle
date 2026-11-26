"""The kind-only flags: --audio-description-only and --forced-subs-only.

Their filters are tested through TrackSelection in test_track_selection.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.admission import refusal
from tests.lang_selection import test_kind_only_selection as harness
from unshackle.commands.dl import dl
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
    assert refusal({**BASE, "audio_description_only": True, "no_audio": True}) == (
        "--audio-description-only cannot be used with --no-audio: --no-audio drops the tracks that "
        "--audio-description-only selects."
    )
    assert refusal({**BASE, "audio_description_only": True, "audio_only": True}) is None


@pytest.mark.parametrize("key", [*KEYS, "audio_description", "audio_only"])
def test_api_rejects_a_non_boolean_flag(key):
    # the string "false" is truthy in dl.result, so it would turn the flag on
    assert refusal({**BASE, key: "false"}) == f"{key} must be a boolean"
    assert refusal({**BASE, key: None}) is None


@pytest.mark.parametrize("flags", [{"forced_subs_only": True}, {"forced_s_lang": ["en"]}, {"forced_s_lang": ["-en"]}])
def test_a_forced_flag_turns_forced_subs_on_for_the_service(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, flags: dict[str, Any]
) -> None:
    # services read forced_subs from the ctx params to fetch forced tracks at all
    service = harness.FakeService({1: {"forced": True}})
    params = {"forced_subs": False}
    service.ctx = SimpleNamespace(parent=SimpleNamespace(params=params))  # type: ignore[attr-defined]
    harness.run(monkeypatch, caplog, {}, service=service, **flags)
    assert params["forced_subs"] is True
