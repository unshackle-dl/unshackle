from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterator

import click
import pytest
from click.testing import CliRunner

from unshackle.commands.dl import dl, select_interactive_video
from unshackle.core.interactive import (
    BitrateMatcher,
    get_standard_quality_tier,
    parse_indices,
    run_interactive_session,
    run_service_extra_options,
    run_service_selector,
)
from unshackle.core.services import Services
from unshackle.core.titles import Episode, Series
from unshackle.core.tracks import Audio, Subtitle, Video
from unshackle.core.utils.selector import Selector


@dataclass
class FakeTracks:
    videos: list[Any] = field(default_factory=list)
    audio: list[Any] = field(default_factory=list)
    subtitles: list[Any] = field(default_factory=list)

    def add(self, other: FakeTracks, warn_only: bool = False) -> None:
        del warn_only
        self.videos.extend(other.videos)
        self.audio.extend(other.audio)
        self.subtitles.extend(other.subtitles)


class FakeService:
    def __init__(self, tracks: FakeTracks) -> None:
        self.tracks = tracks
        self.session = None
        self.track_request = SimpleNamespace(codecs=[], ranges=[])
        self.calls = 0

    def get_tracks(self, title: Any) -> FakeTracks:
        del title
        self.calls += 1
        return self.tracks


def video(
    *,
    codec: Video.Codec = Video.Codec.HEVC,
    range_: Video.Range = Video.Range.SDR,
    bitrate: int | None = 8_000_000,
    fps: float = 24.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="video",
        session=None,
        language="en",
        codec=codec,
        range=range_,
        bitrate=bitrate,
        width=1920,
        height=1080,
        fps=fps,
        dv=range_ == Video.Range.DV,
        hdr10=range_ == Video.Range.HDR10,
    )


def audio(*, bitrate: int | None = 640_000, channels: float | str | None = 5.1) -> SimpleNamespace:
    return SimpleNamespace(
        id="audio",
        codec=Audio.Codec.EC3,
        bitrate=bitrate,
        channels=channels,
        language="en",
        is_original_lang=True,
        atmos=False,
        descriptive=False,
    )


def prompt_answers(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    values: Iterator[str] = iter(answers)
    monkeypatch.setattr("unshackle.core.interactive.tui_prompt", lambda *args, **kwargs: next(values))


@pytest.mark.parametrize(
    ("value", "maximum", "expected"),
    [
        ("1, 3-5", 5, [0, 2, 3, 4]),
        ("5-3 1", 5, [0, 2, 3, 4]),
        ("0 2 9 bad", 3, [1]),
        ("", 3, []),
    ],
)
def test_track_indices_accept_lists_ranges_and_reversed_ranges(value: str, maximum: int, expected: list[int]) -> None:
    assert parse_indices(value, maximum) == expected


def test_bitrate_matcher_accepts_engine_kbps_within_tolerance() -> None:
    matcher = BitrateMatcher(8_000_000, tolerance=0.05)

    assert matcher == 8_000
    assert matcher == 8_300
    assert matcher != 8_500
    assert matcher != None  # noqa: E711


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [(1920, 800, 800), (1280, 534, 534), (1920, 1080, 1080)],
)
def test_quality_preserves_the_selected_track_height(width: int, height: int, expected: int) -> None:
    assert get_standard_quality_tier(SimpleNamespace(width=width, height=height)) == expected


