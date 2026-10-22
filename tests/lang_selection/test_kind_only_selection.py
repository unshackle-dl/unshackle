"""Run the kind-only filters through the real dl.result() selection loop.

A fake service hands in-memory tracks to the loop, and the run stops at the first
title that reaches ``Tracks.tree``, the step right after selection. A title with no
track of the kind never gets there, so what the hook records is what the run kept.
"""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace
from typing import Any, Optional, cast

import pytest

from unshackle.commands.dl import dl
from unshackle.core.api.handlers import DEFAULT_DOWNLOAD_PARAMS
from unshackle.core.interactive import BitrateMatcher
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
    """One episode per entry; ``ad`` adds a descriptive track, ``forced`` a forced subtitle."""

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
        tracks.add(Subtitle(id_="s", url="https://x/s", language="en", codec=Subtitle.Codec.WebVTT))
        if kinds.get("forced"):
            tracks.add(Subtitle(id_="sf", url="https://x/sf", language="en", codec=Subtitle.Codec.WebVTT, forced=True))
        return tracks

    def get_chapters(self, title: Episode) -> list:
        return []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("on_"):
            # the event hooks (on_track_downloaded, ...) the loop subscribes
            return lambda *a, **k: None
        raise AttributeError(name)


def run(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    episodes: dict,
    service: Optional[FakeService] = None,
    **flags: Any,
) -> Any:
    """Return the Selected ids, the exit code, or None for a run that completes."""
    monkeypatch.setattr(Tracks, "tree", lambda self, *a, **k: (_ for _ in ()).throw(Selected(self)))
    monkeypatch.setattr("unshackle.core.providers.search_metadata", lambda *a, **k: None)
    cmd = Harness.__new__(Harness)
    cmd.log = logging.getLogger("test_kind_only")
    cmd.service = "FAKE"
    cmd.tmdb_searched = True
    cmd.completed_files = []
    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(dl.result).parameters.items():
        if name in DEFAULT_DOWNLOAD_PARAMS:
            kwargs[name] = DEFAULT_DOWNLOAD_PARAMS[name]
        elif name not in ("self", "service"):
            kwargs[name] = None if param.default is inspect.Parameter.empty else param.default
    kwargs.update(quality=[], vcodec=[], acodec=[], range_=[], wanted=[], cdm_only=None, downloads=1)
    kwargs.update(flags)
    caplog.set_level(logging.WARNING, logger="test_kind_only")
    try:
        cmd.result(cast(Service, service or FakeService(episodes)), **kwargs)
    except Selected as sel:
        return sel.ids
    except SystemExit as exc:
        return exc.code
    return None


def test_ado_keeps_the_descriptive_track_and_the_other_types(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, {1: {"ad": True, "forced": True}}, audio_description_only=True)
    assert ids == {"video": ["v"], "audio": ["ad"], "subs": ["s"]}


def test_fso_keeps_the_forced_track_and_the_other_types(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, {1: {"ad": True, "forced": True}}, forced_subs_only=True)
    assert ids == {"video": ["v"], "audio": ["a"], "subs": ["sf"]}


def test_only_flags_with_their_type_flags_give_a_single_kind(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, {1: {"ad": True, "forced": True}}, audio_description_only=True, audio_only=True)
    assert ids == {"video": [], "audio": ["ad"], "subs": []}
    ids = run(monkeypatch, caplog, {1: {"ad": True, "forced": True}}, forced_subs_only=True, subs_only=True)
    assert ids == {"video": [], "audio": [], "subs": ["sf"]}


@pytest.mark.parametrize("flag", ["audio_description_only", "forced_subs_only"])
def test_a_single_title_with_no_such_track_exits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, flag: str
) -> None:
    assert run(monkeypatch, caplog, {1: {}}, **{flag: True}) == 1
    assert "Skipping" not in caplog.text


