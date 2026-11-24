"""Track selection through its interface: real tracks in, the kept tracks or the skip reason out."""

from __future__ import annotations

import logging
from dataclasses import fields
from typing import Any

import pytest

from unshackle.core.titles import Episode
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video
from unshackle.core.tracks.selection import Kept, Skip, TrackSelection


def selection(**flags: Any) -> TrackSelection:
    """A TrackSelection with every filter off but the given flags."""
    off: dict[str, Any] = {}
    for flag in fields(TrackSelection):
        off[flag.name] = [] if str(flag.type).startswith("list") else None if "Optional" in str(flag.type) else False
    return TrackSelection(**{**off, **flags})


class EXAMPLE:
    """The service class an Episode names."""


def video(id_: str = "v", language: str = "en", **kwargs: Any) -> Video:
    kwargs.setdefault("codec", Video.Codec.AVC)
    kwargs.setdefault("range_", Video.Range.SDR)
    return Video(id_=id_, url=f"https://cdn.example/{id_}", language=language, **kwargs)


def audio(id_: str = "a", language: str = "en", **kwargs: Any) -> Audio:
    return Audio(id_=id_, url=f"https://cdn.example/{id_}", language=language, codec=Audio.Codec.AAC, **kwargs)


def subtitle(id_: str = "s", language: str = "en", **kwargs: Any) -> Subtitle:
    return Subtitle(id_=id_, url=f"https://cdn.example/{id_}", language=language, codec=Subtitle.Codec.WebVTT, **kwargs)


def episode(*tracks: Any, language: str = "en") -> Episode:
    title = Episode(id_="show-s01-e001", service=EXAMPLE, title="Show", season=1, number=1, language=language)
    title.tracks = Tracks(list(tracks))
    return title


def standard(**extra: bool) -> Episode:
    """One video, one audio and one subtitle in English; ad adds a descriptive track, forced a forced subtitle."""
    tracks: list[Any] = [video(), audio(bitrate=128_000)]
    if extra.get("ad"):
        tracks.append(audio("ad", bitrate=1, descriptive=True))
    tracks.append(subtitle())
    if extra.get("forced"):
        tracks.append(subtitle("sf", forced=True))
    return episode(*tracks)


def kept(outcome: Kept | Skip) -> dict[str, list[str]]:
    assert isinstance(outcome, Kept), outcome
    return {
        "video": [t.id for t in outcome.tracks.videos],
        "audio": [t.id for t in outcome.tracks.audio],
        "subs": [t.id for t in outcome.tracks.subtitles],
    }


def test_the_defaults_keep_every_track() -> None:
    assert kept(selection().select(standard(), False)) == {"video": ["v"], "audio": ["a"], "subs": ["s"]}


def test_ado_keeps_the_descriptive_track_and_the_other_types() -> None:
    sel = selection(audio_description=True, audio_description_only=True)
    assert kept(sel.select(standard(ad=True, forced=True), False)) == {"video": ["v"], "audio": ["ad"], "subs": ["s"]}


def test_fso_keeps_the_forced_track_and_the_other_types() -> None:
    sel = selection(forced_subs=True, forced_subs_only=True)
    assert kept(sel.select(standard(ad=True, forced=True), False)) == {"video": ["v"], "audio": ["a"], "subs": ["sf"]}


def test_only_flags_with_their_type_flags_give_a_single_kind() -> None:
    sel = selection(audio_description=True, audio_description_only=True, audio_only=True)
    assert kept(sel.select(standard(ad=True, forced=True), False)) == {"video": [], "audio": ["ad"], "subs": []}
    sel = selection(forced_subs=True, forced_subs_only=True, subs_only=True)
    assert kept(sel.select(standard(ad=True, forced=True), False)) == {"video": [], "audio": [], "subs": ["sf"]}


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        ({"audio_description": True, "audio_description_only": True}, "There's no audio description track."),
        (
            {"forced_subs": True, "forced_subs_only": True},
            "There's no forced subtitle track for the selected languages.",
        ),
        ({"subs_only": True, "s_lang": ["fr"]}, "There's no track left for the selected options."),
    ],
)
def test_a_kind_only_miss_is_a_kind_only_skip(flags: dict[str, Any], reason: str) -> None:
    assert selection(**flags).select(standard(), False) == Skip(reason, kind_only=True)


def test_ado_runs_when_the_title_has_no_separate_audio() -> None:
    title = episode(video(data={"audio_language": "en"}))
    sel = selection(audio_description=True, audio_description_only=True)
    assert sel.select(title, False) == Skip("There's no audio description track.", kind_only=True)


def test_ado_does_not_count_audio_embedded_in_the_video() -> None:
    def title() -> Episode:
        return episode(video(data={"audio_language": "ko"}), audio("ad", bitrate=1, descriptive=True))

    assert kept(selection(audio_description=True, a_lang=["ko"]).select(title(), False))["video"] == ["v"]
    sel = selection(audio_description=True, audio_description_only=True, a_lang=["ko"])
    assert sel.select(title(), False) == Skip("ko not found in audio description tracks")


def test_a_missing_audio_language_skips_unless_best_available(caplog: pytest.LogCaptureFixture) -> None:
    def title() -> Episode:
        return episode(video(), audio("ja", "ja", bitrate=1), audio("en", "en", bitrate=1), language="ja")

    assert selection(a_lang=["orig", "ko", "en"]).select(title(), False) == Skip("ko not found in audio tracks")
    with caplog.at_level(logging.WARNING):
        outcome = selection(a_lang=["orig", "ko", "en"], best_available=True).select(title(), False)
    assert kept(outcome)["audio"] == ["ja", "en"]
    assert "ko not found in audio tracks, continuing with: orig, en" in caplog.text


def test_an_lcevc_layer_is_left_out_unless_asked_for() -> None:
    def title() -> Episode:
        return episode(video(height=540), video("lcevc", codec=Video.Codec.LCEVC, height=1080), audio())

    assert kept(selection().select(title(), False))["video"] == ["v"]
    assert kept(selection(vcodec=[Video.Codec.LCEVC]).select(title(), False))["video"] == ["lcevc"]


def test_a_failed_hybrid_falls_back_to_the_other_ranges_only_with_best_available() -> None:
    ranges = [Video.Range.HYBRID, Video.Range.SDR]
    failure = "HYBRID needs HDR10/HDR10+ and DV tracks, available ranges: SDR"
    assert selection(range_=ranges).select(standard(), False) == Skip(failure)
    outcome = selection(range_=ranges, best_available=True).select(standard(), False)
    assert isinstance(outcome, Kept)
    assert outcome.ranges == [Video.Range.SDR]
    assert kept(outcome)["video"] == ["v"]


def test_the_selection_can_run_on_every_title_of_a_run() -> None:
    sel = selection(no_subs=True, range_=[Video.Range.HYBRID, Video.Range.SDR], best_available=True)
    for _ in range(2):
        outcome = sel.select(standard(), False)
        assert isinstance(outcome, Kept)
        assert outcome.ranges == [Video.Range.SDR]
        assert kept(outcome)["subs"] == []
    assert sel.range_ == [Video.Range.HYBRID, Video.Range.SDR]


def test_a_service_without_subtitles_drops_them_and_their_requirement() -> None:
    assert kept(selection(require_subs=["ko"]).select(standard(), True)) == {"video": ["v"], "audio": ["a"], "subs": []}
