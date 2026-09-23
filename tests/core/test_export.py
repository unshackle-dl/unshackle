"""Tests for ``dl.write_export``, the ``--export`` mediaexport sidecar.

Regression: DRM-free tracks never pass through ``prepare_drm``, so ``write_export``
must accept ``drm=None`` (and DRM systems without ``to_dict``/``content_keys`` such
as ClearKey) and still record the track/manifest/chapter/attachment info that
``unshackle import`` rebuilds a download from.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import click
import pytest

from unshackle.commands.dl import dl
from unshackle.core.drm import drm_from_dict
from unshackle.core.drm.clearkey import ClearKey
from unshackle.core.drm.clearkey_cenc import ClearKeyCENC
from unshackle.core.import_service import ImportService
from unshackle.core.titles import Movie
from unshackle.core.tracks import Audio, Chapter, Subtitle, Video

KID = UUID(hex="00000000000000000000000000000001")


class StubService:
    """Stands in for the service class slot on Movie; never instantiated."""


class StubDRM:
    """Minimal licensed-DRM shape: ``to_dict`` plus filled ``content_keys``."""

    def __init__(self) -> None:
        self.content_keys = {KID: "aa" * 16}

    def to_dict(self) -> dict:
        return {"system": "Widevine", "pssh_b64": "AAAA"}


def make_dl() -> dl:
    # __new__ skips the CLI-driven __init__; write_export only needs `service` and `log`.
    instance = dl.__new__(dl)
    instance.service = "EXAMPLE"
    instance.log = logging.getLogger("download")
    return instance


def make_title() -> Movie:
    title = Movie(id_="movie-1", service=StubService, name="Example Movie", year=2024, language="en")
    title.tracks.add(
        Video(
            id_="v1",
            url="https://example.test/v1.mp4",
            language="en",
            codec=Video.Codec.AVC,
            range_=Video.Range.SDR,
            width=1920,
            height=1080,
            bitrate=5_000_000,
        )
    )
    title.tracks.add(
        Audio(
            id_="a1",
            url="https://example.test/a1.mp4",
            language="en",
            codec=Audio.Codec.AAC,
            bitrate=128_000,
        )
    )
    title.tracks.add(
        Subtitle(
            id_="s1",
            url="https://example.test/s1.vtt",
            language="en",
            codec=Subtitle.Codec.WebVTT,
        )
    )
    return title


def import_ctx() -> click.Context:
    # ImportService only touches ctx.parent.params (proxy flags) when building its session.
    return cast(click.Context, SimpleNamespace(parent=None, params={}))


def read_export(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf8"))


def entry(path: Path) -> dict:
    return read_export(path)["titles"][0]


def test_drm_free_track_exports(tmp_path: Path) -> None:
    """The reported bug: DRM-free downloads produced no usable export."""
    export = tmp_path / "export.json"
    title = make_title()
    video = title.tracks.videos[0]

    make_dl().write_export(export, title, video)

    doc = read_export(export)
    assert doc["kind"] == "mediaexport" and doc["version"] == 1
    assert doc["service"]["tag"] == "EXAMPLE"
    tinfo = doc["titles"][0]
    assert tinfo["title"] == "Example Movie" and tinfo["kind"] == "movie"
    assert {row["id"] for row in tinfo["tracks"]} == {"v1", "a1", "s1"}
    assert tinfo["x-unshackle"]["meta"]["name"] == "Example Movie"
    assert set(tinfo["x-unshackle"]["tracks"]) == {"v1"}
    assert "keys" not in tinfo and "drm" not in tinfo


def test_clearkey_drm_exports_track_without_keys(tmp_path: Path) -> None:
    """ClearKey has no to_dict/content_keys; the track info must still export."""
    export = tmp_path / "export.json"
    title = make_title()
    video = title.tracks.videos[0]

    make_dl().write_export(export, title, video, ClearKey(key="bb" * 16))

    tinfo = entry(export)
    assert "keys" not in tinfo and "drm" not in tinfo
    assert "v1" in tinfo["x-unshackle"]["tracks"]


def test_post_download_write_keeps_licensed_keys(tmp_path: Path) -> None:
    """The drm=None write after download must not clobber prepare_drm's DRM/keys."""
    export = tmp_path / "export.json"
    title = make_title()
    video = title.tracks.videos[0]
    runner = make_dl()

    runner.write_export(export, title, video, StubDRM())  # prepare_drm
    runner.write_export(export, title, video)  # post-download hook

    tinfo = entry(export)
    assert tinfo["drm"] == [{"system": "widevine", "pssh": "AAAA"}]
    assert tinfo["keys"] == {KID.hex: "aa" * 16}
    assert tinfo["x-unshackle"]["drm"] == [{"system": "Widevine", "pssh_b64": "AAAA"}]