def test_a_multi_title_run_skips_the_title_and_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, {1: {}, 2: {"ad": True}}, audio_description_only=True)
    assert ids["audio"] == ["ad"]
    assert "Skipping Show S01E01" in caplog.text


def test_a_multi_title_run_with_every_title_skipped_completes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert run(monkeypatch, caplog, {1: {}, 2: {}}, forced_subs_only=True) is None
    assert caplog.text.count("Skipping Show S01E") == 2


def test_interactive_export_initializes_the_export_namer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    created_for: list[str] = []

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {"export": True, "select_titles": False},
    )
    monkeypatch.setattr(
        "unshackle.commands.dl.ExportNamer",
        lambda service: created_for.append(service) or SimpleNamespace(seasons={}),
    )

    ids = run(monkeypatch, caplog, {1: {}}, interactive=True, export=False)

    assert ids == {"video": ["v"], "audio": ["a"], "subs": ["s"]}
    assert created_for == ["FAKE"]


class VariantService(FakeService):
    def get_tracks(self, title: Episode) -> Tracks:
        del title
        return Tracks(
            [
                Video(id_="v", url="https://x/v", language="en", codec=Video.Codec.AVC, range_=Video.Range.SDR),
                Audio(
                    id_="surround",
                    url="https://x/a51",
                    language="en",
                    codec=Audio.Codec.EC3,
                    bitrate=640_000,
                    channels=5.1,
                ),
                Audio(
                    id_="stereo",
                    url="https://x/a20",
                    language="en",
                    codec=Audio.Codec.EC3,
                    bitrate=640_000,
                    channels=2.0,
                ),
                Subtitle(id_="s", url="https://x/s", language="en", codec=Subtitle.Codec.WebVTT),
                Subtitle(id_="sf", url="https://x/sf", language="en", codec=Subtitle.Codec.WebVTT, forced=True),
            ]
        )


class VideoVariantService(FakeService):
    def __init__(self, episodes: dict[int, dict[str, bool]], bitrates: list[int]) -> None:
        super().__init__(episodes)
        self.bitrates = bitrates

    def get_tracks(self, title: Episode) -> Tracks:
        tracks = super().get_tracks(title)
        tracks.videos = [
            Video(
                id_=str(bitrate),
                url=f"https://x/{bitrate}",
                language="en",
                codec=Video.Codec.AVC,
                range_=Video.Range.SDR,
                bitrate=bitrate,
                width=1920,
                height=1080,
            )
            for bitrate in self.bitrates
        ]
        return tracks


def test_interactive_single_title_keeps_the_exact_video_variant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "video_profile": {"bitrate": 8_000_000, "rank": 1, "single": True},
            "quality": [1080],
            "vcodec": [Video.Codec.AVC],
            "range_": [Video.Range.SDR],
        },
    )

    ids = run(
        monkeypatch,
        caplog,
        {1: {}},
        service=VideoVariantService({1: {}}, [8_200_000, 8_000_000]),
        interactive=True,
    )

    assert ids["video"] == ["8000000"]


def test_interactive_single_title_keeps_the_exact_same_bitrate_video_variant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class FrameRateVariantService(VideoVariantService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            for track, fps in zip(tracks.videos, (24.0, 60.0), strict=True):
                track.id = str(int(fps))
                track.fps = fps
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "video_profile": {
                "id": "60",
                "bitrate": 8_000_000,
                "fps": 60.0,
                "rank": 1,
                "single": True,
            },
            "quality": [1080],
            "vcodec": [Video.Codec.AVC],
            "range_": [Video.Range.SDR],
        },
    )

    ids = run(
        monkeypatch,
        caplog,
        {1: {}},
        service=FrameRateVariantService({1: {}}, [8_000_000, 8_000_000]),
        interactive=True,
    )

    assert ids["video"] == ["60"]