def test_service_selector_hides_the_example_service(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeSelector:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def run(self) -> list[int]:
            return [1]

    monkeypatch.setattr("unshackle.core.interactive.Services.get_tags", lambda: ["EXAMPLE", "AMZN", "NF"])
    monkeypatch.setattr("unshackle.core.interactive.Selector", FakeSelector)

    assert run_service_selector() == "NF"
    assert captured["options"] == ["AMZN", "NF"]
    assert captured["single_select"] is True


def test_interactive_command_uses_the_service_group_router(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[click.Context, str]] = []
    initialized: list[dict[str, Any]] = []

    @click.command(name="REMOTE")
    @click.argument("title")
    def remote_service(title: str) -> str:
        return title

    def get_command(self: Services, ctx: click.Context, name: str) -> click.Command:
        calls.append((ctx, name))
        return remote_service

    monkeypatch.setattr("unshackle.core.interactive.run_service_selector", lambda *args: "REMOTE")
    monkeypatch.setattr("unshackle.core.utils.selector.tui_prompt", lambda *args, **kwargs: "title-id")
    monkeypatch.setattr("unshackle.core.interactive.run_service_extra_options", lambda *args: {})
    monkeypatch.setattr(Services, "get_command", get_command)
    monkeypatch.setattr(Services, "list_commands", lambda self, ctx: ["REMOTE"])
    monkeypatch.setattr(
        Services, "load", lambda *args: pytest.fail("interactive routing bypassed Services.get_command")
    )
    monkeypatch.setattr(dl, "__init__", lambda self, ctx, **kwargs: initialized.append(kwargs))

    ctx = dl.cli.make_context("dl", ["--interactive", "--remote", "--server", "box", "--tag", "MYGROUP"])
    with ctx:
        result = dl.cli.callback(**ctx.params)

    assert result == "title-id"
    assert calls == [(ctx, "REMOTE")]
    assert ctx.params["remote"] is True
    assert ctx.params["server"] == "box"
    assert initialized[0]["interactive_service"] == "REMOTE"
    assert initialized[0]["tag"] == "MYGROUP"


def test_interactive_service_options_do_not_overwrite_download_options(monkeypatch: pytest.MonkeyPatch) -> None:
    @click.command(name="REMOTE")
    @click.argument("title")
    @click.option("--lang", type=click.Choice(["en", "fr"]), default="en")
    def remote_service(title: str, lang: str) -> tuple[str, str]:
        return title, lang

    monkeypatch.setattr("unshackle.core.interactive.run_service_selector", lambda *args: "REMOTE")
    monkeypatch.setattr("unshackle.core.utils.selector.tui_prompt", lambda *args, **kwargs: "title-id")
    monkeypatch.setattr("unshackle.core.interactive.run_service_extra_options", lambda *args: {"lang": "fr"})
    monkeypatch.setattr(Services, "get_command", lambda self, ctx, name: remote_service)
    monkeypatch.setattr(Services, "list_commands", lambda self, ctx: ["REMOTE"])
    monkeypatch.setattr(dl, "__init__", lambda self, ctx, **kwargs: None)

    ctx = dl.cli.make_context("dl", ["--interactive", "--lang", "en"])
    with ctx:
        result = dl.cli.callback(**ctx.params)

    assert result == ("title-id", "fr")
    assert ctx.params["lang"] == ["en"]


def test_global_cli_option_does_not_supply_a_same_named_service_option(monkeypatch: pytest.MonkeyPatch) -> None:
    @click.command()
    @click.option("--lang", type=click.Choice(["original", "dubbed"]), default="original")
    def service(lang: str) -> str:
        return lang

    ctx = dl.cli.make_context("dl", ["--interactive", "--lang", "en"])
    monkeypatch.setattr("unshackle.core.interactive.tui_confirm", lambda *args, **kwargs: False)

    values = run_service_extra_options(service, ctx)

    assert values == {}
    with ctx:
        assert ctx.invoke(service, **values) == "original"


def test_service_option_prompts_preserve_click_types(monkeypatch: pytest.MonkeyPatch) -> None:
    @click.command()
    @click.option("--limit", type=int, default=2)
    def service(limit: int) -> int:
        return limit

    monkeypatch.setattr("unshackle.core.interactive.tui_confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr("unshackle.core.interactive.tui_prompt", lambda *args, **kwargs: "7")

    assert run_service_extra_options(service, click.Context(service)) == {"limit": 7}


def test_repeatable_choice_service_option_preserves_click_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    @click.command()
    @click.option("--device", type=click.Choice(["tv", "web"]), multiple=True)
    def service(device: tuple[str, ...]) -> tuple[str, ...]:
        return device

    monkeypatch.setattr("unshackle.core.interactive.tui_confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(Selector, "run", lambda self: [0, 1])

    assert run_service_extra_options(service, click.Context(service)) == {"device": ("tv", "web")}


def test_repeatable_choice_service_option_can_clear_its_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    @click.command()
    @click.option("--device", type=click.Choice(["tv", "web"]), multiple=True, default=("tv",))
    def service(device: tuple[str, ...]) -> tuple[str, ...]:
        return device

    monkeypatch.setattr("unshackle.core.interactive.tui_confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(Selector, "run", lambda self: [])

    assert run_service_extra_options(service, click.Context(service)) == {"device": ()}


def test_repeatable_text_service_option_preserves_click_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    @click.command()
    @click.option("--region", multiple=True, default=("us",))
    def service(region: tuple[str, ...]) -> tuple[str, ...]:
        return region

    monkeypatch.setattr("unshackle.core.interactive.tui_confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr("unshackle.core.interactive.tui_prompt", lambda *args, **kwargs: "us, ca")

    assert run_service_extra_options(service, click.Context(service)) == {"region": ("us", "ca")}


@pytest.mark.parametrize(
    "option",
    [click.option("--device", type=click.Choice(["tv", "web"]), default="tv"), click.option("--debug", is_flag=True)],
)
def test_service_option_selector_cancellation_aborts(monkeypatch: pytest.MonkeyPatch, option: Any) -> None:
    @click.command()
    def service() -> None:
        pass

    service = option(service)
    monkeypatch.setattr("unshackle.core.interactive.tui_confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(Selector, "run", lambda self: setattr(self, "cancelled", True) or [])

    with pytest.raises(click.Abort):
        run_service_extra_options(service, click.Context(service))


def test_dry_run_applies_codec_and_range_before_the_download_lists_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    chosen_video = video(range_=Video.Range.DV)
    service = FakeService(FakeTracks(videos=[chosen_video]))
    title = SimpleNamespace(tracks=FakeTracks())
    codec_index = list(Video.Codec).index(Video.Codec.HEVC) + 2
    range_index = list(Video.Range).index(Video.Range.DV) + 2

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [4])
    prompt_answers(monkeypatch, [str(codec_index), str(range_index)])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["list_"] is True
    assert selections["vcodec"] == [Video.Codec.HEVC]
    assert selections["range_"] == [Video.Range.DV]
    assert service.track_request.codecs == [Video.Codec.HEVC]
    assert service.track_request.ranges == [Video.Range.DV]
    assert service.calls == 1
    assert not title.tracks.videos


def test_latest_episode_dry_run_disables_title_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(FakeTracks(videos=[video()]))
    titles = Series(
        [
            Episode(id_=f"episode-{number}", service=FakeService, title="Show", season=1, number=number)
            for number in (1, 2)
        ]
    )
    for title in titles:
        title.tracks = None

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [4, 6])
    prompt_answers(monkeypatch, ["1", "1"])

    selections = run_interactive_session(service, titles, logging.getLogger("test"))

    assert selections["list_"] is True
    assert selections["latest_episode"] is True
    assert selections["select_titles"] is False


def test_latest_split_episode_uses_batch_video_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    class SplitService(FakeService):
        def get_tracks(self, title: Episode) -> FakeTracks:
            return FakeTracks(videos=[video(bitrate=5_000_000 if title.part == 1 else 8_000_000)])

    service = SplitService(FakeTracks())
    titles = Series(
        [
            Episode(
                id_=f"episode-part-{part}",
                service=SplitService,
                title="Show",
                season=1,
                number=1,
                part=part,
            )
            for part in (1, 2)
        ]
    )
    for title in titles:
        title.tracks = None

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [6])
    prompt_answers(monkeypatch, ["1", "1", "1", "1"])

    selections = run_interactive_session(service, titles, logging.getLogger("test"))

    assert selections["latest_episode"] is True
    assert selections["video_profile"]["single"] is False
    assert selections["vbitrate"] is None


def test_normal_run_discards_preview_tracks_before_download(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(FakeTracks(videos=[video()]))
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "1"])

    run_interactive_session(service, title, logging.getLogger("test"))

    assert not title.tracks.videos


def test_unavailable_video_filter_resets_to_the_available_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(FakeTracks(videos=[video(codec=Video.Codec.AVC)]))
    title = SimpleNamespace(tracks=FakeTracks())
    hevc_index = list(Video.Codec).index(Video.Codec.HEVC) + 2

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, [str(hevc_index), "1", "1", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["vcodec"] == [Video.Codec.AVC]
    assert selections["range_"] == [Video.Range.SDR]
    assert service.track_request.codecs == []
    assert service.track_request.ranges == []
    assert selections["video_profile"] is not None


def test_unavailable_video_manifest_resets_to_the_available_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    class ManifestGatedService(FakeService):
        def get_tracks(self, title: Any) -> FakeTracks:
            del title
            self.calls += 1
            if self.track_request.codecs == [Video.Codec.HEVC]:
                raise ValueError("HEVC manifest unavailable")
            return self.tracks

    service = ManifestGatedService(FakeTracks(videos=[video(codec=Video.Codec.AVC)]))
    title = SimpleNamespace(tracks=FakeTracks())
    hevc_index = list(Video.Codec).index(Video.Codec.HEVC) + 2

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, [str(hevc_index), "1", "1", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["vcodec"] == [Video.Codec.AVC]
    assert selections["range_"] == [Video.Range.SDR]
    assert service.track_request.codecs == []
    assert service.track_request.ranges == []
    assert selections["video_profile"] is not None


def test_hybrid_selection_keeps_the_composite_range(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(FakeTracks(videos=[video(range_=Video.Range.HDR10), video(range_=Video.Range.DV)]))
    title = SimpleNamespace(tracks=FakeTracks())
    hybrid_index = list(Video.Range).index(Video.Range.HYBRID) + 2

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", str(hybrid_index), "1", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["range_"] == [Video.Range.HYBRID]
    assert selections["vbitrate"] is None


def test_forced_subtitles_refresh_the_remote_session_before_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    class RemoteService(FakeService):
        def __init__(self) -> None:
            super().__init__(FakeTracks())
            self.refreshes: list[tuple[Any, Any, Any]] = []

        def refresh_track_filters(self, codecs: Any, ranges: Any, forced_subs: Any = None) -> None:
            self.refreshes.append((codecs, ranges, forced_subs))

    service = RemoteService()
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [1])
    prompt_answers(monkeypatch, ["1", "1", "1", "1"])

    run_interactive_session(service, title, logging.getLogger("test"))

    assert service.refreshes[0] == (None, None, True)


def test_forced_subtitles_only_offers_forced_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = SimpleNamespace(language="en", codec=Subtitle.Codec.WebVTT, forced=False, sdh=False, cc=False)
    forced = SimpleNamespace(language="en", codec=Subtitle.Codec.WebVTT, forced=True, sdh=False, cc=False)
    service = FakeService(FakeTracks(subtitles=[plain, forced]))
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [1])
    prompt_answers(monkeypatch, ["1", "1", "1", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["subtitle_profiles"] == [
        {"language": "en", "codec": Subtitle.Codec.WebVTT, "forced": True, "sdh": False, "cc": False}
    ]


def test_preview_uses_the_shared_probed_track_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    tracks = FakeTracks(videos=[video(range_=Video.Range.HDR10)])
    service = FakeService(tracks)
    title = SimpleNamespace(tracks=FakeTracks())
    calls: list[tuple[Any, Any]] = []

    def probed(service_arg: Any, title_arg: Any) -> FakeTracks:
        calls.append((service_arg, title_arg))
        return tracks

    monkeypatch.setattr("unshackle.core.interactive.probed_tracks", probed, raising=False)
    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [4])
    prompt_answers(monkeypatch, ["1", "1"])

    run_interactive_session(service, title, logging.getLogger("test"))

    assert calls == [(service, title)]
    assert service.calls == 0


def test_remote_preview_does_not_require_a_local_track_request(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRemoteService:
        session = None

        def __init__(self) -> None:
            self.calls = 0

        def get_tracks(self, title: Any) -> FakeTracks:
            del title
            self.calls += 1
            return FakeTracks(videos=[video(codec=Video.Codec.AVC)])

    service = FakeRemoteService()
    title = SimpleNamespace(tracks=FakeTracks())
    codec_index = list(Video.Codec).index(Video.Codec.AVC) + 2

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, [str(codec_index), "1", "1", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["vcodec"] == [Video.Codec.AVC]
    assert service.calls == 1


def test_remote_dry_run_refreshes_the_session_track_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRemoteService:
        session = None

        def __init__(self) -> None:
            self.calls: list[tuple[list[Video.Codec], list[Video.Range]]] = []

        def get_tracks(self, title: Any) -> FakeTracks:
            del title
            return FakeTracks(videos=[video(codec=Video.Codec.HEVC, range_=Video.Range.SDR)])

        def refresh_track_filters(self, codecs: list[Video.Codec], ranges: list[Video.Range]) -> None:
            self.calls.append((codecs, ranges))

    service = FakeRemoteService()
    title = SimpleNamespace(tracks=FakeTracks())
    codec_index = list(Video.Codec).index(Video.Codec.HEVC) + 2
    range_index = list(Video.Range).index(Video.Range.DV) + 2

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [4])
    prompt_answers(monkeypatch, [str(codec_index), str(range_index)])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["list_"] is True
    assert service.calls == [([Video.Codec.HEVC], [Video.Range.DV])]


def test_codec_only_selection_preserves_the_default_range(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(FakeTracks(videos=[video(codec=Video.Codec.AVC)]))
    service.track_request.ranges = [Video.Range.SDR]
    title = SimpleNamespace(tracks=FakeTracks())
    codec_index = list(Video.Codec).index(Video.Codec.AVC) + 2

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, [str(codec_index), "1", "1", "1"])

    run_interactive_session(service, title, logging.getLogger("test"))

    assert service.track_request.ranges == [Video.Range.SDR]


def test_single_title_selection_returns_the_chosen_video_and_audio_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(FakeTracks(videos=[video()], audio=[audio()]))
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "1", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["quality"] == [1080]
    assert selections["vcodec"] == [Video.Codec.HEVC]
    assert selections["range_"] == [Video.Range.SDR]
    assert selections["v_mode"] == "exact"
    assert selections["vbitrate"] == 8_000
    assert selections["acodec"] == [Audio.Codec.EC3]
    assert selections["a_lang"] == ["en"]
    assert selections["abitrate"] is None
    assert selections["no_subs"] is True


def test_single_title_selection_records_the_exact_video_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(FakeTracks(videos=[video(bitrate=8_200_000), video(bitrate=8_000_000, fps=60.0)]))
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["video_profile"] == {
        "id": "video",
        "language": "en",
        "range": Video.Range.SDR,
        "bitrate": 8_000_000,
        "fps": 60.0,
        "rank": 1,
        "single": True,
    }


def test_batch_selection_uses_a_transferable_resolution_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    cropped = video()
    cropped.height = 800
    service = FakeService(FakeTracks(videos=[cropped]))
    titles = [SimpleNamespace(tracks=FakeTracks()), SimpleNamespace(tracks=FakeTracks())]

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "1", "1"])

    selections = run_interactive_session(service, titles, logging.getLogger("test"))

    assert selections["quality"] == [1080]


def test_batch_selection_uses_width_when_video_height_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    width_only = video()
    width_only.height = None
    service = FakeService(FakeTracks(videos=[width_only]))
    titles = [SimpleNamespace(tracks=FakeTracks()), SimpleNamespace(tracks=FakeTracks())]

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "1", "1"])

    selections = run_interactive_session(service, titles, logging.getLogger("test"))

    assert selections["quality"] == [1080]
    assert selections["video_profile"]["rank"] == 0


def test_batch_video_rank_is_scoped_to_the_selected_language(monkeypatch: pytest.MonkeyPatch) -> None:
    french = video(bitrate=10_000_000)
    french.language = "fr"
    english_high = video(bitrate=8_000_000)
    english_high.language = "en"
    english_low = video(bitrate=6_000_000)
    english_low.language = "en"
    service = FakeService(FakeTracks(videos=[french, english_high, english_low]))
    titles = [SimpleNamespace(tracks=FakeTracks()), SimpleNamespace(tracks=FakeTracks())]

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "2", "1"])

    selections = run_interactive_session(service, titles, logging.getLogger("test"))

    assert selections["video_profile"]["language"] == "en"
    assert selections["video_profile"]["rank"] == 0


