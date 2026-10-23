import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import click
from rich.console import RenderableType
from rich.padding import Padding
from rich.text import Text
from rich.tree import Tree

from unshackle.core import binaries
from unshackle.core.config import POSSIBLE_CONFIG_PATHS, config, config_path
from unshackle.core.console import console, listing_table, print_wide
from unshackle.core.constants import context_settings
from unshackle.core.services import Services
from unshackle.core.temp import TASK_PREFIX, is_stale


def get_dependencies() -> list[dict]:
    """Binary dependency inventory shared by `env check` and the API env check."""
    deps = [
        {
            "name": "FFmpeg",
            "binary": binaries.FFMPEG,
            "required": True,
            "desc": "Media processing",
            "cat": "Core",
        },
        {
            "name": "FFprobe",
            "binary": binaries.FFProbe,
            "required": True,
            "desc": "Media analysis",
            "cat": "Core",
        },
        {
            "name": "MKVToolNix",
            "binary": binaries.MKVToolNix,
            "required": True,
            "desc": "MKV muxing",
            "cat": "Core",
        },
        {
            "name": "mkvpropedit",
            "binary": binaries.Mkvpropedit,
            "required": True,
            "desc": "MKV metadata",
            "cat": "Core",
        },
        {
            "name": "shaka-packager",
            "binary": binaries.ShakaPackager,
            "required": True,
            "desc": "DRM decryption",
            "cat": "DRM",
        },
        {
            "name": "mp4decrypt",
            "binary": binaries.Mp4decrypt,
            "required": False,
            "desc": "DRM decryption",
            "cat": "DRM",
        },
        {
            "name": "dovi_tool",
            "binary": binaries.DoviTool,
            "required": False,
            "desc": "Dolby Vision",
            "cat": "HDR",
        },
        {
            "name": "HDR10Plus_tool",
            "binary": binaries.HDR10PlusTool,
            "required": False,
            "desc": "HDR10+ metadata",
            "cat": "HDR",
        },
        {
            "name": "SubtitleEdit",
            "binary": binaries.SubtitleEdit,
            "required": False,
            "desc": "Sub conversion",
            "cat": "Subtitle",
        },
        {
            "name": "CCExtractor",
            "binary": binaries.CCExtractor,
            "required": False,
            "desc": "CC extraction",
            "cat": "Subtitle",
        },
        {
            "name": "FFplay",
            "binary": binaries.FFPlay,
            "required": False,
            "desc": "Simple player",
            "cat": "Player",
        },
        {
            "name": "MPV",
            "binary": binaries.MPV,
            "required": False,
            "desc": "Advanced player",
            "cat": "Player",
        },
        {
            "name": "HolaProxy",
            "binary": binaries.HolaProxy,
            "required": False,
            "desc": "Proxy service",
            "cat": "Network",
        },
        {
            "name": "Caddy",
            "binary": binaries.Caddy,
            "required": False,
            "desc": "Web server",
            "cat": "Network",
        },
        {
            "name": "Docker",
            "binary": binaries.Docker,
            "required": False,
            "desc": "Gluetun VPN",
            "cat": "Network",
        },
        {
            "name": "git",
            "binary": binaries.Git,
            "required": False,
            "desc": "Service repos",
            "cat": "Network",
        },
    ]

    for reg in binaries.get_registered_dependencies():
        deps.append(
            {
                "name": reg["name"],
                "binary": getattr(binaries, reg["attr"], None),
                "required": False,
                "desc": reg["desc"],
                "cat": "Service",
            }
        )

    return deps


def clear_directory(path: Path) -> tuple[int, int]:
    """Delete a directory's contents, returning (files_removed, freed_bytes). Recreates the dir.

    Skips task directories that belong to a running download.
    """
    files_count = 0
    freed_bytes = 0
    if path.exists():
        for entry in path.iterdir():
            is_real_dir = entry.is_dir() and not entry.is_symlink()
            if is_real_dir and entry.name.startswith(TASK_PREFIX) and not is_stale(entry):
                continue
            if is_real_dir:
                for child in entry.glob("**/*"):
                    if child.is_file():
                        files_count += 1
                        try:
                            freed_bytes += child.stat().st_size
                        except OSError:
                            pass
                shutil.rmtree(entry, ignore_errors=True)
            else:
                files_count += 1
                try:
                    freed_bytes += entry.lstat().st_size
                except OSError:
                    pass
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    pass
    path.mkdir(parents=True, exist_ok=True)
    return files_count, freed_bytes