def test_interactive_single_title_does_not_add_same_bitrate_video_languages(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class MultiLanguageVideoService(FakeService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.videos = [
                Video(
                    id_=track_id,
                    url=f"https://x/{track_id}",
                    language=language,
                    codec=Video.Codec.AVC,
                    range_=Video.Range.SDR,
                    bitrate=8_000_000,
                    width=1920,
                    height=1080,
                )
                for track_id, language in (("selected-en", "en"), ("unselected-fr", "fr"))
            ]
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "video_profile": {
                "id": "selected-en",
                "language": "en",
                "range": Video.Range.SDR,
                "bitrate": 8_000_000,
                "fps": None,
                "rank": 0,
                "single": True,
            },
            "quality": [1080],
            "vcodec": [Video.Codec.AVC],
            "range_": [Video.Range.SDR],
            "v_lang": ["all"],
        },
    )

    ids = run(monkeypatch, caplog, {1: {}}, service=MultiLanguageVideoService({1: {}}), interactive=True)

    assert ids["video"] == ["selected-en"]


def test_interactive_single_title_falls_back_when_video_id_changes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class RefetchedVideoService(FakeService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.videos = [
                Video(
                    id_=track_id,
                    url=f"https://x/{track_id}",
                    language=language,
                    codec=Video.Codec.AVC,
                    range_=Video.Range.SDR,
                    bitrate=8_000_000,
                    width=1920,
                    height=1080,
                    fps=24.0,
                )
                for track_id, language in (("download-en", "en"), ("download-fr", "fr"))
            ]
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "video_profile": {
                "id": "preview-en",
                "language": "en",
                "range": Video.Range.SDR,
                "bitrate": 8_000_000,
                "fps": 24.0,
                "rank": 0,
                "single": True,
            },
            "quality": [1080],
            "vcodec": [Video.Codec.AVC],
            "range_": [Video.Range.SDR],
            "v_lang": ["all"],
        },
    )

    ids = run(monkeypatch, caplog, {1: {}}, service=RefetchedVideoService({1: {}}), interactive=True)

    assert ids["video"] == ["download-en"]


def test_interactive_hybrid_keeps_the_chosen_base_variant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class HybridVariantService(FakeService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.videos = [
                Video(
                    id_=track_id,
                    url=f"https://x/{track_id}",
                    language="en",
                    codec=Video.Codec.HEVC,
                    range_=range_,
                    bitrate=bitrate,
                    width=1920,
                    height=1080,
                )
                for track_id, range_, bitrate in (
                    ("low", Video.Range.HDR10, 4_000_000),
                    ("high", Video.Range.HDR10, 8_000_000),
                    ("dv", Video.Range.DV, 6_000_000),
                )
            ]
            return tracks

    monkeypatch.setattr("unshackle.core.binaries.DoviTool", object())
    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "video_profile": {
                "id": "low",
                "language": "en",
                "range": Video.Range.HDR10,
                "bitrate": 4_000_000,
                "fps": None,
                "rank": 1,
                "single": True,
            },
            "quality": [1080],
            "vcodec": [Video.Codec.HEVC],
            "range_": [Video.Range.HYBRID],
        },
    )

    ids = run(monkeypatch, caplog, {1: {}}, service=HybridVariantService({1: {}}), interactive=True)

    assert ids["video"] == ["low", "dv"]


