"""Music post-scripts, which no live service was available to test against.

A Song runs through the same title loop as an Episode, so its post-scripts come from the same
two functions: ``build_context`` for a file and ``season_context`` for the release folder.
These check what those two feed a script: that a Song builds a usable context, that an
album groups the way a season does, and that the album post-script describes the album rather
than whichever track landed last.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from unshackle.core.utils.post_scripts import build_context, dispatch, season_context, substitute, tokenize


class FakeSong(SimpleNamespace):
    """A Song as the post-script sees it: name/artist/album, no season or episode."""

    def build_template_context(self, media_info, show_service=True):
        return {
            "title": self.name,
            "artist": self.artist,
            "album": self.album,
            "track_number": f"{self.track:02}",
            "year": self.year,
            "audio": "FLAC",
            "video": "",
        }


def song(**kwargs):
    defaults = dict(
        name="Example Track",
        artist="Example Artist",
        album="Example Album, Live & Remastered",
        track=4,
        year=1983,
        id="s1",
    )
    return FakeSong(**{**defaults, "id": "s1", **kwargs})


class FakeEpisode(SimpleNamespace):
    """An Episode as the post-script sees it: a show title, a season, and a number."""

    def build_template_context(self, media_info, show_service=True):
        return {"title": self.title, "season": f"{self.season:02}", "audio": "EAC3", "video": "H.265"}


def episode(**kwargs):
    defaults = dict(title="Some Show", season=1, number=5, name="Pilot", year=2024, id="e1")
    return FakeEpisode(**{**defaults, **kwargs})


def test_song_context_has_music_fields_and_blank_episode_fields():
    context = build_context(
        song(),
        media_info=object(),
        filepath=Path("/dl/Example Artist/04. Example Track.flac"),
        service="EXAMPLE",
    )
    assert context["title"] == "Example Track"
    assert context["artist"] == "Example Artist"
    assert context["album"] == "Example Album, Live & Remastered"
    assert context["track_number"] == "04"
    assert context["service"] == "EXAMPLE"
    assert context["filename"] == "04. Example Track.flac"
    assert context["ext"] == ".flac"
    assert context["folder"] == str(Path("/dl/Example Artist"))
    assert context["season"] == ""
    assert context["episode"] == ""


def test_song_context_survives_without_media_info():
    """The failure path and any pre-mux dispatch pass no MediaInfo."""
    context = build_context(song(), None, service="EXAMPLE", error="HTTPError: 403")
    assert context["title"] == "Example Track"
    assert context["error"] == "HTTPError: 403"
    assert context["filepath"] == ""


def test_album_grouping_matches_the_season_grouping_rule():
    """post_script_group's music branch: albums group by (album artist, album)."""
    from unshackle.commands.dl import post_script_group
    from unshackle.core.titles.music import Song

    def real_song(**kwargs):
        defaults = dict(
            id_="track-0001",
            service=type("DummyService", (), {}),
            name="Example Track",
            artist="Example Artist",
            album="Example Album, Live & Remastered",
            track=4,
        )
        return Song(**{**defaults, **kwargs})

    a = real_song(track=1, id_="track-0002")
    b = real_song(track=2, id_="track-0003")
    c = real_song(album="Technique", id_="track-0004")
    assert post_script_group(a) == post_script_group(b)
    assert post_script_group(a) != post_script_group(c)
    assert post_script_group(a)[0] == "album"


@pytest.mark.parametrize("mode", ["season", "run"])
def test_album_and_run_contexts_blank_the_file_variables(mode):
    """Mirrors the blanking dl.py applies before dispatching an album or run post-script."""
    base = build_context(
        song(),
        media_info=object(),
        filepath=Path("/dl/Example Artist/04. Example Track.flac"),
        service="EXAMPLE",
    )
    folder = Path("/dl/Example Artist")
    if mode == "season":
        context = season_context(base, folder)
    else:
        context = dict.fromkeys(base, "")
        context["folder"] = str(folder)

    assert context["filepath"] == ""
    assert context["sidecars"] == ""
    assert context["folder"] == str(folder)
    if mode == "run":
        assert context["title"] == ""
        assert set(context) == set(base)
    else:
        assert context["album"] == "Example Album, Live & Remastered"