def test_drm_free_export_roundtrips_through_import_service(tmp_path: Path) -> None:
    """A DRM-free export must rebuild via ImportService: titles, tracks (no DRM),
    empty key pool (resolve_server_keys no-op) and chapters."""
    export = tmp_path / "export.json"
    title = make_title()
    title.tracks.chapters.add(Chapter("00:00:10.000", "Intro"))
    runner = make_dl()
    for track in [*title.tracks.videos, *title.tracks.audio, *title.tracks.subtitles]:
        runner.write_export(export, title, track)

    svc = ImportService(import_ctx(), "EXAMPLE", "movie-1", str(export))

    titles = list(svc.get_titles())
    assert len(titles) == 1
    movie = titles[0]
    assert isinstance(movie, Movie)
    assert movie.name == "Example Movie"
    assert movie.year == 2024

    tracks = svc.get_tracks(movie)
    assert {t.id for t in tracks} == {"v1", "a1", "s1"}
    assert all(not t.drm for t in tracks)

    assert svc.key_pool() == {}
    movie.tracks = tracks
    svc.resolve_server_keys(movie)
    assert all(not t.drm for t in movie.tracks)

    # Chapters.add auto-inserts a nameless 00:00:00 baseline chapter; it round-trips too.
    assert [c.name for c in svc.get_chapters(movie)] == [None, "Intro"]


def test_clearkey_cenc_exports_drm_and_keys(tmp_path: Path) -> None:
    """A licensed ClearKeyCENC exports its system dict and KID:KEY map, and the
    exported DRM dict plus keys rebuild a decrypt-ready instance via drm_from_dict."""
    export = tmp_path / "export.json"
    title = make_title()
    video = title.tracks.videos[0]
    drm = ClearKeyCENC(kids=[KID], laurl="https://license.example.test/ck", content_keys={KID: "cc" * 16})

    make_dl().write_export(export, title, video, drm)

    tinfo = entry(export)
    assert tinfo["drm"] == [{"system": "clearkey"}]
    assert tinfo["keys"] == {KID.hex: "cc" * 16}
    own = tinfo["x-unshackle"]["drm"]
    assert own == [{"system": "ClearKeyCENC", "kids": [KID.hex], "laurl": "https://license.example.test/ck"}]

    rebuilt = drm_from_dict({**own[0], "content_keys": tinfo["keys"]})
    assert isinstance(rebuilt, ClearKeyCENC)
    assert rebuilt.content_keys == {KID: "cc" * 16}


def test_keyless_content_keys_writes_no_keys_entry(tmp_path: Path) -> None:
    """A DRM object with empty content_keys must not create an empty keys map."""
    export = tmp_path / "export.json"
    title = make_title()
    video = title.tracks.videos[0]
    drm = StubDRM()
    drm.content_keys = {}

    make_dl().write_export(export, title, video, drm)

    tinfo = entry(export)
    assert tinfo["drm"] == [{"system": "widevine", "pssh": "AAAA"}]
    assert "keys" not in tinfo


def test_unidl_export_imports_without_track_dicts(tmp_path: Path) -> None:
    """A file from another tool has no x-unshackle block: titles, keys and chapters still rebuild."""
    export = tmp_path / "unidl.json"
    export.write_text(
        json.dumps(
            {
                "kind": "unidl-export",
                "version": 1,
                "service": "example",
                "titles": [
                    {
                        "save_name": "Show.S01E02",
                        "title": {
                            "id": "ep-2",
                            "kind": "episode",
                            "name": "Show",
                            "episode_name": "Two",
                            "season": 1,
                            "episode": 2,
                            "year": "2024",
                        },
                        "keys": [f"{KID.hex}:{'dd' * 16}"],
                        "manifest_url": "https://example.test/m.mpd",
                        "headers": {"User-Agent": "unidl-ua"},
                        "drm": {"system": "widevine", "pssh": "AAAA"},
                        "chapters": [{"start_ms": 5000, "title": "Recap"}],
                    }
                ],
            }
        ),
        encoding="utf8",
    )
    svc = ImportService(import_ctx(), "EXAMPLE", "ep-2", str(export))

    ep = next(iter(svc.get_titles()))
    assert (ep.title, ep.season, ep.number) == ("Show", 1, 2)
    assert svc.session.headers["User-Agent"] == "unidl-ua"
    assert svc.key_pool() == {KID: "dd" * 16}
    assert svc.exported_drm_system() == "widevine"
    assert [c.name for c in svc.get_chapters(ep)] == [None, "Recap"]
    assert svc.titles_data["ep-2"]["manifest_type"] == "DASH"