def test_interactive_hybrid_requires_dovi_tool(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class HybridService(FakeService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.videos = [
                Video(
                    id_=range_.name.lower(),
                    url=f"https://x/{range_.name.lower()}",
                    language="en",
                    codec=Video.Codec.HEVC,
                    range_=range_,
                    bitrate=8_000_000,
                    width=1920,
                    height=1080,
                )
                for range_ in (Video.Range.HDR10, Video.Range.DV)
            ]
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {"range_": [Video.Range.HYBRID], "select_titles": False},
    )
    monkeypatch.setattr("unshackle.core.binaries.DoviTool", None)

    assert run(monkeypatch, caplog, {1: {}}, service=HybridService({1: {}}), interactive=True) == 1
    assert "dovi_tool not detected" in caplog.text


class SubtitleLanguageService(FakeService):
    def get_tracks(self, title: Episode) -> Tracks:
        tracks = super().get_tracks(title)
        tracks.subtitles[0].language = "de"
        return tracks


@pytest.mark.parametrize(("best_available", "expected"), [(False, 1), (True, [])])
def test_interactive_subtitle_profile_is_validated_for_each_title(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    best_available: bool,
    expected: int | list[str],
) -> None:
    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "subtitle_profiles": [{"language": "en", "forced": False, "sdh": False, "cc": False}],
            "s_lang": ["en"],
            "no_subs": False,
        },
    )

    result = run(
        monkeypatch,
        caplog,
        {1: {}},
        service=SubtitleLanguageService({1: {}}),
        interactive=True,
        best_available=best_available,
    )

    assert (result if isinstance(result, int) else result["subs"]) == expected


def test_interactive_subtitle_profile_keeps_the_chosen_codec(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class SubtitleCodecService(FakeService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.subtitles = [
                Subtitle(id_="vtt", url="https://x/vtt", language="en", codec=Subtitle.Codec.WebVTT),
                Subtitle(
                    id_="ttml",
                    url="https://x/ttml",
                    language="en",
                    codec=Subtitle.Codec.TimedTextMarkupLang,
                ),
            ]
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "subtitle_profiles": [
                {
                    "language": "en",
                    "codec": Subtitle.Codec.WebVTT,
                    "forced": False,
                    "sdh": False,
                    "cc": False,
                }
            ],
            "s_lang": ["en"],
            "no_subs": False,
        },
    )

    ids = run(monkeypatch, caplog, {1: {}}, service=SubtitleCodecService({1: {}}), interactive=True)

    assert ids["subs"] == ["vtt"]


def test_interactive_multi_title_keeps_the_selected_video_rank(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "video_profile": {"bitrate": 6_000_000, "rank": 1, "single": False},
            "quality": [1080],
            "vcodec": [Video.Codec.AVC],
            "range_": [Video.Range.SDR],
        },
    )

    ids = run(
        monkeypatch,
        caplog,
        {1: {}, 2: {}},
        service=VideoVariantService({1: {}, 2: {}}, [8_000_000, 6_000_000, 4_000_000]),
        interactive=True,
    )

    assert ids["video"] == ["6000000"]


def test_interactive_multi_title_keeps_only_the_selected_video_language(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class MultiLanguageVideoService(FakeService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.videos = [
                Video(
                    id_=track_id,
                    url=f"https://x/{track_id}",
                    language=language,
                    codec=Video.Codec.AVC,
                    range_=Video.Range.SDR,
                    bitrate=bitrate,
                    width=1920,
                    height=1080,
                )
                for track_id, language, bitrate in (
                    ("en-high", "en", 8_000_000),
                    ("en-low", "en", 6_000_000),
                    ("fr-high", "fr", 10_000_000),
                )
            ]
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "video_profile": {
                "language": "en",
                "bitrate": 8_000_000,
                "rank": 0,
                "single": False,
            },
            "quality": [1080],
            "vcodec": [Video.Codec.AVC],
            "range_": [Video.Range.SDR],
            "v_lang": ["all"],
        },
    )

    ids = run(
        monkeypatch,
        caplog,
        {1: {}, 2: {}},
        service=MultiLanguageVideoService({1: {}, 2: {}}),
        interactive=True,
    )

    assert ids["video"] == ["en-high"]


def test_interactive_single_title_accepts_a_video_above_100_mbps(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class HighBitrateVideoService(VideoVariantService):
        pass

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "video_profile": {
                "id": "120000000",
                "language": "en",
                "range": Video.Range.SDR,
                "bitrate": 120_000_000,
                "rank": 0,
                "single": True,
            },
            "quality": [1080],
            "vcodec": [Video.Codec.AVC],
            "range_": [Video.Range.SDR],
            "vbitrate": BitrateMatcher(120_000_000, tolerance=0.05),
        },
    )

    ids = run(
        monkeypatch,
        caplog,
        {1: {}},
        service=HighBitrateVideoService({1: {}}, [120_000_000]),
        interactive=True,
    )

    assert ids["video"] == ["120000000"]


