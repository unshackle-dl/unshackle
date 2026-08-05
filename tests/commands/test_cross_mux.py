"""Unit and offline end-to-end tests for cross-service muxing.

The ``dl`` cross-mux layer takes some track types from a second service and muxes them with the
first. These tests cover the behaviour that is easy to get wrong:

- ``drm_source`` routes each track to the service, title, CDM and vaults that licence it, so a
  cross-sourced track licenses through its own service and a primary track through the primary's.
- Keys from a cross-service cache under that service's own vault, never the primary's.
- Audio and subtitles merge into the primary's own tracks (video and chapters replace), and an
  explicit id shared by both services is namespaced so the two never collide.
- A ``sync_offset_ms`` on a track becomes ``--sync 0:<ms>`` in the built mkvmerge command, and is
  absent when no offset is set.
- ``match_cross_title`` honours the per-type ``--cross-*-wanted`` override, falling back to the
  shared ``--cross-wanted`` and then to the matching season/episode.
- The new CLI options parse and bind.

The offline end-to-end test stands up a synthetic cross-service (its titles and tracks held in
memory, so no network or manifest parsing is needed) and drives ``fetch_cross_tracks`` ->
``apply_cross_tracks`` -> ``Tracks.mux``, asserting the cross track is merged, muxed with its
offset, and routed through the correct service for DRM.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Optional

import click
import pytest

from unshackle.commands.dl import dl
from unshackle.core.titles.episode import Episode
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video
from unshackle.core.tracks import tracks as tracks_mod

pytestmark = pytest.mark.unit


class Svc:
    """Stand-in for a service class, only used to satisfy the Title ``service`` type check."""


class CrossSvc:
    """A minimal cross-service: its class name drives namespacing and the ``cross_source`` tag."""

    def __init__(self, titles: Optional[list] = None, tracks: Optional[Tracks] = None) -> None:
        self.session = object()
        self._titles = titles or []
        self._tracks = tracks or Tracks()

    def get_titles(self) -> list:
        return self._titles

    def get_tracks(self, title: Any) -> Tracks:
        return self._tracks

    def get_chapters(self, title: Any) -> list:
        return []


class FakeVaults:
    """Records added keys so we can prove which vault a key landed in."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.keys: dict[str, str] = {}

    def add_key(self, kid: str, key: str, excluding: Any = None) -> int:
        self.keys[kid] = key
        return 1

    def get_key(self, kid: str) -> tuple[Optional[str], None]:
        return self.keys.get(kid), None


def make_audio(track_id: str, *, language: str = "en", offset: Optional[int] = None, path: Any = None) -> Audio:
    track = Audio(
        id_=track_id,
        url=f"https://example.test/{track_id}.m4a",
        language=language,
        codec=Audio.Codec.AAC,
        bitrate=128000,
    )
    if offset is not None:
        track.data["sync_offset_ms"] = offset
    if path is not None:
        path.touch()  # mkvmerge command building requires the track file to exist
        track.path = path
    return track


def make_subtitle(track_id: str, *, language: str = "en", offset: Optional[int] = None, path: Any = None) -> Subtitle:
    track = Subtitle(
        id_=track_id,
        url=f"https://example.test/{track_id}.vtt",
        language=language,
        codec=Subtitle.Codec.WebVTT,
    )
    if offset is not None:
        track.data["sync_offset_ms"] = offset
    if path is not None:
        path.touch()  # mkvmerge command building requires the track file to exist
        track.path = path
    return track


def make_video(track_id: str, *, language: str = "en") -> Video:
    return Video(
        id_=track_id,
        url=f"https://example.test/{track_id}.m3u8",
        language=language,
        codec=Video.Codec.HEVC,
        range_=Video.Range.SDR,
        width=1920,
        height=1080,
        bitrate=5000000,
    )


def make_dl(**overrides: Any) -> dl:
    """Build a bare ``dl`` with only the attributes the cross-mux methods read (no heavy __init__)."""
    inst = dl.__new__(dl)
    inst.log = logging.getLogger("test.cross")
    inst.cross_video = None
    inst.cross_audio = None
    inst.cross_subtitles = None
    inst.cross_chapters = None
    inst.cross_audio_offset = None
    inst.cross_subtitle_offset = None
    inst.cross_wanted = None
    inst.cross_wanted_by_type = {"video": None, "audio": None, "subtitles": None, "chapters": None}
    inst.cross_track_sources = {}
    inst.cdm = object()
    inst.vaults = object()
    for key, value in overrides.items():
        setattr(inst, key, value)
    return inst