def crit_export(path: Path, *crit_ids: str) -> Path:
    """A mediaexport file with one usable title per id, except ``crit_ids``, which need ``segments``."""
    titles = [
        {
            "id": tid,
            "kind": "movie",
            "title": tid,
            "manifests": [{"url": f"https://example.test/{tid}.mpd", "type": "dash"}],
            **({"crit": ["segments"]} if tid in crit_ids else {}),
        }
        for tid in ("movie-1", "movie-2")
    ]
    doc = {"kind": "mediaexport", "version": 1, "service": {"tag": "EXAMPLE"}, "titles": titles}
    path.write_text(json.dumps(doc), encoding="utf8")
    return path


def test_import_skips_a_title_it_cannot_use(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A title another tool marked as needing a capability unshackle lacks is named and skipped."""
    export = crit_export(tmp_path / "export.json", "movie-2")

    with caplog.at_level(logging.WARNING, logger="EXAMPLE"):
        svc = ImportService(import_ctx(), "EXAMPLE", "movie-1", str(export))

    assert list(svc.titles_data) == ["movie-1"]
    assert "Skipping exported title 'movie-2'" in caplog.text


def test_import_with_no_usable_title_says_so(tmp_path: Path) -> None:
    """A file whose every title is refused stops with a reason, not an empty download."""
    export = crit_export(tmp_path / "export.json", "movie-1", "movie-2")

    with pytest.raises(click.ClickException, match="no title unshackle can use"):
        ImportService(import_ctx(), "EXAMPLE", "movie-1", str(export))


@pytest.mark.parametrize(("system", "expected"), [("playready", "playready"), ("widevine", "widevine")])
def test_import_takes_its_drm_type_from_the_export(tmp_path: Path, system: str, expected: str) -> None:
    """The type stub routes the DRM objects, so it follows the exported system, not a widevine default."""
    export = crit_export(tmp_path / "export.json")
    doc = read_export(export)
    doc["titles"][0]["drm"] = [{"system": system, "pssh": "AAAA"}]
    export.write_text(json.dumps(doc), encoding="utf8")

    assert ImportService(import_ctx(), "EXAMPLE", "movie-1", str(export))._server_cdm_type == expected


class KeyDRM:
    """A licensed DRM system that holds only ``content_keys``."""

    def __init__(self, key: str) -> None:
        self.content_keys = {KID: key}


def test_export_drops_session_headers(tmp_path: Path) -> None:
    """The file travels between people: the session cookie and bearer token must not go with it."""
    export = tmp_path / "export.json"
    title = make_title()
    title.tracks.manifest_url = "https://example.test/m.mpd"
    runner = make_dl()
    headers = {"User-Agent": "example-ua", "Cookie": "session=secret", "Authorization": "Bearer secret"}
    runner.export_service = cast(Any, SimpleNamespace(session=SimpleNamespace(headers=headers)))

    runner.write_export(export, title, title.tracks.videos[0])

    assert entry(export)["manifests"][0]["headers"] == {"User-Agent": "example-ua"}
    assert "secret" not in export.read_text(encoding="utf8")


def test_second_key_for_one_kid_keeps_the_first(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Two titles that license one KID to different keys: the file keeps the first key and stays readable."""
    export = tmp_path / "export.json"
    first, second = make_title(), make_title()
    second.id = "movie-2"
    runner = make_dl()

    runner.write_export(export, first, first.tracks.videos[0], KeyDRM("aa" * 16))
    with caplog.at_level(logging.WARNING, logger="download"):
        runner.write_export(export, second, second.tracks.videos[0], KeyDRM("bb" * 16))

    titles = read_export(export)["titles"]
    assert titles[0]["keys"] == {KID.hex: "aa" * 16}
    assert "keys" not in titles[1]
    assert f"KID {KID.hex} already has a different key" in caplog.text


def test_uppercase_content_key_is_stored_lowercase(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A licence server that sends uppercase hex still gives one key, not a conflict with itself."""
    export = tmp_path / "export.json"
    first, second = make_title(), make_title()
    second.id = "movie-2"
    runner = make_dl()

    runner.write_export(export, first, first.tracks.videos[0], KeyDRM("AA" * 16))
    with caplog.at_level(logging.WARNING, logger="download"):
        runner.write_export(export, second, second.tracks.videos[0], KeyDRM("AA" * 16))

    titles = read_export(export)["titles"]
    assert titles[0]["keys"] == {KID.hex: "aa" * 16}
    assert "different key" not in caplog.text


@pytest.mark.parametrize("key", ["zz" * 16, "aa" * 8, "aa" * 32])
def test_malformed_content_key_skips_the_key_not_the_download(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, key: str
) -> None:
    """A key that is not 16 bytes of hex, as a new DRM system's tool might return, must not stop licensing."""
    export = tmp_path / "export.json"
    title = make_title()
    title.tracks.manifest_url = "https://example.test/m.mpd"
    runner = make_dl()

    with caplog.at_level(logging.WARNING, logger="download"):
        runner.write_export(export, title, title.tracks.videos[0], KeyDRM(key))

    assert "keys" not in entry(export)
    assert f"Not exporting the key for KID {KID.hex}" in caplog.text
