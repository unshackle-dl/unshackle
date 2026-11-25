"""Run regional language requests through the real dl.result() selection loop."""

from __future__ import annotations

from typing import Any

import pytest
from langcodes import Language

from tests.lang_selection import test_kind_only_selection as harness
from unshackle.core.mux_step import hybrid_plan
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
    ids = run(monkeypatch, caplog, service, lang=["ja"], s_lang=["en", "es-ES"])
    assert "es-ES not found in subtitle tracks, continuing with: en" in caplog.text
    assert ids["subs"] == ["en"]


def test_a_missing_subtitle_language_continues_without_subtitles(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = RegionalService("en", ["ja"], [("en", "")])
    ids = run(monkeypatch, caplog, service, lang=["ja"], s_lang=["fr"])
    assert "fr not found in subtitle tracks, continuing without subtitles" in caplog.text
    assert ids["subs"] == []


def test_a_missing_orig_subtitle_is_reported_as_orig(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = RegionalService("ja", ["ja"], [("en", "")])
    ids = run(monkeypatch, caplog, service, s_lang=["orig", "en"])
    assert "orig not found in subtitle tracks, continuing with: en" in caplog.text
    assert ids["subs"] == ["en"]


def test_require_subs_fails_on_a_forced_track_that_is_dropped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = RegionalService("ja", ["ja"], [("en", "forced")])
    assert run(monkeypatch, caplog, service, require_subs=["en"]) == 1


def test_require_subs_counts_a_forced_track_that_fs_keeps(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = RegionalService("ja", ["ja"], [("en", "forced")])
    assert run(monkeypatch, caplog, service, require_subs=["en"], forced_subs=True)["subs"] == ["en-forced"]


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


class RegionalVideoService(RegionalService):
    """A RegionalService whose one video is replaced by `videos`: SDR AVC tracks given as (id, language, height, kbps)."""

    def __init__(self, title_lang: str, videos: list[tuple[Any, ...]]) -> None:
        super().__init__(title_lang, [title_lang], [])
        self.videos = videos

    def get_tracks(self, title: Episode) -> Tracks:
        tracks = super().get_tracks(title)
        tracks.videos = [self.video(*row) for row in self.videos]
        return tracks

    def video(self, *row: Any) -> Video:
        id_, lang, height, kbps = row
        return Video(
            id_=id_,
            url=f"https://x/{id_}",
            language=lang,
            codec=Video.Codec.AVC,
            range_=Video.Range.SDR,
            width=height * 16 // 9,
            height=height,
            bitrate=kbps * 1000,
        )


# en means en-US, and the title has none, so a bare en or orig request takes both regional forms
MIXED = [("au480", "en-AU", 480, 900), ("gb720", "en-GB", 720, 2000)]
SAME_HEIGHT = [("au720", "en-AU", 720, 900), ("gb720", "en-GB", 720, 2000)]


@pytest.mark.parametrize("request_", [{"v_lang": ["en"]}, {}], ids=["vl-en", "orig"])
@pytest.mark.parametrize(
    ("videos", "flags", "expected"),
    [
        (MIXED, {}, "gb720"),
        (MIXED, {"quality": [1], "worst": True, "best_available": True}, "au480"),
        (SAME_HEIGHT, {"quality": [720]}, "gb720"),
        (SAME_HEIGHT, {"quality": [720], "worst": True}, "au720"),
    ],
    ids=["best", "lowest-resolution", "best-at-720", "worst-at-720"],
)
def test_the_regional_forms_of_one_request_rank_as_one_language(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    videos: list,
    flags: dict,
    expected: str,
    request_: dict,
) -> None:
    ids = run(monkeypatch, caplog, RegionalVideoService("en", videos), **request_, **flags)
    assert ids["video"] == [expected]


@pytest.mark.parametrize(
    ("v_lang", "expected"),
    [
        (["en-AU"], ["au480"]),
        (["en-GB", "en-AU"], ["gb720", "au480"]),
        (["en-AU", "en-GB"], ["au480", "gb720"]),
        (["all"], ["gb720", "au480"]),
    ],
)
def test_regional_tags_and_several_video_languages_keep_their_selection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, v_lang: list, expected: list
) -> None:
    assert run(monkeypatch, caplog, RegionalVideoService("en", MIXED), v_lang=v_lang)["video"] == expected


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({"v_lang": ["en", "fr"]}, ["gb720", "fr720"]),
        ({"v_lang": ["en", "orig"]}, ["gb720"]),
        ({"v_lang": ["en", "de"], "best_available": True}, ["gb720"]),
    ],
    ids=["two-languages", "orig-is-en", "missing-language-dropped"],
)
def test_each_video_request_gives_one_video_across_its_regional_tags(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, flags: dict, expected: list
) -> None:
    service = RegionalVideoService("en", [*MIXED, ("fr720", "fr-FR", 720, 1500)])
    assert run(monkeypatch, caplog, service, **flags)["video"] == expected


