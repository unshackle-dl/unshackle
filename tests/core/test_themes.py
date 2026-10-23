"""Theme files: shipped themes, user files on top of them, and the errors that reject a file."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich import box
from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.progress import Progress
from rich.rule import Rule
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from rich.tree import Tree

from unshackle.commands.env import env
from unshackle.core.config import config
from unshackle.core.console import (
    ComfyConsole,
    Gradient,
    GradientPulseBarColumn,
    PinnedLive,
    SyncLive,
    gradient_fill,
    gradient_lut,
    slide_gradients,
    use_gradients,
)
from unshackle.core.themes import (
    ALIASES,
    DEFAULT_THEME,
    ROLES,
    apply_theme,
    build_console_theme,
    build_help_config,
    load_theme,
    read_theme_file,
    shipped_themes,
    user_themes,
)


@pytest.fixture
def themes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config.directories, "themes", tmp_path)
    return tmp_path


def write(folder: Path, name: str, text: str) -> None:
    (folder / f"{name}.yaml").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("name", sorted(shipped_themes()))
def test_shipped_theme_sets_every_role(name: str) -> None:
    assert set(read_theme_file(shipped_themes()[name])) >= set(ROLES)
    palette, styles, _ = load_theme(name, user_files=False)
    for set_terminal_bg in (False, True):
        build_console_theme(palette, styles, set_terminal_bg)
    build_help_config(palette)


def test_the_shipped_themes_ship() -> None:
    # the root .gitignore hides unshackle.yaml; the default theme needs its exception
    assert {DEFAULT_THEME, "nord", "rainbow", "dracula", "tokyo-night"} <= set(shipped_themes())


@pytest.mark.parametrize("alias", sorted(ALIASES))
def test_every_alias_names_a_shipped_theme(alias: str) -> None:
    assert load_theme(alias) == load_theme(ALIASES[alias], user_files=False)


def test_user_theme_extends_a_shipped_theme(themes_dir: Path) -> None:
    write(themes_dir, "My_Nord", 'extends: nord\naccent: "#88c0d0"\n')
    nord, nord_styles, _ = load_theme("nord")

    palette, styles, _ = load_theme("my-nord")

    assert palette == {**nord, "pink": "#88c0d0"}
    assert styles == nord_styles


def test_without_extends_a_new_theme_starts_from_the_default(themes_dir: Path) -> None:
    write(themes_dir, "mine", 'text: "#ffffff"\n')
    assert load_theme("mine")[0] == {**load_theme(DEFAULT_THEME)[0], "text": "#ffffff"}


def test_a_user_file_replaces_a_shipped_theme_that_it_extends(themes_dir: Path) -> None:
    write(themes_dir, "nord", 'extends: nord\ntext: "#ffffff"\n')
    assert load_theme("nord")[0]["text"] == "#ffffff"
    assert load_theme("nord", user_files=False)[0]["text"] != "#ffffff"


def test_alias_names_are_reserved(themes_dir: Path) -> None:
    write(themes_dir, "default", 'text: "#000000"\n')
    assert "default" not in user_themes()
    assert load_theme("default") == load_theme(DEFAULT_THEME, user_files=False)


def test_styles_resolve_role_names_to_the_theme_colours(themes_dir: Path) -> None:
    write(themes_dir, "mine", 'extends: nord\ncyan: "#00ffff"\nstyles:\n  bar.complete: bold accent on cyan\n')
    palette, styles, _ = load_theme("mine")
    assert styles["bar.complete"] == f"bold {palette['pink']} on #00ffff"


@pytest.mark.parametrize(
    "text, reason",
    [
        ("pink: #b48ead\n", "put hex values in quotes"),
        ('pink: "#fff"\n', "pink:"),
        ('pink: "#ff79c6"\naccent: "#ff79c6"\n', "pink or accent, not both"),
        ('pnik: "#ff79c6"\n', "unknown key pnik"),
        ('styles:\n  pink: "#ff79c6"\n', "set pink at the top level"),
        ("styles:\n  rule.line: shiny\n", "styles: rule.line"),
        ("extends: no-such-theme\n", "not a shipped theme"),
        ("extends: [nord]\n", "extends must be a quoted theme name"),
        ("- pink\n", "not a mapping"),
        ('red: "rgb(1,,2)"\n', "is not a colour"),
        ("red: off\n", "red must be a quoted colour"),  # YAML reads an unquoted off as False
        ("styles:\n  on: bold\n", "quote .* and its value"),
        ("red: " + "[" * 600 + "]" * 600 + "\n", "cannot read"),
        ("styles:\n  none: link https://example.com\n", "link and conceal not allowed"),
        ("styles:\n  logging.level.error: conceal\n", "link and conceal not allowed"),
        ("gradients: [red, blue]\n", "gradients must be a mapping"),
        ("gradients:\n  logo: [red]\n", "gradients: unknown key 'logo'"),
        ("gradients:\n  direction: sideways\n", "direction must be horizontal or vertical or diagonal"),
        ("gradients:\n  motion: true\n", "motion must be static or scroll"),
        ("gradients:\n  bar: []\n", "bar must be a list of quoted colours"),
        ("gradients:\n  bar: [[red]]\n", "bar must be a list of quoted colours"),
        ('gradients:\n  bar: [red, "#12"]\n', "gradients: bar: '#12' is not a colour"),
    ],
)
def test_a_bad_file_is_rejected_with_its_reason(themes_dir: Path, text: str, reason: str) -> None:
    write(themes_dir, "bad", text)
    with pytest.raises(ValueError, match=f"(?i)bad.yaml: .*{reason}"):
        load_theme("bad")


def test_a_yml_file_is_a_theme_too(themes_dir: Path) -> None:
    (themes_dir / "mine.yml").write_text('text: "#ffffff"\n', encoding="utf-8")
    assert load_theme("mine")[0]["text"] == "#ffffff"


def test_role_names_in_styles_ignore_case(themes_dir: Path) -> None:
    write(themes_dir, "mine", "extends: nord\nstyles:\n  bar.complete: Bold RED on Accent\n")
    palette, styles, _ = load_theme("mine")
    assert styles["bar.complete"] == f"Bold {palette['red']} on {palette['pink']}"


def test_rgb_with_spaces_is_stored_without_them(themes_dir: Path) -> None:
    write(themes_dir, "mine", 'cyan: "rgb(1, 2, 3)"\n')
    palette, styles, _ = load_theme("mine")
    assert palette["cyan"] == "rgb(1,2,3)"
    build_console_theme(palette, styles, set_terminal_bg=True)


def test_a_file_that_is_not_utf8_is_rejected(themes_dir: Path) -> None:
    (themes_dir / "bad.yaml").write_bytes(b"red: \xff\n")
    with pytest.raises(ValueError, match="bad.yaml: cannot read"):
        load_theme("bad")


def test_yaml_anchors_cannot_expand_a_role_value(themes_dir: Path) -> None:
    # an alias bomb: a thousand strings from a few hundred bytes
    write(
        themes_dir,
        "bad",
        "description: &a [" + ", ".join(['"lol"'] * 10) + "]\n"
        "author: &b [" + ", ".join(["*a"] * 10) + "]\n"
        "red: [" + ", ".join(["*b"] * 10) + "]\n",
    )

    start = time.monotonic()
    with pytest.raises(ValueError) as error:
        load_theme("bad")
    assert time.monotonic() - start < 1
    assert len(str(error.value)) < 200


def test_an_unknown_name_is_rejected(themes_dir: Path) -> None:
    with pytest.raises(ValueError, match="^unknown theme 'no-such-theme'$"):
        load_theme("no-such-theme")


def test_console_falls_back_to_the_shipped_default(themes_dir: Path) -> None:
    # console builds its theme at import, so this needs a fresh interpreter
    write(themes_dir, DEFAULT_THEME, "pink: #b48ead\n")
    script = (
        "import sys\n"
        "from unshackle.core.config import config\n"
        "config.theme = 'default'\n"
        "config.directories.themes = sys.argv[1]\n"
        "import unshackle.core.console as c\n"
        "print(c.primary_scheme['pink'])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(themes_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == load_theme(DEFAULT_THEME, user_files=False)[0]["pink"]
    assert "unshackle.yaml: pink is empty" in result.stderr


def test_env_theme_lists_themes_and_flags_a_broken_file(themes_dir: Path) -> None:
    write(themes_dir, "mine", "extends: nord\n")
    write(themes_dir, "bad", "pink: #b48ead\n")
    result = CliRunner().invoke(env, ["theme"])
    assert result.exit_code == 0, result.output
    assert "mine" in result.output and "nord" in result.output
    assert "not valid" in result.output


def test_env_theme_marks_the_fallback_as_active(themes_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import unshackle.core.console as console_module

    monkeypatch.setattr(config, "theme", "no-such-theme")
    monkeypatch.setattr(console_module, "active_theme", DEFAULT_THEME)
    result = CliRunner().invoke(env, ["theme"])
    assert result.exit_code == 0, result.output
    assert "(active, fallback)" in result.output


def test_env_theme_previews_one_theme(themes_dir: Path) -> None:
    result = CliRunner().invoke(env, ["theme", "nord"])
    assert result.exit_code == 0, result.output
    assert "Available Tracks" in result.output


def test_env_theme_names_the_error_for_a_bad_theme(themes_dir: Path) -> None:
    result = CliRunner().invoke(env, ["theme", "no-such-theme"])
    assert result.exit_code == 1
    assert "unknown theme 'no-such-theme'" in result.output


def test_progress_bar_accepts_a_named_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    import unshackle.core.console as console_module

    palette = {**console_module.primary_scheme, "pink": "magenta", "dark_gray": "default"}
    monkeypatch.setattr(console_module, "primary_scheme", palette)
    GradientPulseBarColumn()


def test_help_ignores_the_rich_click_theme_env_var() -> None:
    assert build_help_config(load_theme(DEFAULT_THEME, user_files=False)[0]).enable_theme_env_var is False


def test_gradients_resolve_role_names_and_take_one_colour(themes_dir: Path) -> None:
    write(
        themes_dir,
        "mine",
        'gradients:\n  direction: diagonal\n  motion: scroll\n  bar: [accent, "rgb(1, 2, 3)"]\n  rule: Blue\n',
    )
    palette, _, gradients = load_theme("mine")
    assert gradients == {
        "direction": "diagonal",
        "motion": "scroll",
        "bar": (palette["pink"], "rgb(1,2,3)"),
        "rule": (palette["blue"],),
    }


def test_gradients_merge_over_the_base_theme() -> None:
    palette = load_theme(DEFAULT_THEME, user_files=False)[0]
    _, _, gradients = apply_theme("x.yaml", {"gradients": {"rule": "#123456"}}, palette, {}, {"bar": ("#000000",)})
    assert gradients == {"bar": ("#000000",), "rule": ("#123456",)}


def test_gradient_lut_runs_from_the_first_stop_to_the_last() -> None:
    lut = gradient_lut(("#ff0000", "#00ff00", "#0000ff"))
    assert [hex_of(lut[0]), hex_of(lut[-1])] == ["#ff0000", "#0000ff"]
    cyclic = gradient_lut(("#ff0000", "#0000ff"), cyclic=True)
    assert cyclic[0] == cyclic[-1]
    assert len(gradient_lut(("red",))) == 1


def hex_of(style: Style) -> str:
    assert style.color is not None
    return style.color.get_truecolor().hex


def render(renderable: RenderableType) -> list[Segment]:
    return list(Console(file=io.StringIO(), color_system="truecolor", width=40).render(renderable))


def colours_of(segments: list[Segment], characters: str) -> list[str]:
    """The colour of each segment whose text is one of *characters*, or is *characters*."""
    return [
        hex_of(s.style)
        for s in segments
        if (s.text in characters or s.text == characters) and s.text.strip() and s.style and s.style.color
    ]


def test_panel_gradient_paints_the_frame_and_not_the_content() -> None:
    tree = Tree("root", guide_style="#888888")
    tree.add("leaf")
    panel = Panel(tree, title="Title", border_style="#888888", box=box.SQUARE, expand=False)
    with use_gradients({"panel": ("#ff0000", "#0000ff")}):
        segments = render(panel)
    assert colours_of(segments, "┌") == ["#ff0000"]
    assert colours_of(segments, "┘") == ["#0000ff"]
    assert "#888888" in colours_of(segments, "└── ")  # the tree guide keeps its colour
    assert any("Title" in s.text for s in segments)


def test_rule_gradient_paints_the_line_and_not_the_title() -> None:
    with use_gradients({"rule": ("#ff0000", "#0000ff"), "direction": "vertical"}):
        segments = render(Rule("[#00ff00]Title", style="#888888"))
    line = colours_of(segments, "─")
    assert line[0] == "#ff0000" and line[-1] == "#0000ff"  # always left to right, whatever the direction
    assert colours_of(segments, "Title") == ["#00ff00"]


def test_without_gradients_a_panel_renders_as_rich_draws_it() -> None:
    with use_gradients({}):
        assert set(colours_of(render(Panel("x", border_style="#888888")), "─│╭╮╰╯")) == {"#888888"}


def test_gradient_fill_static_and_scrolling() -> None:
    bar = gradient_fill(gradient_lut(("#ff0000", "#0000ff")), 0.5, 10, None, "#333333")
    assert bar.plain == "━━━━━╺━━━━"
    assert hex_of(bar.get_style_at_offset(Console(), 0)) == "#ff0000"
    scrolled = gradient_fill(gradient_lut(("#ff0000", "#0000ff"), cyclic=True), 0.5, 10, 3.0, "#333333")
    assert hex_of(scrolled.get_style_at_offset(Console(), 3)) == "#ff0000"


def test_bar_column_uses_the_gradient_until_the_task_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    import unshackle.core.console as console_module

    monkeypatch.setattr(console_module, "theme_gradients", {"bar": ("#ff0000", "#0000ff")})
    column = GradientPulseBarColumn()
    progress = Progress(column, console=Console(file=io.StringIO()))
    progress.add_task("x", total=10, completed=5)
    progress.add_task("y", total=10, completed=10)
    running, done = progress.tasks
    assert type(column.render(running)).__name__ == "_GradientFill"
    assert type(column.render(done)).__name__ == "ProgressBar"


def slid(renderable: RenderableType, offset: float) -> list[Segment]:
    lines = Console(file=io.StringIO(), color_system="truecolor", width=40).render_lines(renderable, pad=False)
    return [segment for line in slide_gradients(lines, offset) for segment in line]


def test_sliding_a_gradient_moves_the_colours_and_loops() -> None:
    text = Text("█" * 20, style="#888888")
    with use_gradients({"banner": ("#ff0000", "#0000ff")}):
        frames = [colours_of(slid(Gradient(text, "banner", "#888888"), o), "█") for o in (0.0, 0.5, 1.0)]
    assert frames[0] != frames[1]
    assert frames[0] == frames[2]


def test_sliding_moves_a_nested_panel_border_and_nothing_else() -> None:
    from rich.padding import Padding

    panel = Padding(Panel("x", border_style="#888888", box=box.SQUARE), (0, 3))
    with use_gradients({"panel": ("#ff0000", "#0000ff")}):
        still = colours_of(render(panel), "┌")
        moved = colours_of(slid(panel, 0.5), "┌")
        text = colours_of(slid(Text("x", style="#888888"), 0.5), "x")
    assert still == ["#ff0000"] and moved != still
    assert text == ["#888888"]


def test_an_unreadable_themes_folder_is_skipped_with_a_warning(
    themes_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(self: Path) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "iterdir", refuse)
    with caplog.at_level(logging.WARNING, logger="console"):
        assert user_themes() == {}
    assert "cannot read the themes folder" in caplog.text


def test_a_file_name_with_a_control_character_is_skipped(themes_dir: Path) -> None:
    write(themes_dir, "\u202emine", 'text: "#ffffff"\n')
    write(themes_dir, "mine", 'text: "#ffffff"\n')
    assert list(user_themes()) == ["mine"]


def pinned_console(height: int = 12) -> tuple[io.StringIO, Console]:
    out = io.StringIO()
    return out, Console(
        file=out, force_terminal=True, force_interactive=True, width=60, height=height, color_system="truecolor"
    )


def test_pinned_live_sends_old_lines_to_the_scrollback_once_the_screen_is_full() -> None:
    out, con = pinned_console()
    pinned = PinnedLive(Text("HEADER"), con)
    pinned.start(refresh=True)
    try:
        panel = Panel("first state")
        con.print(panel)
        panel.renderable = "CHANGED"  # a kept print shows the state it had when it was printed
        con.print("narrow", width=4)
        assert ["".join(segment.text for segment in line) for line in pinned.kept[-2:]] == ["narr", "ow"]
        for i in range(30):
            con.print(f"line {i:02d}")
        assert pinned.header_sent
        assert len(pinned.kept) == pinned.room  # exactly the lines that fit; the rest went to the scrollback
    finally:
        pinned.close()
    assert pinned.kept == []
    plain = Text.from_ansi(out.getvalue()).plain
    assert "first state" in plain and "CHANGED" not in plain
    assert "line 29" in plain


def test_pinned_live_prints_the_header_at_close_when_nothing_scrolled() -> None:
    out, con = pinned_console(height=40)
    pinned = PinnedLive(Text("HEADER"), con)
    pinned.start(refresh=True)
    con.print("one line")
    pinned.close()
    assert Text.from_ansi(out.getvalue()).plain.rstrip().endswith("HEADER\none line")


def test_a_prompt_inside_a_spinner_keeps_the_live_areas_in_order() -> None:
    con = ComfyConsole(file=io.StringIO(), force_terminal=True, force_interactive=True, width=60, height=20)
    pinned = PinnedLive(Text("HEADER"), con)
    pinned.start()
    try:
        with con.status("working"):
            assert con.input("code: ", stream=io.StringIO("123\n")).strip() == "123"
            assert con._live_stack[0] is pinned and not pinned._nested
            assert con._live_stack[1]._nested
    finally:
        pinned.close()
    assert con._live_stack == []


def test_sync_codes_wrap_each_print_but_not_on_the_old_windows_console() -> None:
    out, con = pinned_console()
    with SyncLive(Text("live"), console=con, auto_refresh=False) as live:
        con.print("hello")
        value = out.getvalue()
        assert value.index("\x1b[?2026h") < value.index("hello") < value.index("\x1b[?2026l", value.index("hello"))
        con.legacy_windows = True
        printed = Text("x")
        assert live.synced([printed]) == [printed]


@pytest.mark.parametrize(
    "subcommand, args, pinned",
    [
        ("dl", ["dl", "EXAMPLE", "title"], True),
        ("dl", ["--debug", "dl", "-q", "1080", "EXAMPLE", "title"], True),
        ("dl", ["dl"], True),  # Click prints the usage text after the pinned area closes
        ("dl", ["dl", "EXAMPLE", "--help"], False),
        ("dl", ["dl", "-?"], False),
        ("env", ["env", "info"], False),
    ],
)
def test_only_a_dl_run_pins_the_banner(
    monkeypatch: pytest.MonkeyPatch, subcommand: str, args: list[str], pinned: bool
) -> None:
    import unshackle.core.console as console_module
    from unshackle.core.__main__ import pins_banner

    monkeypatch.setattr(console_module, "theme_gradients", {"banner": ("#ff0000",)})
    monkeypatch.setattr(PinnedLive, "wanted", staticmethod(lambda live_console: True))
    assert pins_banner(subcommand, args) is pinned
    monkeypatch.setattr(console_module, "theme_gradients", {})
    assert pins_banner(subcommand, args) is False


def test_the_banner_pins_only_on_an_interactive_colour_terminal() -> None:
    _, con = pinned_console()
    assert PinnedLive.wanted(con)
    assert not PinnedLive.wanted(
        Console(file=io.StringIO(), force_terminal=True, force_interactive=True, no_color=True)
    )
    assert not PinnedLive.wanted(Console(file=io.StringIO(), force_terminal=True, force_interactive=False))


@pytest.mark.parametrize("height", [5, 500])
def test_env_theme_previews_in_a_short_and_a_tall_terminal(
    themes_dir: Path, monkeypatch: pytest.MonkeyPatch, height: int
) -> None:
    import unshackle.core.console as console_module

    monkeypatch.setattr(console_module.console, "_height", height)
    result = CliRunner().invoke(env, ["theme", "rainbow"])
    assert result.exit_code == 0, result.output
    assert result.output.count("github.com/unshackle") == 1
    assert "Available Tracks" in result.output


def test_a_narrow_terminal_gets_no_live_animation(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.console import fits_terminal

    _, con = pinned_console()
    monkeypatch.setenv("COLUMNS", str(con.width - 1))
    assert not fits_terminal(con) and not PinnedLive.wanted(con)
    monkeypatch.setenv("COLUMNS", str(con.width))
    assert fits_terminal(con) and PinnedLive.wanted(con)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_during_a_pinned_run_still_prints_the_kept_lines(tmp_path: Path) -> None:
    import os
    import pty
    import signal

    script = tmp_path / "pinned.py"
    script.write_text(
        "import signal, sys, time\n"
        "from rich.text import Text\n"
        "from unshackle.core.console import PinnedLive, console\n"
        "pinned = PinnedLive(Text('HEADER'), console)\n"
        "pinned.start(refresh=True)\n"
        "signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))\n"
        "try:\n"
        "    console.print('kept line')\n"
        "    print('READY', file=sys.__stderr__, flush=True)\n"
        "    time.sleep(30)\n"
        "finally:\n"
        "    pinned.close()\n",
        encoding="utf-8",
    )
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, str(script)], stdin=slave, stdout=slave, stderr=subprocess.PIPE)
    os.close(slave)
    assert proc.stderr is not None and b"READY" in proc.stderr.readline()
    proc.send_signal(signal.SIGTERM)
    out = b""
    while True:
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    assert proc.wait(timeout=30) == 128 + signal.SIGTERM
    tail = out[out.rfind(b"\x1b[?25h") :]
    assert b"HEADER" in tail and b"kept line" in tail
