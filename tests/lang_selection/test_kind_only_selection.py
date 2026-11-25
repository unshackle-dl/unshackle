"""Run track selection through the real dl.result() loop, for what the run does with a skip.

A fake service hands in-memory tracks to the loop, and the run stops at the first
title that reaches ``Tracks.tree``, the step right after selection. What a selection keeps
is tested through TrackSelection in test_track_selection; these tests cover the run:
when a skip stops it, what it reports, and how it settles the post-script groups.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any, Optional, cast

import pytest
import requests

from unshackle.commands.dl import dl
from unshackle.core import binaries
from unshackle.core.constants import DOWNLOAD_LICENCE_ONLY
from unshackle.core.dl_options import DEFAULT_DOWNLOAD_PARAMS
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Series
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video


class Selected(BaseException):
    """Carries the kept track ids out of the loop."""

    def __init__(self, tracks: Tracks) -> None:
        self.ids = {
            "video": [t.id for t in tracks.videos],
            "audio": [t.id for t in tracks.audio],
            "subs": [t.id for t in tracks.subtitles],
        }


class Harness(dl):
    def __getattr__(self, name: str) -> None:
        # every optional dl attribute (tmdb ids, cdm, vaults, ...) reads as unset
        return None


class FakeService:
    """One episode per entry; ``ad`` adds a descriptive track, ``forced`` a forced subtitle, ``ko`` Korean audio."""

    def __init__(self, episodes: dict[int, dict[str, bool]]) -> None:
        self.episodes = episodes
        self.session = None

    def authenticate(self, cookies: Any = None, credential: Any = None) -> None:
        pass

    def get_titles_cached(self) -> Series:
        return Series(
            Episode(id_=f"show-s01-e{n:03}", service=FakeService, title="Show", season=1, number=n, language="en")
            for n in self.episodes
        )

    def get_tracks(self, title: Episode) -> Tracks:
        kinds = self.episodes[title.number]
        tracks = Tracks()
        tracks.add(Video(id_="v", url="https://x/v", language="en", codec=Video.Codec.AVC, range_=Video.Range.SDR))
        tracks.add(Audio(id_="a", url="https://x/a", language="en", codec=Audio.Codec.AAC, bitrate=128_000))
        if kinds.get("ad"):
            tracks.add(
                Audio(id_="ad", url="https://x/ad", language="en", codec=Audio.Codec.AAC, bitrate=1, descriptive=True)
            )
        if kinds.get("ko"):
            tracks.add(Audio(id_="ko", url="https://x/ko", language="ko", codec=Audio.Codec.AAC, bitrate=128_000))
        tracks.add(Subtitle(id_="s", url="https://x/s", language="en", codec=Subtitle.Codec.WebVTT))
        if kinds.get("forced"):
            tracks.add(Subtitle(id_="sf", url="https://x/sf", language="en", codec=Subtitle.Codec.WebVTT, forced=True))
        return tracks

    def get_chapters(self, title: Episode) -> list:
        return []

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("on_", "get_")):
            # the event hooks (on_track_downloaded, ...) and the DRM callbacks a run past selection probes
            return lambda *a, **k: None
        raise AttributeError(name)


def stub_hybrid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a hybrid run start without dovi_tool, and load the service's tracks with no probes."""
    monkeypatch.setattr(binaries, "DoviTool", Path("/unused/dovi_tool"), raising=False)
    monkeypatch.setattr(
        "unshackle.commands.dl.load_tracks",
        lambda service, title, **_: setattr(title, "tracks", service.get_tracks(title)),
    )


def run(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    episodes: dict,
    service: Optional[FakeService] = None,
    to_end: bool = False,
    **flags: Any,
) -> Any:
    """Return the Selected ids, the exit code, or None for a run that completes; to_end runs past selection."""
    if not to_end:
        monkeypatch.setattr(Tracks, "tree", lambda self, *a, **k: (_ for _ in ()).throw(Selected(self)))
    else:
        monkeypatch.setattr("unshackle.commands.dl.download_tracks_in_passes", lambda *a, **k: None)
    monkeypatch.setattr("unshackle.core.providers.search_metadata", lambda *a, **k: None)
    cmd = Harness.__new__(Harness)
    cmd.log = logging.getLogger("test_kind_only")
    cmd.service = "FAKE"
    cmd.tmdb_searched = True
    cmd.completed_files = []
    cmd.skipped_titles = []
    cmd.skipped_subtitles = []
    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(dl.result).parameters.items():
        if name in DEFAULT_DOWNLOAD_PARAMS:
            kwargs[name] = DEFAULT_DOWNLOAD_PARAMS[name]
        elif name not in ("self", "service"):
            kwargs[name] = None if param.default is inspect.Parameter.empty else param.default
    kwargs.update(quality=[], vcodec=[], acodec=[], range_=[], wanted=[], cdm_only=None, downloads=1)
    kwargs.update(flags)
    caplog.set_level(logging.WARNING, logger="test_kind_only")
    service = service or FakeService(episodes)
    if to_end:
        service.session = requests.Session()
    try:
        cmd.result(cast(Service, service), **kwargs)
    except Selected as sel:
        return sel.ids
    except SystemExit as exc:
        return exc.code
    finally:
        DOWNLOAD_LICENCE_ONLY.clear()
    return None