class HdrRegionalService(RegionalVideoService):
    """A RegionalVideoService of HEVC videos given as (id, language, range, width, height, kbps)."""

    def video(self, *row: Any) -> Video:
        id_, lang, range_, width, height, kbps = row
        return Video(
            id_=id_,
            url=f"https://x/{id_}",
            language=lang,
            codec=Video.Codec.HEVC,
            range_=Video.Range[range_],
            width=width,
            height=height,
            bitrate=kbps * 1000,
        )


class SelectedWithStandalone(harness.Selected):
    def __init__(self, tracks: Tracks) -> None:
        super().__init__(tracks)
        self.ids["standalone"] = [t.id for t in tracks.videos if not t.hybrid_base_only]


def run_hdr(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, videos: list, ranges: list, **flags: Any
) -> Any:
    harness.stub_hybrid(monkeypatch)
    monkeypatch.setattr(harness, "Selected", SelectedWithStandalone)
    service = HdrRegionalService("en", videos)
    range_ = [Video.Range[r] for r in ranges]
    return run(monkeypatch, caplog, service, quality=[2160], v_lang=["en"], range_=range_, **flags)


def test_hybrid_takes_the_best_dv_source_of_the_lowest_height_across_regional_tags(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    videos = [
        ("hdr", "en-AU", "HDR10", 3840, 2160, 8000),
        ("dv-au", "en-AU", "DV", 1920, 1080, 2000),
        ("dv-gb", "en-GB", "DV", 1920, 1080, 6000),
    ]
    assert run_hdr(monkeypatch, caplog, videos, ["HYBRID"])["video"] == ["hdr", "dv-gb"]


def test_generated_dv_takes_the_best_base_across_regional_tags(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    videos = [("hdr-au", "en-AU", "HDR10", 3840, 1600, 8000), ("hdr-gb", "en-GB", "HDR10", 3840, 2160, 8000)]
    assert run_hdr(monkeypatch, caplog, videos, ["HDR10"], generate_dv=True)["video"] == ["hdr-gb"]


@pytest.mark.parametrize(
    ("videos", "expected"),
    [
        (
            [
                ("hdr-au", "en-AU", "HDR10", 3840, 1600, 8000),
                ("hdr-gb", "en-GB", "HDR10", 3840, 2160, 8000),
                ("dv", "en-AU", "DV", 1920, 1080, 2000),
            ],
            ["hdr-gb"],
        ),
        (
            [
                ("hdr10p", "en-AU", "HDR10P", 3840, 2160, 8000),
                ("hdr-crop", "en-AU", "HDR10", 3840, 1600, 9000),
                ("dv", "en-AU", "DV", 1920, 1080, 2000),
            ],
            ["hdr10p"],
        ),
    ],
    ids=["regional-tags", "standalone-at-another-height"],
)
def test_a_standalone_range_beside_hybrid_does_not_add_a_hybrid_base(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, videos: list, expected: list
) -> None:
    class Planned(BaseException):
        def __init__(self, videos: list[Video], generate_dv: bool) -> None:
            self.base_ids = [v.id for v in hybrid_plan(videos, generate_dv)[0]]

    def stop_at_plan(videos: list[Video], generate_dv: bool) -> None:
        raise Planned(videos, generate_dv)

    monkeypatch.setattr("unshackle.core.mux_step.hybrid_plan", stop_at_plan)
    monkeypatch.setattr(Video, "normalize_vui", lambda self: None)
    with pytest.raises(Planned) as planned:
        run_hdr(monkeypatch, caplog, videos, ["HYBRID", "HDR10"], to_end=True, video_only=True)
    assert planned.value.base_ids == expected


def test_the_fallback_from_a_failed_hybrid_ranks_regional_tags_as_one_language(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    videos = [("sdr-au", "en-AU", "SDR", 3840, 2160, 3000), ("sdr-gb", "en-GB", "SDR", 3840, 2160, 9000)]
    assert run_hdr(monkeypatch, caplog, videos, ["HYBRID", "SDR"], best_available=True)["video"] == ["sdr-gb"]