def test_batch_video_rank_uses_the_transferable_resolution_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    cropped = video(bitrate=6_000_000)
    cropped.id = "cropped"
    cropped.height = 800
    full = video(bitrate=8_000_000)
    full.id = "full"
    service = FakeService(FakeTracks(videos=[cropped, full]))
    titles = [SimpleNamespace(tracks=FakeTracks()), SimpleNamespace(tracks=FakeTracks())]

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "1", "1"])

    selections = run_interactive_session(service, titles, logging.getLogger("test"))

    assert selections["quality"] == [1080]
    assert selections["video_profile"]["rank"] == 1
    assert select_interactive_video([cropped, full], selections["video_profile"], worst=False) is cropped


def test_audio_selection_preserves_the_chosen_channel_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(FakeTracks(audio=[audio(channels=5.1), audio(channels=2.0)]))
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["audio_profiles"] == [
        {
            "id": "audio",
            "language": "en",
            "codec": Audio.Codec.EC3,
            "bitrate": 640_000,
            "channels": 2.0,
            "descriptive": False,
            "atmos": False,
            "rank": 0,
            "single": True,
        }
    ]


def test_audio_selection_preserves_distinct_profiles_at_the_same_bitrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(FakeTracks(audio=[audio(channels=2.0), audio(channels=5.1)]))
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "1 2"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert [profile["channels"] for profile in selections["audio_profiles"]] == [2.0, 5.1]


