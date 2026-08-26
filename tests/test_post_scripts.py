"""Post-script command building: the injection guard, and how variables become argv."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from unshackle.core.config import config
from unshackle.core.utils.post_scripts import (
    SIDECAR_SEPARATOR,
    _entries,
    build_context,
    dispatch,
    substitute,
    tokenize,
)


def build(command: str, context: dict[str, str]) -> list[str]:
    return substitute(tokenize(command), context)


def test_metacharacters_in_a_value_stay_one_argument():
    """The whole point of tokenizing before substituting: a title can't become a command."""
    argv = build(
        "upload.py {filepath} --title={title}",
        {
            "filepath": "/downloads/Bob's Burgers S01E01.mkv",
            "title": 'Bob"; rm -rf ~',
        },
    )
    assert argv == ["upload.py", "/downloads/Bob's Burgers S01E01.mkv", '--title=Bob"; rm -rf ~']


def test_empty_variable_substitutes_empty():
    argv = build("upload.py {filepath} --tmdb={tmdb}", {"filepath": "/x.mkv", "tmdb": ""})
    assert argv == ["upload.py", "/x.mkv", "--tmdb="]


def test_sidecars_join_as_a_single_token():
    subs = [Path("/dl/Show S01E01.en.srt"), Path("/dl/Show S01E01.fr.srt")]
    argv = build("upload.py --subs={sidecars}", {"sidecars": SIDECAR_SEPARATOR.join(map(str, subs))})
    assert len(argv) == 2
    assert argv[1].removeprefix("--subs=").split(SIDECAR_SEPARATOR) == [str(p) for p in subs]


@pytest.mark.parametrize(
    "hostile_value",
    ["--upload-file=/etc/passwd", "-rf", "--output=/etc/cron.d/x"],
)
def test_bare_variable_expanding_to_a_flag_is_refused(caplog, hostile_value):
    """A service-controlled value that becomes a whole -flag must not run the command."""
    dispatch("success", "file", {"title_raw": hostile_value}, ["curl example.com {title_raw}"])
    assert "option-like token" in caplog.text


def test_flag_prefixed_variable_is_not_refused(caplog):
    """--opt={var} is the user's own flag; the value in the middle can never forge one."""
    calls = []
    import unshackle.core.utils.post_scripts as ps

    original = ps.subprocess.Popen
    ps.subprocess.Popen = lambda argv, **kw: calls.append(argv) or original(["true"])
    try:
        dispatch("success", "file", {"title": "--evil=1"}, ["echo --title={title}"])
    finally:
        ps.subprocess.Popen = original
    assert calls and calls[0] == ["echo", "--title=--evil=1"]
    assert "option-like token" not in caplog.text


def test_unknown_variable_becomes_empty(caplog):
    argv = build("upload.py --x={nope}", {})
    assert argv == ["upload.py", "--x="]
    assert "nope" in caplog.text


@pytest.mark.parametrize(
    "command,expected",
    [
        ('python "/opt/my scripts/upload.py" {filepath}', ["python", "/opt/my scripts/upload.py", "/x.mkv"]),
        ("python upload.py {filepath}", ["python", "upload.py", "/x.mkv"]),
    ],
)
def test_quoted_paths_in_the_template_survive_tokenizing(command, expected):
    assert build(command, {"filepath": "/x.mkv"}) == expected


@pytest.mark.parametrize(
    "command,expected",
    [
        (r"python C:\Scripts\up.py {filepath}", ["python", "C:\\Scripts\\up.py", "/x.mkv"]),
        (r'python "C:\My Scripts\up.py" {filepath}', ["python", "C:\\My Scripts\\up.py", "/x.mkv"]),
        ('python up.py --title="{title}"', ["python", "up.py", "--title=T"]),
    ],
)
def test_windows_tokenizing_matches_posix_semantics(monkeypatch, command, expected):
    import os

    monkeypatch.setattr(os, "name", "nt")
    assert build(command, {"filepath": "/x.mkv", "title": "T"}) == expected


def test_control_chars_stripped_so_sidecar_newline_separator_holds():
    """A service title with a newline must not survive into a filename and forge a {sidecars} entry."""
    from unshackle.core.utilities import sanitize_filename

    assert "\n" not in sanitize_filename("Ep\n/etc/cron.d/x", " ")
    assert "\t" not in sanitize_filename("Ep\tfoo", " ")