@click.group(short_help="Manage and configure the project environment.", context_settings=context_settings)
def env() -> None:
    """Manage and configure the project environment."""


@env.command()
def check() -> None:
    """Checks environment for the required dependencies."""
    all_deps = get_dependencies()

    all_required_installed = True
    total_installed = 0
    total_required = 0
    missing_required = []

    table = listing_table("Environment Dependencies", expand=True, show_lines=True)
    table.add_column("Category", style="cyan", width=10)
    table.add_column("Tool", style="text", width=16)
    table.add_column("Status", justify="center", width=10)
    table.add_column("Req", justify="center", width=4)
    table.add_column("Purpose", style="text2", overflow="fold")

    last_cat = None
    for dep in all_deps:
        path = dep["binary"]

        category = dep["cat"] if dep["cat"] != last_cat else ""
        last_cat = dep["cat"]

        if path:
            status = "[green]✓[/green]"
            total_installed += 1
        else:
            status = "[red]✗[/red]"
            if dep["required"]:
                all_required_installed = False
                missing_required.append(dep["name"])

        if dep["required"]:
            total_required += 1

        req = "[red]Y[/red]" if dep["required"] else "[bright_black]-[/bright_black]"

        table.add_row(category, dep["name"], status, req, dep["desc"])

    print_wide(table)

    summary_parts = [f"[text]Total:[/text] [repr.number]{total_installed}[/]/[repr.number]{len(all_deps)}[/]"]

    if all_required_installed:
        summary_parts.append("[green]All required tools installed ✓[/green]")
    else:
        summary_parts.append(f"[red]Missing required: {', '.join(missing_required)}[/red]")

    console.print(Padding("  ".join(summary_parts), (0, 3, 1, 3)))
    if not all_required_installed:
        console.print(Padding("[text]Run [cyan]unshackle setup[/cyan] to install them.[/text]", (0, 3, 1, 3)))


@env.command()
@click.argument("name", required=False)
def theme(name: Optional[str]) -> None:
    """List the CLI themes, or preview the theme NAME in full."""

    from unshackle.core.console import active_theme
    from unshackle.core.themes import canonical_name, load_theme, shipped_themes, user_themes

    if name:
        try:
            palette, styles, gradients = load_theme(name)
        except ValueError as e:
            raise click.ClickException(str(e)) from e
        preview_theme(canonical_name(name), palette, styles, gradients)
        return

    shipped, user = shipped_themes(), user_themes()
    mark = "  (active)" if active_theme == canonical_name(config.theme) else "  (active, fallback)"
    table = listing_table("Themes", show_lines=True)
    table.add_column("Theme", style="cyan", no_wrap=True)
    table.add_column("Source", style="text2", no_wrap=True)
    table.add_column("Colours", overflow="fold")
    for theme_name in sorted({*shipped, *user}):
        source = "shipped" if theme_name not in user else "user, replaces shipped" if theme_name in shipped else "user"
        label = Text(theme_name)
        try:
            palette, *_ = load_theme(theme_name)
        except ValueError as e:
            table.add_row(label, source, Text(f"not valid: {e}", style="red"))
            continue
        if theme_name == active_theme:
            label.append(mark, style="green")
        table.add_row(label, source, swatch(palette))
    print_wide(table)
    hint = Text.assemble(
        ("Theme files folder: ", "text"),
        (str(config.directories.themes), "text2"),
        ("\nPreview one theme with ", "text"),
        ("unshackle env theme NAME", "cyan"),
    )
    print_wide(hint, (0, 3, 1, 3))


def swatch(palette: dict[str, str]) -> Text:
    """A row of colour blocks, one for each main role of a theme."""

    blocks = Text()
    for role in ("red", "green", "yellow", "blue", "pink", "cyan", "text", "gray"):
        blocks.append("██ ", style=palette[role])
    return blocks