@pytest.mark.parametrize(("best_available", "expected"), [(False, 1), (True, ["fr-high"])])
def test_missing_interactive_video_language_respects_best_available(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    best_available: bool,
    expected: int | list[str],
) -> None:
    class FrenchVideoService(FakeService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.videos = [
                Video(
                    id_="fr-high",
                    url="https://x/fr-high",
                    language="fr",
                    codec=Video.Codec.AVC,
                    range_=Video.Range.SDR,
                    bitrate=8_000_000,
                    width=1920,
                    height=1080,
                )
            ]
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "video_profile": {"language": "en", "bitrate": 8_000_000, "rank": 0, "single": False},
            "quality": [1080],
            "vcodec": [Video.Codec.AVC],
            "range_": [Video.Range.SDR],
            "v_lang": ["all"],
        },
    )

    result = run(
        monkeypatch,
        caplog,
        {1: {}},
        service=FrenchVideoService({1: {}}),
        interactive=True,
        best_available=best_available,
    )

    assert (result if isinstance(result, int) else result["video"]) == expected


def test_real_video_bitrate_does_not_reject_the_exact_interactive_track(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "video_profile": {
                "id": "8000000",
                "language": "en",
                "range": Video.Range.SDR,
                "bitrate": 8_000_000,
                "rank": 0,
                "single": True,
            },
            "quality": [1080],
            "vcodec": [Video.Codec.AVC],
            "range_": [Video.Range.SDR],
            "vbitrate": BitrateMatcher(8_000_000, tolerance=0.05),
        },
    )

    def measure(tracks: list[Video], *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        for track in tracks:
            track.bitrate = 6_000_000

    monkeypatch.setattr("unshackle.commands.dl.apply_real_bitrates", measure)

    ids = run(
        monkeypatch,
        caplog,
        {1: {}},
        service=VideoVariantService({1: {}}, [8_000_000]),
        interactive=True,
        real_video_bitrate=True,
    )

    assert ids["video"] == ["8000000"]


def test_real_audio_bitrate_preserves_the_exact_interactive_track(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class AudioVariantService(FakeService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.audio = [
                Audio(
                    id_=str(bitrate),
                    url=f"https://x/{bitrate}",
                    language="en",
                    codec=Audio.Codec.AAC,
                    bitrate=bitrate,
                    channels=2.0,
                )
                for bitrate in (192_000, 128_000)
            ]
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "audio_profiles": [
                {
                    "id": "128000",
                    "language": "en",
                    "codec": Audio.Codec.AAC,
                    "bitrate": 128_000,
                    "channels": 2.0,
                    "descriptive": False,
                    "atmos": False,
                    "rank": 1,
                    "single": True,
                }
            ],
            "a_lang": ["en"],
            "acodec": [Audio.Codec.AAC],
        },
    )

    def measure(tracks: list[Audio], *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        measured = {192_000: 120_000, 128_000: 96_000}
        for track in tracks:
            track.bitrate = measured[track.bitrate]

    monkeypatch.setattr("unshackle.commands.dl.apply_real_bitrates", measure)

    ids = run(
        monkeypatch,
        caplog,
        {1: {}},
        service=AudioVariantService({1: {}}),
        interactive=True,
        real_audio_bitrate=True,
    )

    assert ids["audio"] == ["128000"]


def test_interactive_audio_profile_survives_the_real_selection_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "audio_profiles": [
                {
                    "language": "en",
                    "codec": Audio.Codec.EC3,
                    "bitrate": 640_000,
                    "channels": 2.0,
                    "descriptive": False,
                    "atmos": False,
                }
            ],
            "a_lang": ["en"],
            "acodec": [Audio.Codec.EC3],
        },
    )

    ids = run(monkeypatch, caplog, {1: {}}, service=VariantService({1: {}}), interactive=True)

    assert ids["audio"] == ["stereo"]