def test_a_music_post_script_command_builds_the_argv_a_script_would_get(monkeypatch, tmp_path):
    """End to end through the real dispatcher: config entry in, argv out."""
    from unshackle.core.config import config

    marker = tmp_path / "argv.txt"
    script = tmp_path / "hook.py"
    script.write_text("import sys, pathlib\npathlib.Path(sys.argv[1]).write_text(chr(10).join(sys.argv[2:]))\n")

    import sys

    command = (
        f'"{sys.executable}" "{script}" "{marker}" '
        "--album={album} --artist={artist} --track={track_number} --file={filepath}"
    )
    monkeypatch.setattr(config, "post_scripts", [{"command": command, "mode": "file", "event": "success"}])

    context = build_context(
        song(),
        media_info=object(),
        filepath=Path("/dl/Example Artist/04. Example Track.flac"),
        service="EXAMPLE",
    )
    dispatch("success", "file", context)

    for _ in range(100):
        if marker.exists():
            break
        import time

        time.sleep(0.05)
    assert marker.exists(), "the post-script process never ran"
    argv = marker.read_text().splitlines()
    assert "--album=Example Album, Live & Remastered" in argv
    assert "--artist=Example Artist" in argv
    assert "--track=04" in argv
    assert f"--file={Path('/dl/Example Artist/04. Example Track.flac')}" in argv


def test_season_context_blanks_the_file_variables_and_sets_the_folder():
    """An album post-script describes the release folder, so no track's file may leak into it."""
    folder = Path("/dl/Example Artist - Example Album, Live & Remastered (1983)")
    context = build_context(
        song(),
        media_info=object(),
        filepath=folder / "04. Example Track.flac",
        sidecars=[folder / "04. Example Track.lrc"],
        service="EXAMPLE",
    )

    album = season_context(context, folder)

    for key in ("filepath", "filename", "ext", "sidecars", "episode", "episode_name"):
        assert album[key] == "", f"{key} still describes a file"
    assert album["folder"] == str(folder)
    assert album["service"] == "EXAMPLE"
    assert context["filepath"] != "", "the file context handed in must not be mutated"


def test_season_context_identifies_the_album_not_the_last_track():
    """A Song's ``{title}`` is the song, so blanking only the file fields is not enough.

    Guards a regression where an album post-script was handed whichever track landed last:
    ``{title}`` was a song name and ``{track_number}``/``{isrc}`` were that track's.
    """
    folder = Path("/dl/Example Artist - Example Album (1983)")
    context = build_context(
        song(track=11),
        media_info=object(),
        filepath=folder / "11. Example Track.flac",
        service="EXAMPLE",
    )
    context["isrc"] = "ZZABC2400001"
    context["disc"] = "02"

    album = season_context(context, folder)

    assert album["title"] == "Example Album, Live & Remastered"
    assert album["title_raw"] == "Example Album, Live & Remastered"
    assert album["track_number"] == ""
    assert album["disc"] == ""
    assert album["isrc"] == ""
    assert album["artist"] == "Example Artist"  # album-level, kept
    assert album["year"] == "1983"  # album-level, kept


def test_season_context_leaves_a_non_music_context_untouched():
    """The music branch is gated on ``{album}``; a season post-script must not lose its own keys."""
    folder = Path("/dl/Some Show/Season 1")
    context = build_context(
        episode(),
        media_info=object(),
        filepath=folder / "Some Show S01E05 Pilot.mkv",
        service="EXAMPLE",
    )
    context["disc"] = "sentinel"  # only the music branch clears this

    season = season_context(context, folder)

    assert season["title"] == "Some Show"
    assert season["title_raw"] == "Some Show"
    assert season["season"] == "1"
    assert season["disc"] == "sentinel"
    assert season["episode"] == ""
    assert season["episode_name"] == ""
    assert season["folder"] == str(folder)


def test_a_failed_track_gets_a_track_scoped_failure_context():
    """Failure is per-track, like every other title type, and does not describe the album.

    The real dispatch passes no MediaInfo and no filepath, so the context is whatever the
    Song itself carries, and it is never run through ``season_context``.
    """
    context = build_context(song(track=11), None, service="EXAMPLE", error="HTTPError: 403")

    assert context["title"] == "Example Track"
    assert context["title_raw"] == "Example Track"
    assert context["error"] == "HTTPError: 403"
    assert context["filepath"] == ""
    assert context["season"] == ""


def test_an_album_name_with_a_comma_stays_one_argument():
    """'Example Album, Live & Remastered' must not split, which is the whole tokenize-first point."""
    argv = substitute(tokenize("upload.py --album={album}"), {"album": "Example Album, Live & Remastered"})
    assert argv == ["upload.py", "--album=Example Album, Live & Remastered"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