@pytest.mark.parametrize("flag", ["audio_description_only", "forced_subs_only"])
def test_a_single_title_with_no_such_track_exits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, flag: str
) -> None:
    assert run(monkeypatch, caplog, {1: {}}, **{flag: True}) == 1
    assert "Skipping" not in caplog.text


def test_subs_only_with_no_matching_subtitle_exits_a_single_title_and_skips_in_a_season(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert run(monkeypatch, caplog, {1: {}}, subs_only=True, s_lang=["fr"]) == 1
    assert run(monkeypatch, caplog, {1: {}, 2: {}}, subs_only=True, s_lang=["fr"]) == 1
    assert caplog.text.count("Skipping Show S01E") == 2
    assert "Skipping Show S01E02: There's no track left for the selected options." in caplog.text


def test_a_multi_title_run_skips_the_title_and_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, {1: {}, 2: {"ad": True}}, audio_description_only=True)
    assert ids["audio"] == ["ad"]
    assert "Skipping Show S01E01" in caplog.text


def test_a_multi_title_run_with_every_title_skipped_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert run(monkeypatch, caplog, {1: {}, 2: {}}, forced_subs_only=True) == 1
    assert caplog.text.count("Skipping Show S01E") == 2


def test_a_required_language_miss_stops_the_run_by_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sink: list[dict] = []
    assert run(monkeypatch, caplog, {1: {}, 2: {"ko": True}}, require_audio=["ko"], progress_sink=sink.append) == 1
    assert "Skipping" not in caplog.text
    assert "Show S01E01: Required audio language(s) not found: ko" in caplog.text
    assert not any("skipped_titles" in event for event in sink)


def test_skip_missing_skips_the_title_and_reports_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sink: list[dict] = []
    ids = run(
        monkeypatch,
        caplog,
        {1: {}, 2: {"ko": True}},
        require_audio=["ko"],
        skip_missing=True,
        progress_sink=sink.append,
    )
    assert ids == {"video": ["v"], "audio": ["a"], "subs": ["s"]}
    assert "Skipping Show S01E01: Required audio language(s) not found: ko" in caplog.text
    assert {"skipped_titles": [{"title": "Show S01E01", "reason": "Required audio language(s) not found: ko"}]} in sink


def test_skip_missing_skips_a_filter_miss_and_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, {1: {}, 2: {"ko": True}}, a_lang=["ko"], skip_missing=True)
    assert ids == {"video": ["v"], "audio": ["ko"], "subs": ["s"]}
    assert "Skipping Show S01E01" in caplog.text