def capture_mux_command(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch mkvmerge discovery and Popen so ``Tracks.mux`` builds a command without running it."""
    captured: dict = {}

    class FakeStdout:
        def readline(self) -> str:
            return ""

    class FakePopen:
        def __init__(self, cmd: list, **_: Any) -> None:
            captured["cmd"] = cmd
            self.stdout = FakeStdout()

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(tracks_mod.binaries, "MKVToolNix", "/usr/bin/mkvmerge")
    monkeypatch.setattr(tracks_mod.subprocess, "Popen", FakePopen)
    return captured


def test_drm_source_routes_cross_and_primary() -> None:
    d = make_dl()
    primary_service = SimpleNamespace(session=object())
    primary_title = object()
    cross_service = SimpleNamespace(session=object())
    cross_title = object()
    cross_cdm = object()
    cross_vaults = object()

    cross_track = make_audio("cross-1", language="ja")
    d.cross_track_sources[cross_track.id] = (cross_service, cross_title, cross_cdm, cross_vaults)

    assert d.drm_source(cross_track, primary_service, primary_title) == (
        cross_service,
        cross_title,
        cross_cdm,
        cross_vaults,
    )

    primary_track = make_audio("prim-1")
    assert d.drm_source(primary_track, primary_service, primary_title) == (
        primary_service,
        primary_title,
        d.cdm,
        d.vaults,
    )


def test_cross_key_caches_under_owning_vault() -> None:
    primary_vaults = FakeVaults("primary")
    cross_vaults = FakeVaults("cross")
    d = make_dl(vaults=primary_vaults)

    cross_service = CrossSvc()
    cross_track = make_audio("cross-1", language="ja")
    d.cross_track_sources[cross_track.id] = (cross_service, object(), object(), cross_vaults)

    # download_track resolves the owning vault through drm_source and threads it into prepare_drm,
    # which caches licensed keys to exactly that vault.
    _, _, _, vaults = d.drm_source(cross_track, SimpleNamespace(session=object()), object())
    assert vaults is cross_vaults

    vaults.add_key("kid-1", "deadbeef")
    assert cross_vaults.keys == {"kid-1": "deadbeef"}
    assert primary_vaults.keys == {}


def test_apply_cross_tracks_merges_audio_and_selection_sees_it() -> None:
    d = make_dl()
    title = Episode(id_="primary-ep-merge", service=Svc, title="Show", season=1, number=1, language="en")
    title.tracks.audio.append(make_audio("prim-en", language="en"))

    cross_service = CrossSvc()
    cross_tracks = Tracks()
    cross_tracks.audio.append(make_audio("cross-ja", language="ja"))
    sources = {"audio": (cross_service, object(), cross_tracks, object(), object())}

    d.apply_cross_tracks(title, sources)

    assert sorted(str(a.language) for a in title.tracks.audio) == ["en", "ja"]
    assert any(a.id == "prim-en" for a in title.tracks.audio)  # primary retained, not replaced
    picked = Tracks.by_language(title.tracks.audio, ["ja"])
    assert picked and picked[0].id == "cross-ja"


def test_apply_cross_tracks_namespaces_colliding_ids() -> None:
    d = make_dl()
    title = Episode(id_="primary-ep-clash", service=Svc, title="Show", season=1, number=1, language="en")
    title.tracks.audio.append(make_audio("dup", language="en"))

    cross_service = CrossSvc()
    cross_tracks = Tracks()
    cross_tracks.audio.append(make_audio("dup", language="ja"))  # explicit id shared with the primary's
    sources = {"audio": (cross_service, object(), cross_tracks, object(), object())}

    d.apply_cross_tracks(title, sources)

    assert sorted(a.id for a in title.tracks.audio) == ["dup", "dup_crosssvc"]
    # The cross track is routed by its namespaced id; the primary track is not recorded as cross.
    assert set(d.cross_track_sources) == {"dup_crosssvc"}


def test_apply_cross_tracks_namespaces_video_against_primary_ids() -> None:
    # A cross video sharing an id with a surviving primary track must not shadow that track's
    # drm_source entry, or the primary track would license through the cross service.
    d = make_dl()
    title = Episode(id_="primary-ep-vclash", service=Svc, title="Show", season=1, number=1, language="en")
    title.tracks.audio.append(make_audio("dup", language="en"))

    cross_tracks = Tracks()
    cross_tracks.videos.append(make_video("dup"))
    sources = {"video": (CrossSvc(), object(), cross_tracks, object(), object())}

    d.apply_cross_tracks(title, sources)

    assert [v.id for v in title.tracks.videos] == ["dup_crosssvc"]
    assert set(d.cross_track_sources) == {"dup_crosssvc"}
    prim = title.tracks.audio[0]
    assert d.drm_source(prim, "PRIMARY", title) == ("PRIMARY", title, d.cdm, d.vaults)


def test_fetch_cross_tracks_clears_stale_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    # Titles are processed one after another; an id recorded for the previous episode must not
    # linger and shadow one of the next episode's own tracks.
    import unshackle.commands.dl as dl_mod

    monkeypatch.setattr(dl_mod.Services, "get_tag", lambda name: name)

    d = make_dl(cross_audio=("CROSS", "https://cross/1"))
    d.cross_track_sources["stale"] = (object(), object(), object(), object())
    monkeypatch.setattr(d, "load_cross_service", lambda tag, url: (CrossSvc(), object(), object()))

    title = Episode(id_="primary-ep-stale", service=Svc, title="Show", season=1, number=2, language="en")
    assert d.fetch_cross_tracks(title) == {}  # cross catalogue has no S01E02
    assert d.cross_track_sources == {}


def test_apply_cross_tracks_replaces_video() -> None:
    d = make_dl()
    title = Episode(id_="primary-ep-video", service=Svc, title="Show", season=1, number=1, language="en")
    title.tracks.videos.append(make_video("prim-v"))

    cross_tracks = Tracks()
    cross_tracks.videos.append(make_video("cross-v"))
    sources = {"video": (CrossSvc(), object(), cross_tracks, object(), object())}

    d.apply_cross_tracks(title, sources)

    assert [v.id for v in title.tracks.videos] == ["cross-v"]


def test_apply_cross_tracks_records_offset() -> None:
    d = make_dl(cross_audio_offset=7500)
    title = Episode(id_="primary-ep-offset", service=Svc, title="Show", season=1, number=1, language="en")

    cross_service = CrossSvc()
    cross_tracks = Tracks()
    cross_tracks.audio.append(make_audio("cross-ja", language="ja"))
    sources = {"audio": (cross_service, object(), cross_tracks, object(), object())}

    d.apply_cross_tracks(title, sources)

    cross = next(a for a in title.tracks.audio if a.id == "cross-ja")
    assert cross.data["sync_offset_ms"] == 7500


def test_audio_offset_emits_sync(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    captured = capture_mux_command(monkeypatch)
    track = make_audio("a-off", offset=10000, path=tmp_path / "a.m4a")
    Tracks(track).mux("Title", delete=False)

    cmd = captured["cmd"]
    assert "--sync" in cmd
    assert cmd[cmd.index("--sync") + 1] == "0:10000"


def test_subtitle_offset_emits_sync(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    captured = capture_mux_command(monkeypatch)
    track = make_subtitle("s-off", offset=-2500, path=tmp_path / "s.vtt")
    Tracks(track).mux("Title", delete=False)

    cmd = captured["cmd"]
    assert "--sync" in cmd
    assert cmd[cmd.index("--sync") + 1] == "0:-2500"


def test_no_offset_no_sync(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    captured = capture_mux_command(monkeypatch)
    track = make_audio("a-plain", path=tmp_path / "a.m4a")
    Tracks(track).mux("Title", delete=False)

    assert "--sync" not in captured["cmd"]


def test_match_cross_title_per_type_override_and_fallback() -> None:
    primary = Episode(id_="primary-ep-match", service=Svc, title="Show", season=1, number=1, language="en")
    catalogue = [
        Episode(id_="cross-ep-0101", service=Svc, title="Show", season=1, number=1, language="en"),
        Episode(id_="cross-ep-0205", service=Svc, title="Show", season=2, number=5, language="en"),
    ]

    # Default: the same season and episode as the title being downloaded.
    assert make_dl().match_cross_title(primary, catalogue, "audio").id == "cross-ep-0101"

    # Per-type override wins over the default.
    per_type = {"video": None, "audio": "S02E05", "subtitles": None, "chapters": None}
    assert make_dl(cross_wanted_by_type=per_type).match_cross_title(primary, catalogue, "audio").id == "cross-ep-0205"

    # A type with no per-type override falls back to the shared --cross-wanted.
    shared = make_dl(cross_wanted="S02E05")
    assert shared.match_cross_title(primary, catalogue, "video").id == "cross-ep-0205"

    # Per-type override beats the shared fallback for its own type.
    mixed = make_dl(
        cross_wanted="S02E05",
        cross_wanted_by_type={"video": None, "audio": "S01E01", "subtitles": None, "chapters": None},
    )
    assert mixed.match_cross_title(primary, catalogue, "audio").id == "cross-ep-0101"


def test_match_cross_title_rejects_bad_wanted() -> None:
    primary = Episode(id_="primary-ep-bad", service=Svc, title="Show", season=1, number=1, language="en")
    with pytest.raises(click.UsageError):
        make_dl(cross_wanted="episode two").match_cross_title(primary, [], "audio")


def test_cross_cli_options_parse() -> None:
    command = click.Command("dl", params=list(dl.cli.params), callback=lambda **_: None)
    ctx = command.make_context(
        "dl",
        [
            "--cross-proxy",
            "http://p:8080",
            "--cross-audio-wanted",
            "S02E05",
            "--cross-audio-offset",
            "10s",
            "--cross-video",
            "SVC",
            "https://example.test/1",
        ],
    )
    assert ctx.params["cross_proxy"] == "http://p:8080"
    assert ctx.params["cross_audio_wanted"] == "S02E05"
    assert ctx.params["cross_audio_offset"] == 10000
    assert ctx.params["cross_video"] == ("SVC", "https://example.test/1")


def test_cross_mux_end_to_end(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import unshackle.commands.dl as dl_mod

    monkeypatch.setattr(dl_mod.Services, "get_tag", lambda name: name)

    primary = Episode(id_="primary-ep-e2e", service=Svc, title="Show", season=1, number=1, language="en")
    primary.tracks.audio.append(make_audio("prim-en", language="en", path=tmp_path / "prim_en.m4a"))

    cross_episode = Episode(id_="cross-ep-e2e", service=Svc, title="Show", season=1, number=1, language="ja")
    cross_tracks = Tracks()
    cross_tracks.audio.append(make_audio("cross-ja", language="ja", path=tmp_path / "cross_ja.m4a"))
    cross_service = CrossSvc(titles=[cross_episode], tracks=cross_tracks)
    cross_cdm = object()
    cross_vaults = object()

    d = make_dl(cross_audio=("CROSS", "https://cross/1"), cross_audio_offset=5000)
    monkeypatch.setattr(d, "load_cross_service", lambda tag, url: (cross_service, cross_cdm, cross_vaults))

    sources = d.fetch_cross_tracks(primary)
    assert set(sources) == {"audio"}

    d.apply_cross_tracks(primary, sources)

    # Merged, not replaced: both languages present.
    assert {str(a.language) for a in primary.tracks.audio} == {"en", "ja"}

    # DRM routes the cross track through the cross service's own service/cdm/vaults.
    cross_track = next(a for a in primary.tracks.audio if str(a.language) == "ja")
    svc, ttl, cdm, vaults = d.drm_source(cross_track, SimpleNamespace(session=object()), primary)
    assert svc is cross_service
    assert ttl is cross_episode
    assert cdm is cross_cdm
    assert vaults is cross_vaults

    # The primary track still routes to the primary's own service/cdm/vaults.
    prim_track = next(a for a in primary.tracks.audio if str(a.language) == "en")
    assert d.drm_source(prim_track, "PRIMARY", primary) == ("PRIMARY", primary, d.cdm, d.vaults)

    # The built mux command carries the cross audio's offset.
    captured = capture_mux_command(monkeypatch)
    primary.tracks.mux("Show S01E01", delete=False)
    cmd = captured["cmd"]
    assert "--sync" in cmd
    assert cmd[cmd.index("--sync") + 1] == "0:5000"
