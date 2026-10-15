"""Run regional language requests through the real dl.result() selection loop."""

from __future__ import annotations

from typing import Any

import pytest
from langcodes import Language

from tests.lang_selection import test_kind_only_selection as harness
from unshackle.core.titles import Episode
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video


class RegionalService(harness.FakeService):
    """One episode in `title_lang`, with one video, one audio track per entry of `audio` and the given subtitles."""

    def __init__(self, title_lang: str, audio: list[str], subs: list[tuple[str, str]]) -> None:
        super().__init__({1: {}})
        self.title_lang, self.audio, self.subs = title_lang, audio, subs

    def get_titles_cached(self) -> Any:
        title = super().get_titles_cached()
        for episode in title:
            episode.language = Language.get(self.title_lang)
        return title

    def get_tracks(self, title: Episode) -> Tracks:
        tracks = Tracks()
        lang = self.audio[0].split(":")[0]
        tracks.add(Video(id_="v", url="https://x/v", language=lang, codec=Video.Codec.AVC, range_=Video.Range.SDR))
        for entry in self.audio:
            # "es-ES:EC3" gives the track a codec other than AAC
            lang, _, codec = entry.partition(":")
            tracks.add(
                Audio(id_=entry, url=f"https://x/{entry}", language=lang, codec=Audio.Codec[codec or "AAC"], bitrate=1)
            )
        for lang, kind in self.subs:
            sdh, forced = kind == "sdh", kind == "forced"
            sub_id = f"{lang}{'-' + kind if kind else ''}"
            tracks.add(
                Subtitle(
                    id_=sub_id,
                    url=f"https://x/{sub_id}",
                    language=lang,
                    codec=Subtitle.Codec.WebVTT,
                    sdh=sdh,
                    forced=forced,
                )
            )
        return tracks


def run(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    service: RegionalService,
    **flags: Any,
) -> Any:
    return harness.run(monkeypatch, caplog, {1: {}}, service=service, **flags)


def test_orig_takes_another_region_of_the_title_language(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, RegionalService("en-US", ["en-GB", "fr-FR"], []))
    assert ids["audio"] == ["en-GB"]
    assert ids["video"] == ["v"]


def test_orig_keeps_the_script_the_title_region_implies(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, RegionalService("zh-TW", ["zh-HK", "zh-CN"], []))
    assert ids["audio"] == ["zh-HK"]


def test_a_regional_audio_request_misses_rather_than_take_a_sibling(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert run(monkeypatch, caplog, RegionalService("fr-FR", ["fr-CA"], []), a_lang=["fr-FR"]) == 1
    assert "fr-FR not found in audio tracks" in caplog.text


def test_a_regional_sdh_track_is_stripped_when_only_a_sibling_is_plain(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = RegionalService("en-US", ["en-US"], [("en-US", "sdh"), ("en-GB", "")])
    ids = run(monkeypatch, caplog, service, s_lang=["en-US"])
    assert sorted(ids["subs"]) == ["en-US-sdh", "en-US-sdh_stripped"]


def test_a_forced_track_does_not_hide_the_plain_track_a_region_means(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = RegionalService("ko", ["ko"], [("es", ""), ("es-ES", "forced"), ("fr", "")])
    assert run(monkeypatch, caplog, service, s_lang=["es-ES"])["subs"] == ["es"]


def test_a_bare_sdh_track_is_stripped_when_only_another_variant_is_plain(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = RegionalService("en", ["en"], [("en", "sdh"), ("en-GB", "")])
    assert sorted(run(monkeypatch, caplog, service, s_lang=["en"])["subs"]) == ["en-sdh", "en-sdh_stripped"]


def test_a_bare_audio_request_takes_the_variant_it_means(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = RegionalService("en", ["en", "pt-PT", "pt-BR"], [])
    assert run(monkeypatch, caplog, service, a_lang=["pt"])["audio"] == ["pt-BR"]


def test_orig_takes_the_title_variant_over_the_one_a_bare_tag_means(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert run(monkeypatch, caplog, RegionalService("fr-CA", ["fr-FR", "fr-CA", "en"], []))["audio"] == ["fr-CA"]


@pytest.mark.parametrize(
    ("subs", "flags", "expected"),
    [
        # -fs keeps a forced track that -sl names, even when no plain track has its language
        ([("en", "forced")], {"s_lang": ["en"], "forced_subs": True}, ["en-forced"]),
        ([("es", ""), ("es-ES", "forced")], {"s_lang": ["es-ES"], "forced_subs": True}, ["es", "es-ES-forced"]),
        # an SDH track cannot take the plain variant's place, and joins it as the same variant
        ([("en-US", ""), ("en", "sdh")], {"s_lang": ["en"]}, ["en-US", "en-sdh"]),
        # -fsl matches across every forced track, so the result does not depend on -sl
        ([("es-ES", "forced"), ("es-419", "forced")], {"forced_s_lang": ["es"]}, ["es-ES-forced"]),
        (
            [("fr", ""), ("es-ES", "forced"), ("es-419", "forced")],
            {"s_lang": ["fr"], "forced_s_lang": ["es"]},
            ["es-ES-forced", "fr"],
        ),
        # -sl orig resolves to the title language
        ([("en-GB", ""), ("en-US", "")], {"s_lang": ["orig"]}, ["en-GB"]),
    ],
)
def test_subtitle_selection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, subs: list, flags: dict, expected: list
) -> None:
    ids = run(monkeypatch, caplog, RegionalService("en-GB", ["ja"], subs), lang=["ja"], **flags)
    assert sorted(ids["subs"]) == expected


def test_a_forced_only_language_without_fs_is_reported_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = RegionalService("en", ["ja"], [("en", ""), ("es-419", ""), ("es-ES", "forced")])
    assert run(monkeypatch, caplog, service, lang=["ja"], s_lang=["en", "es-ES"]) == 1
    assert "es-ES not found in subtitle tracks" in caplog.text


def test_an_exclusion_removes_every_regional_form(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = RegionalService("ja", ["ja", "es-419", "es-ES"], [])
    assert run(monkeypatch, caplog, service, a_lang=["all", "-es"])["audio"] == ["ja"]


def test_the_meant_variant_does_not_depend_on_codec(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = RegionalService("ja", ["ja", "es-ES:EC3", "es-419"], [])
    ids = run(monkeypatch, caplog, service, a_lang=["es"], acodec=[Audio.Codec.AAC, Audio.Codec.EC3])
    assert ids["audio"] == ["es-ES:EC3"]