def play_preview(frame: Callable[[Optional[float]], RenderableType], seconds: float) -> None:
    """Play *frame* on a terminal for *seconds*, then leave its still frame. *frame* takes the time in seconds, or None."""
    import time

    from unshackle.core.console import SyncLive, fits_terminal

    if not console.is_interactive or not fits_terminal(console):
        console.print(frame(None))
        return
    with SyncLive(frame(0.0), console=console, auto_refresh=False) as live:
        start = time.monotonic()
        while (elapsed := time.monotonic() - start) < seconds:
            live.update(frame(elapsed), refresh=True)
            time.sleep(1 / 30)
        live.update(frame(None), refresh=True)


def preview_theme(name: str, palette: dict[str, str], styles: dict[str, str], gradients: dict[str, Any]) -> None:
    """Print a sample of each kind of CLI output in one theme."""
    import math

    from rich.console import Group
    from rich.progress_bar import ProgressBar
    from rich.rule import Rule
    from rich.segment import SegmentLines
    from rich.table import Table

    from unshackle.core.__main__ import banner
    from unshackle.core.console import (
        PULSE_PERIOD,
        PULSE_SPEED,
        _GradientFill,
        _GradientPulse,
        bar_gradients,
        listing_panel,
        slide_gradients,
        use_gradients,
    )
    from unshackle.core.themes import build_console_theme, build_help_config

    text, text2, gray, guide = palette["text"], palette["text2"], palette["gray"], palette["bright_black"]
    help_config = build_help_config(palette)

    docstring = Text()
    docstring.append("Service docstrings render like this, ", style=text)
    docstring.append("with secondary detail lines like this.", style=text2)

    # the help screen's own styles
    separator = Text("─" * 60, style=help_config.style_options_table_border_style or "")
    options: list[Text] = []
    rows = (
        ("-q, ", "--quality ", "TEXT            ", "Video quality to download", "  [default: best]", "default"),
        ("-r, ", "--range   ", "[SDR|HDR10|DV]  ", "Video color range", "  [env: RANGE]", "envvar"),
    )
    for short, long_opt, metavar, help_text, extra, extra_kind in rows:
        row = Text()
        row.append(short, style=help_config.style_switch)
        row.append(long_opt, style=help_config.style_option)
        row.append(metavar, style=help_config.style_metavar)
        row.append(f"  {help_text}", style=help_config.style_option_help)
        row.append(extra, style=getattr(help_config, f"style_option_{extra_kind}"))
        options.extend((row, separator))

    listing = Tree(Text("1 season, S1(1)", style=text), guide_style=guide)
    episode = listing.add(Text.assemble(("1. ", palette["blue"]), ("Pilot", text)), guide_style=guide)
    track_rows = (
        ("VID", "H.264 | 1920x1080 @ 4523 kb/s"),
        ("AUD", "AAC 2.0 | en @ 192 kb/s"),
        ("SUB", "WebVTT | en (SDH)"),
    )
    for kind, desc in track_rows:
        episode.add(Text.assemble((kind, palette["pink"]), (" | ", guide), (desc, text)), guide_style=guide)
    tracks = listing_panel(listing, "Available Tracks")

    logs = []
    log_rows = (
        ("INFO", "Downloading 3 tracks"),
        ("WARNING", "Subtitle cues overlapped, fixed 2"),
        ("ERROR", "License request denied (403)"),
    )
    for level, message in log_rows:
        line = Text()
        line.append("18:00:01 ", style=gray)
        line.append(f"{level:<8} ", style=f"logging.level.{level.lower()}")
        line.append(message, style=text if level != "ERROR" else palette["red"])
        logs.append(line)

    seconds = 5.0

    def bars(t: Optional[float]) -> RenderableType:
        """The bars *t* seconds into the sample. The download finishes at *seconds*, or at None."""
        now = seconds if t is None else t
        fill: RenderableType = (
            _GradientFill(fill_lut, now / seconds, now * PULSE_SPEED if scroll else None, 39, "bar.back")
            if fill_lut and now < seconds
            else ProgressBar(total=seconds, completed=now, width=39)
        )
        # style names, so a theme's `styles:` overrides show
        remaining = f"0:00:{math.ceil(seconds - now):02d}"
        speed = Text.assemble(" • ", (remaining, "progress.remaining"), " • ", ("12.4 MB/s", "progress.data.speed"))
        grid = Table.grid()
        grid.add_row(Text("Pilot  ", style=text), fill, speed)
        grid.add_row(Text("Muxing ", style=text), _GradientPulse(pulse_lut, 8 + now * PULSE_SPEED, PULSE_PERIOD, 39))
        return Padding(grid, (1, 2, 1, 2))

    block = Group(swatch(palette), Text(), docstring, *options, tracks, *logs)
    with console.use_theme(build_console_theme(palette, styles, config.set_terminal_bg)), use_gradients(gradients):
        scroll, fill_lut, pulse_lut = bar_gradients(palette)
        header = Padding(Rule(f"[rule.text]{name}"), (0, 2))
        still = console.render_lines(Group(header, banner(), Padding(block, (0, 2))), pad=False)

        def sample(t: Optional[float]) -> RenderableType:
            """The whole sample *t* seconds in. The gradient colours loop twice while the download runs."""
            lines = still if t is None else slide_gradients(still, 2 * t / seconds)
            return Group(SegmentLines(lines, new_lines=True), bars(t))

        # a live area taller than the terminal is cropped, so only the bars move
        if len(console.render_lines(sample(None), pad=False)) < console.size.height:
            play_preview(sample, seconds)
        else:
            console.print(SegmentLines(still, new_lines=True))
            play_preview(bars, seconds)


