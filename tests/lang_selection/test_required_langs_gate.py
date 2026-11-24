"""The required-language gate of the track selection, and inputs the unit tests skip.

The gate runs in TrackSelection.select, so its tests cross that interface with real tracks.
The value inputs cover what the helper's own tests leave out: real tracks carry
langcodes.Language objects, not strings.
"""

from __future__ import annotations

from typing import Any

import pytest
from langcodes import Language

from tests.lang_selection import test_kind_only_selection as harness
from tests.lang_selection.test_track_selection import audio, episode, selection, standard, video
from unshackle.core.tracks.selection import Kept, Skip
from unshackle.core.utilities import missing_required_langs


def test_language_objects_satisfy_a_requirement():
    # track.language is a langcodes.Language, never a str, at the real call site
    assert missing_required_langs(["en"], [Language.get("en")]) == []
    assert missing_required_langs(["en"], [Language.get("en-US")]) == []
    assert missing_required_langs(["en"], [Language.get("fr")]) == ["en"]


def test_orig_accepts_a_language_object_title_language():
    # title.language is a langcodes.Language at the call site, never a str
    assert missing_required_langs(["orig"], ["ja"], Language.get("ja")) == []
    assert missing_required_langs(["orig"], ["en"], Language.get("ja")) == ["ja"]


def test_requirement_checks_the_catalogue_not_the_selection():
    # -l best,-fr --require-audio en: the gate answers "does en exist", so the
    # exclusion of fr from the *selection* must not make en count as missing
    assert missing_required_langs(["en"], ["en", "ko", "ja", "fr"]) == []
    # requiring a language the selection excludes is still satisfied by the catalogue
    assert missing_required_langs(["fr"], ["en", "ko", "ja", "fr"]) == []


def test_the_gate_is_disabled_with_the_track_kind() -> None:
    # --no-audio / -V must not fail on a requirement the download cannot honour
    assert isinstance(selection(require_audio=["ko"], no_audio=True).select(standard(), False), Kept)
    assert isinstance(selection(require_video=["ko"], audio_only=True).select(standard(), False), Kept)


def test_the_gate_counts_audio_embedded_in_a_kept_video() -> None:
    def title() -> Any:
        return episode(video(data={"audio_language": "ko"}), audio())

    assert isinstance(selection(require_audio=["ko"]).select(title(), False), Kept)
    missing = Skip("Required audio language(s) not found: ko")
    assert selection(require_audio=["ko"], audio_only=True).select(title(), False) == missing


def test_ado_does_not_count_audio_embedded_in_the_video_at_the_gate() -> None:
    title = episode(video(data={"audio_language": "ko"}), audio("ad", bitrate=1, descriptive=True))
    sel = selection(require_audio=["ko"], audio_description=True, audio_description_only=True)
    assert sel.select(title, False) == Skip("Required audio language(s) not found: ko")


def test_a_run_with_every_required_language_present_keeps_the_title() -> None:
    sel = selection(require_audio=["en"], require_video=["en"], require_subs=["en"])
    assert isinstance(sel.select(standard(), False), Kept)


def test_the_gate_honours_exact_lang() -> None:
    def title() -> Any:
        return episode(video(), audio(language="en-GB"))

    assert isinstance(selection(require_audio=["en"]).select(title(), False), Kept)
    missing = Skip("Required audio language(s) not found: en")
    assert selection(require_audio=["en"], exact_lang=True).select(title(), False) == missing


def test_the_gate_reads_the_tracks_before_any_filter() -> None:
    title = episode(video(), audio("en", "en", bitrate=1), audio("ko", "ko", bitrate=1))
    outcome = selection(a_lang=["en"], require_audio=["ko"]).select(title, False)
    assert isinstance(outcome, Kept)
    assert [a.id for a in outcome.tracks.audio] == ["en"]


def test_every_kind_misses_in_one_reason() -> None:
    sel = selection(require_audio=["ko"], require_video=["ko"], require_subs=["ko"])
    assert sel.select(standard(), False) == Skip(
        "Required audio language(s) not found: ko; Required video language(s) not found: ko; "
        "Required subtitle language(s) not found: ko"
    )


@pytest.mark.parametrize(
    ("flags", "clash"),
    [
        ({"require_audio": ["en"], "lang": ["all", "-en"]}, "--require-audio: en"),
        ({"require_video": ["en"], "v_lang": ["all", "-en"]}, "--require-video: en"),
        ({"require_subs": ["en"], "s_lang": ["all", "-en"]}, "--require-subs: en"),
        ({"require_audio": ["-en"]}, "--require-audio: en"),
    ],
)
def test_a_required_and_excluded_language_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, flags: dict[str, Any], clash: str
) -> None:
    assert harness.run(monkeypatch, caplog, {1: {}}, **flags) == 1
    assert f"{clash} cannot be required and excluded at the same time." in caplog.text


def test_a_language_override_does_not_inherit_the_lang_exclusions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # -al and -vl name languages, so they replace -l and its -en
    flags: dict[str, Any] = {"require_audio": ["en"], "a_lang": ["en"], "v_lang": ["all"], "lang": ["all", "-en"]}
    ids = harness.run(monkeypatch, caplog, {1: {}}, **flags)
    assert ids["audio"] == ["a"]
