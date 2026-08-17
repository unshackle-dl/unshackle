"""Music post-script hooks, which no live service was available to test against.

Music returns from ``dl.result`` before the title loop, so its hooks are dispatched inside
the music pipeline. These check the pieces that pipeline feeds them: that a Song builds a
usable context, that an Album groups the way a season does, and that the album and run
contexts blank the file-level variables.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from unshackle.core.utils.post_scripts import build_context, dispatch, substitute, tokenize


class FakeSong(SimpleNamespace):
    """A Song as the hook sees it: name/artist/album, no season or episode."""

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
        name="Blue Monday", artist="New Order", album="Power, Corruption & Lies", track=4, year=1983, id="s1"
    )
    return FakeSong(**{**defaults, "id": "s1", **kwargs})


def test_song_context_has_music_fields_and_blank_episode_fields():
    context = build_context(
        song(),
        media_info=object(),
        filepath=Path("/dl/New Order/04. Blue Monday.flac"),
        service="EXAMPLE",
    )
    assert context["title"] == "Blue Monday"
    assert context["artist"] == "New Order"
    assert context["album"] == "Power, Corruption & Lies"
    assert context["track_number"] == "04"
    assert context["service"] == "EXAMPLE"
    assert context["filename"] == "04. Blue Monday.flac"
    assert context["ext"] == ".flac"
    assert context["folder"] == "/dl/New Order"
    assert context["season"] == ""
    assert context["episode"] == ""


def test_song_context_survives_without_media_info():
    """The failure path and any pre-mux dispatch pass no MediaInfo."""
    context = build_context(song(), None, service="EXAMPLE", error="HTTPError: 403")
    assert context["title"] == "Blue Monday"
    assert context["error"] == "HTTPError: 403"
    assert context["filepath"] == ""


def test_album_grouping_matches_the_season_grouping_rule():
    """post_script_group's music branch: albums group by (album artist, album)."""
    from unshackle.commands.dl import dl  # noqa: F401 - import guard, the grouping is inlined in result()

    def group(candidate):
        artist = getattr(candidate, "album_artist", None) or getattr(candidate, "artist", "")
        return ("album", artist, getattr(candidate, "album", ""))

    a = song(track=1)
    b = song(track=2)
    c = song(album="Technique")
    assert group(a) == group(b)
    assert group(a) != group(c)


@pytest.mark.parametrize("mode", ["season", "run"])
def test_album_and_run_contexts_blank_the_file_variables(mode):
    """Mirrors the blanking dl.py applies before dispatching an album or run hook."""
    base = build_context(
        song(),
        media_info=object(),
        filepath=Path("/dl/New Order/04. Blue Monday.flac"),
        service="EXAMPLE",
    )
    context = dict(base)
    if mode == "season":
        for key in ("filepath", "filename", "ext", "sidecars", "episode", "episode_name"):
            context[key] = ""
    else:
        context = dict.fromkeys(base, "")
    context["folder"] = "/dl/New Order"

    assert context["filepath"] == ""
    assert context["sidecars"] == ""
    assert context["folder"] == "/dl/New Order"
    if mode == "run":
        assert context["title"] == ""
        assert set(context) == set(base)
    else:
        assert context["album"] == "Power, Corruption & Lies"


def test_a_music_hook_command_builds_the_argv_a_script_would_get(monkeypatch, tmp_path):
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
        filepath=Path("/dl/New Order/04. Blue Monday.flac"),
        service="EXAMPLE",
    )
    dispatch("success", "file", context)

    for _ in range(100):
        if marker.exists():
            break
        import time

        time.sleep(0.05)
    assert marker.exists(), "the hook process never ran"
    argv = marker.read_text().splitlines()
    assert "--album=Power, Corruption & Lies" in argv
    assert "--artist=New Order" in argv
    assert "--track=04" in argv
    assert "--file=/dl/New Order/04. Blue Monday.flac" in argv


def test_album_hook_mapping_is_bound_before_any_early_branch():
    """--skip-dl skips the final-move block; the album hook still runs after it.

    Guards a NameError: the mapping used to be created inside that block, so a music
    download with --skip-dl reached the album hook with an unbound name.
    """
    import inspect

    from unshackle.commands.dl import dl

    source = inspect.getsource(dl.result)
    body = source[source.index("def download_music_title") :]
    bind = body.index("album_post_script: dict")
    for use in ("album_post_script[final_path.parent]", "album_post_script.items()"):
        assert bind < body.index(use), f"{use} can run before the mapping is bound"
    line = body[:bind].rsplit("\n", 1)[-1]
    assert len(line) == 16, f"bound at indent {len(line)}, expected the function body level"


def test_album_hook_identifies_the_album_not_the_last_track():
    """A Song's ``{title}`` is the song, so blanking only the video fields is not enough.

    Guards a regression where an album hook was handed whichever track landed last:
    ``{title}`` was a song name and ``{track_number}``/``{isrc}`` were that track's.
    """
    from unshackle.commands.dl import dl

    context = build_context(
        song(track=11, isrc="GBAAA8300001"),
        media_info=object(),
        filepath=Path("/dl/New Order - PCL (1983)/11. Blue Monday.flac"),
        service="EXAMPLE",
    )
    context.setdefault("isrc", "GBAAA8300001")

    album = dict(context)
    for key in ("filepath", "filename", "ext", "sidecars", "episode", "episode_name"):
        album[key] = ""
    for key in ("track_number", "disc", "isrc"):
        if key in album:
            album[key] = ""
    album["title"] = album["title_raw"] = album.get("album", "")

    assert album["title"] == "Power, Corruption & Lies"
    assert album["title_raw"] == "Power, Corruption & Lies"
    assert album["track_number"] == ""
    assert album["isrc"] == ""
    assert album["artist"] == "New Order"  # album-level, kept

    import inspect

    body = inspect.getsource(dl.result)
    body = body[body.index("for folder, context in album_post_script.items()") :][:900]
    for key in ("track_number", "disc", "isrc"):
        assert f'"{key}"' in body, f"the album hook no longer blanks {key}"
    assert 'album_context["title"]' in body


def test_a_music_download_failure_dispatches_a_failure_hook():
    """Parity with the video worker, which dispatches failure/file when it raises."""
    import inspect

    from unshackle.commands.dl import dl

    body = inspect.getsource(dl.result)
    body = body[body.index("def download_music_title") :]
    assert body.count("music_failure(") == 3, "expected one definition and both failure exits"
    assert body.index("def music_failure") < body.index("music_failure(e)")
    assert "music_failure(error)" in body

    release = SimpleNamespace(title="Power, Corruption & Lies", artist="New Order", year=1983)
    context = build_context(release, service="EXAMPLE", error="HTTPError: 403")
    assert context["title"] == "Power, Corruption & Lies"
    assert context["error"] == "HTTPError: 403"
    assert context["filepath"] == ""


def test_an_album_name_with_a_comma_stays_one_argument():
    """'Power, Corruption & Lies' must not split, which is the whole tokenize-first point."""
    argv = substitute(tokenize("upload.py --album={album}"), {"album": "Power, Corruption & Lies"})
    assert argv == ["upload.py", "--album=Power, Corruption & Lies"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