@env.command()
def info() -> None:
    """Displays information about the current environment."""
    log = logging.getLogger("env")

    if config_path:
        log.info(f"Config loaded from {config_path}")
    else:
        tree = Tree(
            "[text]No config file found, you can use any of the following locations:[/]", guide_style="bright_black"
        )
        for i, path in enumerate(POSSIBLE_CONFIG_PATHS, start=1):
            tree.add(f"[repr.number]{i}.[/] [text2]{path.resolve()}[/]")
        console.print(Padding(tree, (0, 5)))

    table = listing_table("Directories", expand=True, show_lines=True)
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Path", style="text2", no_wrap=False, overflow="fold")

    path_vars = {
        x: Path(os.getenv(x))
        for x in ("TEMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE")
        if sys.platform == "win32" and os.getenv(x)
    }

    rows = {
        name: getattr(config.directories, name)
        for name in dir(config.directories)
        if not name.startswith("__") and name != "app_dirs"
    }
    rows["user_binaries"] = binaries.user_binaries_dir
    for name in sorted(rows):
        attr_value = rows[name]

        if isinstance(attr_value, list):
            paths_str = "\n".join(str(p.resolve()) if isinstance(p, Path) else str(p) for p in attr_value)
            table.add_row(name.title(), paths_str)
        else:
            path = attr_value.resolve()
            for var, var_path in path_vars.items():
                if path.is_relative_to(var_path):
                    path = rf"%{var}%\{path.relative_to(var_path)}"
                    break
            table.add_row(name.title(), str(path))

    print_wide(table)


@env.group(name="clear", short_help="Clear an environment directory.", context_settings=context_settings)
def clear() -> None:
    """Clear an environment directory."""


@clear.command()
@click.argument("service", type=str, required=False)
def cache(service: Optional[str]) -> None:
    """Clear the environment cache directory."""
    log = logging.getLogger("env")
    cache_dir = config.directories.cache
    if service:
        cache_dir = cache_dir / Services.get_tag(service)
    log.info(f"Clearing cache directory: {cache_dir}")
    files_count, _ = clear_directory(cache_dir)
    if not files_count:
        log.info("No files to delete")
    else:
        log.info(f"Deleted {files_count} files")
        log.info("Cleared")


@clear.command()
def temp() -> None:
    """Clear the environment temp directory."""
    log = logging.getLogger("env")
    log.info(f"Clearing temp directory: {config.directories.temp}")
    files_count, _ = clear_directory(config.directories.temp)
    if not files_count:
        log.info("No files to delete")
    else:
        log.info(f"Deleted {files_count} files")
        log.info("Cleared")