def test_multiple_interactive_audio_profiles_survive_the_real_selection_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    profiles = [
        {
            "language": "en",
            "codec": Audio.Codec.EC3,
            "bitrate": bitrate,
            "channels": channels,
            "descriptive": False,
            "atmos": False,
        }
        for bitrate, channels in ((640_000, 5.1), (128_000, 2.0))
    ]

    class MultiVariantService(VariantService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.audio[1].bitrate = 128_000
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {"audio_profiles": profiles, "a_lang": ["en"], "acodec": [Audio.Codec.EC3]},
    )

    ids = run(monkeypatch, caplog, {1: {}}, service=MultiVariantService({1: {}}), interactive=True)

    assert set(ids["audio"]) == {"surround", "stereo"}


@pytest.mark.parametrize("best_available", [False, True])
def test_missing_interactive_audio_profile_respects_best_available(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    best_available: bool,
) -> None:
    profiles = [
        {
            "language": "en",
            "codec": Audio.Codec.EC3,
            "bitrate": 640_000,
            "channels": channels,
            "descriptive": False,
            "atmos": False,
            "rank": 0,
            "single": False,
        }
        for channels in (2.0, 5.1)
    ]

    class StereoOnlyService(VariantService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.audio = [track for track in tracks.audio if track.channels == 2.0]
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "audio_profiles": profiles,
            "a_lang": ["en"],
            "acodec": [Audio.Codec.EC3],
            "select_titles": False,
        },
    )

    result = run(
        monkeypatch,
        caplog,
        {1: {}},
        service=StereoOnlyService({1: {}}),
        interactive=True,
        best_available=best_available,
    )

    if best_available:
        assert result["audio"] == ["stereo"]
        assert "selected interactive audio variant is unavailable" in caplog.text.lower()
    else:
        assert result == 1


def test_selected_audio_profile_missing_from_empty_pool_exits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class NoAudioService(FakeService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.audio = []
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "audio_profiles": [
                {
                    "language": "en",
                    "codec": Audio.Codec.AAC,
                    "bitrate": 128_000,
                    "channels": None,
                    "descriptive": False,
                    "atmos": False,
                    "rank": 0,
                    "single": False,
                }
            ],
            "a_lang": ["en"],
            "acodec": [Audio.Codec.AAC],
        },
    )

    assert run(monkeypatch, caplog, {1: {}}, service=NoAudioService({1: {}}), interactive=True) == 1
    assert "selected interactive audio variant is unavailable" in caplog.text.lower()


def test_interactive_kind_only_choice_rechecks_existing_type_only_flags(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "forced_subs_only": True,
            "forced_subs": True,
            "no_subs": False,
            "select_titles": False,
        },
    )

    assert run(monkeypatch, caplog, {1: {"forced": True}}, interactive=True, video_only=True) == 1
    assert "--forced-subs-only cannot be used with --video-only" in caplog.text


def test_interactive_audio_profiles_preserve_an_unknown_bitrate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class MixedBitrateService(FakeService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.audio.append(
                Audio(id_="unknown", url="https://x/unknown", language="en", codec=Audio.Codec.AAC, bitrate=None)
            )
            return tracks

    profiles = [
        {
            "language": "en",
            "codec": Audio.Codec.AAC,
            "bitrate": bitrate,
            "channels": None,
            "descriptive": False,
            "atmos": False,
        }
        for bitrate in (128_000, None)
    ]
    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "audio_profiles": profiles,
            "a_lang": ["en"],
            "acodec": [Audio.Codec.AAC],
            "abitrate": 128,
        },
    )

    ids = run(monkeypatch, caplog, {1: {}}, service=MixedBitrateService({1: {}}), interactive=True)

    assert ids["audio"] == ["a", "unknown"]