@pytest.mark.parametrize("channel_layout", [None, "5.1.4"])
def test_audio_selection_accepts_unknown_and_immersive_channels(
    monkeypatch: pytest.MonkeyPatch, channel_layout: str | None
) -> None:
    service = FakeService(FakeTracks(audio=[audio(channels=channel_layout)]))
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["audio_profiles"][0]["channels"] == channel_layout


def test_audio_selection_reprompts_after_invalid_indices(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(FakeTracks(audio=[audio()]))
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "99", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["a_lang"] == ["en"]
    assert len(selections["audio_profiles"]) == 1


def test_forced_subtitles_only_sets_the_engine_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(FakeTracks())
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [1])
    prompt_answers(monkeypatch, ["1", "1", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["forced_subs_only"] is True
    assert selections["no_subs"] is False
    assert "forced_subs" not in selections


def test_subtitle_selection_preserves_the_chosen_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = SimpleNamespace(language="en", codec=Subtitle.Codec.WebVTT, forced=False, sdh=False, cc=False)
    forced = SimpleNamespace(language="en", codec=Subtitle.Codec.WebVTT, forced=True, sdh=False, cc=False)
    service = FakeService(FakeTracks(subtitles=[plain, forced]))
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "2"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["subtitle_profiles"] == [
        {"language": "en", "codec": Subtitle.Codec.WebVTT, "forced": True, "sdh": False, "cc": False}
    ]


def test_subtitle_selection_reprompts_after_invalid_nonempty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    subtitle = SimpleNamespace(language="en", codec=Subtitle.Codec.WebVTT, forced=False, sdh=False, cc=False)
    service = FakeService(FakeTracks(subtitles=[subtitle]))
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1", "99", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["s_lang"] == ["en"]


def test_cancelling_customizations_aborts_before_fetching_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(FakeTracks())
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr(Selector, "get_input_unix", lambda self: "CANCEL")
    monkeypatch.setattr(
        "unshackle.core.interactive.tui_prompt",
        lambda *args, **kwargs: pytest.fail("track prompts must not run after cancellation"),
    )

    with pytest.raises(click.Abort):
        run_interactive_session(service, title, logging.getLogger("test"))

    assert service.calls == 0


def test_empty_manifest_skips_track_selection_without_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(FakeTracks())
    title = SimpleNamespace(tracks=FakeTracks())

    monkeypatch.setattr("unshackle.core.interactive.Selector.run", lambda self: [])
    prompt_answers(monkeypatch, ["1", "1", "1"])

    selections = run_interactive_session(service, title, logging.getLogger("test"))

    assert selections["vcodec"] == []
    assert selections["a_lang"] == ["all"]
    assert selections["no_subs"] is True


def test_download_help_lists_interactive_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core import services

    monkeypatch.setattr(services, "SERVICES", [])
    monkeypatch.setattr(services, "MODULES", {})
    monkeypatch.setattr(services, "ALIASES", {})
    monkeypatch.setattr(services, "LOAD_ERRORS", [])
    monkeypatch.setattr(services, "DIRTY_REPOS", [])
    result = CliRunner().invoke(dl.cli, ["--help"])

    assert result.exit_code == 0
    assert "--interactive" in result.output
    assert "Enable interactive" in result.output
