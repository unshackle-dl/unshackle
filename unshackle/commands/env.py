import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

import click
from rich.padding import Padding
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


@env.command()
def theme() -> None:
    """Preview the available CLI themes."""
    import math

    from rich import box
    from rich.color import Color, blend_rgb
    from rich.console import Group
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.style import Style
    from rich.text import Text

    from unshackle.core.themes import ALIASES, DEFAULT_THEME, PALETTES, resolve_palette

    active = resolve_palette(config.theme) or PALETTES[DEFAULT_THEME]
    for name, palette in PALETTES.items():
        text, text2, gray, guide = palette["text"], palette["text2"], palette["gray"], palette["bright_black"]

        header = Text(name, style=palette["pink"])
        if palette == active:
            header.append("  (active)", style=palette["green"])
        aliases = sorted(alias for alias, target in ALIASES.items() if target == name)
        if aliases:
            header.append(f"  aliases: {', '.join(aliases)}", style=f"dim {gray}")

        swatch = Text()
        for role in ("red", "green", "yellow", "blue", "pink", "cyan", "text", "gray"):
            swatch.append("██ ", style=palette[role])

        docstring = Text()
        docstring.append("Service docstrings render like this, ", style=text)
        docstring.append("with secondary detail lines like this.", style=text2)

        separator = Text("─" * 60, style=palette["dark_gray"])
        options: list[Text] = []
        rows = (
            ("-q, ", "--quality ", "TEXT            ", "Video quality to download", "  [default: best]", f"dim {gray}"),
            (
                "-r, ",
                "--range   ",
                "[SDR|HDR10|DV]  ",
                "Video color range",
                "  [env: RANGE]",
                f"dim {palette['yellow']}",
            ),
        )
        for short, long_opt, metavar, help_text, extra, extra_style in rows:
            row = Text()
            row.append(short, style=palette["green"])
            row.append(long_opt, style=text)
            row.append(metavar, style=palette["yellow"])
            row.append(f"  {help_text}", style=text)
            row.append(extra, style=extra_style)
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
        tracks = Panel(listing, title="Available Tracks", box=box.SQUARE, border_style=guide, expand=False)

        logs = []
        log_rows = (
            ("INFO", palette["green"], "Downloading 3 tracks"),
            ("WARNING", palette["yellow"], "Subtitle cues overlapped, fixed 2"),
            ("ERROR", palette["red"], "License request denied (403)"),
        )
        for level, level_style, message in log_rows:
            line = Text()
            line.append("18:00:01 ", style=gray)
            line.append(f"{level:<8} ", style=level_style)
            line.append(message, style=text if level != "ERROR" else palette["red"])
            logs.append(line)

        progress = Text()
        progress.append("Pilot  ", style=text)
        progress.append("━" * 24, style=palette["green"])
        progress.append("╸", style=palette["green"])
        progress.append("━" * 14, style=guide)
        progress.append("  62%", style=palette["yellow"])
        progress.append("  12.4 MB/s", style=palette["cyan"])
        progress.append("  0:00:42", style=text2)

        # one frozen frame of GradientPulseBarColumn's dark_gray-to-pink cosine sweep
        lo = Color.parse(palette["dark_gray"]).triplet
        hi = Color.parse(palette["pink"]).triplet
        assert lo and hi
        lut = [Style(color=Color.from_triplet(blend_rgb(lo, hi, i / 31))) for i in range(32)]
        pulse = Text("Muxing ", style=text)
        for i in range(39):
            fade = (math.cos((i - 8) * math.tau / 40.0) + 1) / 2
            pulse.append("━", style=lut[int(fade * 31)])

        block = Group(header, swatch, Text(), docstring, *options, tracks, *logs, Text(), progress, pulse)
        print_wide(block, (1, 2, 1, 2))
        print_wide(Rule(style=guide, characters="─"), (0, 0))
    console.print()


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

    for name in sorted(dir(config.directories)):
        if name.startswith("__") or name == "app_dirs":
            continue
        attr_value = getattr(config.directories, name)

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