def test_failure_context_still_has_title_and_year():
    """A failure dispatch has no MediaInfo, but a notifier's --title={title} must not go empty."""
    title = SimpleNamespace(title="Bob's Burgers", name="Ep", year=2011, season=1, number=2, id="x")
    context = build_context(title, None, service="SVC", error="Boom")
    assert context["title"] == "Bob's Burgers"
    assert context["year"] == "2011"
    assert context["error"] == "Boom"
    assert context["filepath"] == ""


def test_success_context_prefers_the_template_title():
    title = SimpleNamespace(
        title="Raw $how",
        name="Ep",
        year=2011,
        season=1,
        number=2,
        id="x",
        build_template_context=lambda media_info, show_service: {"title": "From Template", "year": 2020},
    )
    context = build_context(title, media_info=object(), service="SVC")
    assert context["title"] == "From Template"
    assert context["year"] == "2020"
    assert context["title_raw"] == "Raw $how"


def test_dispatch_survives_an_unbalanced_quote(caplog):
    """dispatch never raises: a malformed template is skipped with a warning, not a crash."""
    dispatch("success", "file", {}, ['python "unclosed'])
    assert "malformed" in caplog.text


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("python x.py {filepath}", ["python x.py {filepath}"]),  # lone string, not a list
        ({"command": "python x.py"}, ["python x.py"]),  # lone mapping, not a list
        (42, []),  # nonsense is ignored, never iterated
        ([42, None, "ok.py"], ["ok.py"]),  # junk entries are dropped, valid ones survive
    ],
)
def test_malformed_post_scripts_config_shapes(monkeypatch, raw, expected):
    monkeypatch.setattr(config, "post_scripts", raw)
    assert [c for c, _ in _entries("success", "file")] == expected


def test_typoed_event_or_mode_warns_instead_of_silently_dropping(monkeypatch, caplog):
    """Fire-and-forget gives no other feedback, so a dead entry must at least warn."""
    monkeypatch.setattr(
        config,
        "post_scripts",
        [{"command": "typo.py", "mode": "seson"}, {"command": "ok.py", "mode": "season"}],
    )
    for dispatch_mode in ("file", "season", "run"):
        assert [c for c, _ in _entries("success", dispatch_mode)] == (["ok.py"] if dispatch_mode == "season" else [])
    assert caplog.text.count("unknown event/mode") == 1  # warned once, not per dispatch


def test_tagging_ids_are_read_at_dispatch_not_snapshotted():
    """A series' TMDB ID is resolved inside the title loop, after any pre-loop snapshot.

    Guards a regression where ``post_script_ids`` was a dict built before the loop, so
    ``{tmdb}`` was empty for every download that did not pass ``--tmdb`` by hand.
    """
    import ast
    import inspect
    import textwrap

    from unshackle.commands.dl import dl

    holder = SimpleNamespace(tmdb_id=None, imdb_id="tt0000001", tvdb_id=None)
    assert build_context(SimpleNamespace(), ids=dl.post_script_ids(holder))["tmdb"] == ""
    holder.tmdb_id = 1396
    assert build_context(SimpleNamespace(), ids=dl.post_script_ids(holder))["tmdb"] == "1396"

    body = ast.parse(textwrap.dedent(inspect.getsource(dl.result)))
    for node in ast.walk(body):
        targets = getattr(node, "targets", [])
        assert not any(isinstance(t, ast.Name) and t.id == "post_script_ids" for t in targets), (
            "post_script_ids is snapshotted again; it must be read per dispatch"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def _dispatch_seconds(monkeypatch, entry: dict) -> float:
    import time

    monkeypatch.setattr(config, "post_scripts", [entry])
    start = time.monotonic()
    dispatch("success", "file", {})
    return time.monotonic() - start


def test_wait_true_waits_for_the_script_and_logs_the_exit_code(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="post-script")
    slow = 'python -c "import time,sys; time.sleep(0.3); sys.exit(3)"'
    assert _dispatch_seconds(monkeypatch, {"command": slow, "wait": True}) >= 0.3
    assert "Post-script exited 3" in caplog.text


def test_wait_defaults_to_fire_and_forget(monkeypatch):
    slow = 'python -c "import time; time.sleep(0.3)"'
    assert _dispatch_seconds(monkeypatch, {"command": slow}) < 0.3