def test_interactive_audio_profile_keeps_one_nearest_bitrate_variant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class AdjacentBitrateService(FakeService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.audio = [
                Audio(
                    id_=str(bitrate),
                    url=f"https://x/{bitrate}",
                    language="en",
                    codec=Audio.Codec.AAC,
                    bitrate=bitrate,
                    channels=2.0,
                )
                for bitrate in (130_000, 128_000)
            ]
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "audio_profiles": [
                {
                    "language": "en",
                    "codec": Audio.Codec.AAC,
                    "bitrate": 128_000,
                    "channels": 2.0,
                    "descriptive": False,
                    "atmos": False,
                    "rank": 1,
                    "single": True,
                }
            ],
            "a_lang": ["en"],
            "acodec": [Audio.Codec.AAC],
        },
    )

    ids = run(monkeypatch, caplog, {1: {}}, service=AdjacentBitrateService({1: {}}), interactive=True)

    assert ids["audio"] == ["128000"]


def test_interactive_batch_audio_profile_falls_back_to_the_same_rank(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class VaryingBitrateService(FakeService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            bitrates = (192_000, 128_000) if title.number == 1 else (256_000,)
            tracks.audio = [
                Audio(
                    id_=str(bitrate),
                    url=f"https://x/{bitrate}",
                    language="en",
                    codec=Audio.Codec.AAC,
                    bitrate=bitrate,
                    channels=2.0,
                )
                for bitrate in bitrates
            ]
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "audio_profiles": [
                {
                    "language": "en",
                    "codec": Audio.Codec.AAC,
                    "bitrate": 128_000,
                    "channels": 2.0,
                    "descriptive": False,
                    "atmos": False,
                    "rank": 1,
                    "single": False,
                }
            ],
            "a_lang": ["en"],
            "acodec": [Audio.Codec.AAC],
            "select_titles": True,
        },
    )
    monkeypatch.setattr("unshackle.commands.dl.select_multiple", lambda *args, **kwargs: [1])

    ids = run(
        monkeypatch,
        caplog,
        {1: {}, 2: {}},
        service=VaryingBitrateService({1: {}, 2: {}}),
        interactive=True,
    )

    assert ids["audio"] == ["256000"]


def test_interactive_audio_profile_keeps_the_exact_regional_language(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class RegionalVariantService(VariantService):
        def get_tracks(self, title: Episode) -> Tracks:
            tracks = super().get_tracks(title)
            tracks.audio = [
                Audio(
                    id_=language,
                    url=f"https://x/{language}",
                    language=language,
                    codec=Audio.Codec.EC3,
                    bitrate=640_000,
                    channels=5.1,
                )
                for language in ("es-ES", "es-419")
            ]
            return tracks

    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "audio_profiles": [
                {
                    "language": "es-ES",
                    "codec": Audio.Codec.EC3,
                    "bitrate": 640_000,
                    "channels": 5.1,
                    "descriptive": False,
                    "atmos": False,
                }
            ],
            "a_lang": ["es-ES"],
            "acodec": [Audio.Codec.EC3],
        },
    )

    ids = run(monkeypatch, caplog, {1: {}}, service=RegionalVariantService({1: {}}), interactive=True)

    assert ids["audio"] == ["es-ES"]


def test_interactive_forced_only_survives_the_real_selection_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "unshackle.core.interactive.run_interactive_session",
        lambda *args: {
            "forced_subs_only": True,
            "s_lang": ["en"],
            "no_subs": False,
            "subtitle_profiles": [{"language": "en", "forced": True, "sdh": False, "cc": False}],
        },
    )

    ids = run(monkeypatch, caplog, {1: {}}, service=VariantService({1: {}}), interactive=True)

    assert ids["subs"] == ["sf"]