def test_skip_missing_still_fails_a_run_that_skips_every_title(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert run(monkeypatch, caplog, {1: {}, 2: {}}, require_audio=["ko"], skip_missing=True) == 1
    assert "Skipped all 2 titles" in caplog.text
    assert run(monkeypatch, caplog, {1: {}}, require_audio=["ko"], skip_missing=True) == 1


class LangService(FakeService):
    """Each episode names its original language and its audio languages; the video is in the original language."""

    def get_titles_cached(self) -> Series:
        return Series(
            Episode(id_=f"show-s01-e{n:03}", service=FakeService, title="Show", season=1, number=n, language=ep["orig"])
            for n, ep in self.episodes.items()
        )

    def get_tracks(self, title: Episode) -> Tracks:
        ep = self.episodes[title.number]
        tracks = Tracks()
        tracks.add(Video(id_="v", url="https://x/v", language=ep["orig"] or "en", codec=Video.Codec.AVC))
        for lang in ep["audio"]:
            tracks.add(Audio(id_=lang, url=f"https://x/{lang}", language=lang, codec=Audio.Codec.AAC, bitrate=128_000))
        return tracks


JA_EN = {"orig": "ja", "audio": ["ja", "en"]}
FR = {"orig": "ja", "audio": ["fr"]}
EVERY = {"orig": "ja", "audio": ["ja", "ko", "en"]}
ORIG_KO_EN = ["orig", "ko", "en"]


@pytest.mark.parametrize(
    "first,flags,outcome,logged",
    [
        (JA_EN, {}, 1, "Show S01E01: ko not found in audio tracks"),
        (JA_EN, {"skip_missing": True}, ["ja", "ko", "en"], "Skipping Show S01E01: ko not found in audio tracks"),
        ({"orig": "ja", "audio": ["ko", "en"]}, {"skip_missing": True}, ["ja", "ko", "en"], "S01E01: orig not found"),
        (JA_EN, {"best_available": True}, ["ja", "en"], "ko not found in audio tracks, continuing with: orig, en"),
        (FR, {"best_available": True}, 1, "orig, ko, en not found in audio tracks and no fallback available"),
        (FR, {"best_available": True, "skip_missing": True}, ["ja", "ko", "en"], "Skipping Show S01E01: orig, ko, en"),
        (JA_EN, {"best_available": True, "require_audio": ["ko"]}, 1, "Required audio language(s) not found: ko"),
        (
            JA_EN,
            {"best_available": True, "require_audio": ["ko"], "skip_missing": True},
            ["ja", "ko", "en"],
            "Skipping Show S01E01: Required audio language(s) not found: ko",
        ),
    ],
)
def test_several_audio_languages_skip_an_episode_that_misses_one(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    first: dict,
    flags: dict[str, Any],
    outcome: Any,
    logged: str,
) -> None:
    """-al orig,ko,en on a season where only the second episode has all three."""
    episodes = {1: first, 2: EVERY}
    result = run(monkeypatch, caplog, episodes, service=LangService(episodes), a_lang=ORIG_KO_EN, **flags)
    assert (result if outcome == 1 else result["audio"]) == outcome
    assert logged in caplog.text


def test_several_audio_languages_fail_a_season_that_misses_one_everywhere(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    episodes = {1: JA_EN, 2: JA_EN}
    assert run(monkeypatch, caplog, episodes, service=LangService(episodes), a_lang=ORIG_KO_EN, skip_missing=True) == 1
    assert caplog.text.count("ko not found in audio tracks") == 2
    assert "Skipped all 2 titles" in caplog.text


def test_a_partly_skipped_run_completes_and_lists_the_skips(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    episodes = {1: {}, 2: {"ko": True}}
    assert run(monkeypatch, caplog, episodes, to_end=True, a_lang=["ko"], skip_missing=True, skip_dl=True) is None
    assert "Skipped 1 of 2 titles: Show S01E01" in caplog.text


@pytest.mark.parametrize("no_mux,settled", [(False, 2), (True, 1)])
def test_a_skipped_title_settles_its_post_script_group(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, no_mux: bool, settled: int
) -> None:
    """The season event waits on every title of a group; a skipped title counts as settled, once at count time."""
    import unshackle.commands.dl as dl_module

    seen: list[str] = []
    group = dl_module.post_script_group
    monkeypatch.setattr(dl_module, "post_script_group", lambda t: seen.append(str(t)) or group(t))
    run(monkeypatch, caplog, {1: {}, 2: {"ko": True}}, a_lang=["ko"], skip_missing=True, no_mux=no_mux)
    assert seen.count("Show S01E01") == settled


class CaptionlessService(FakeService):
    """A service that finds out in get_tracks that the title has no subtitles, and turns them off."""

    def get_tracks(self, title: Episode) -> Tracks:
        tracks = super().get_tracks(title)
        tracks.subtitles = []
        self.NO_SUBTITLES = True
        return tracks


def test_no_subtitles_set_in_get_tracks_turns_the_subtitle_requirement_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, {1: {}}, service=CaptionlessService({1: {}}), require_subs=["en"])
    assert ids == {"video": ["v"], "audio": ["a"], "subs": []}


class Muxed(BaseException):
    """Stops the run at the first mux."""


def test_a_failed_hybrid_muxes_the_ranges_it_falls_back_to(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The mux step must see the fallback ranges, or it runs the hybrid step on an SDR title."""

    def hybrid_step(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the hybrid step ran for a title that fell back to SDR")

    def mux(*args: Any, **kwargs: Any) -> None:
        raise Muxed()

    stub_hybrid(monkeypatch)
    monkeypatch.setattr("unshackle.core.mux_step.hybrid_plan", hybrid_step)
    monkeypatch.setattr(Tracks, "mux", mux)
    ranges = [Video.Range.HYBRID, Video.Range.SDR]
    with pytest.raises(Muxed):
        run(monkeypatch, caplog, {1: {}}, to_end=True, video_only=True, range_=ranges, best_available=True)
    assert "continuing with SDR" in caplog.text
