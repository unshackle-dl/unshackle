import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

import click
from rich.padding import Padding
from rich.table import Table
from rich.tree import Tree

from unshackle.core import binaries
from unshackle.core.config import POSSIBLE_CONFIG_PATHS, config, config_path
from unshackle.core.console import console
from unshackle.core.constants import context_settings
from unshackle.core.services import Services


@click.group(short_help="Manage and configure the project environment.", context_settings=context_settings)
def env() -> None:
    """Manage and configure the project environment."""


@env.command()
def check() -> None:
    """Checks environment for the required dependencies."""
    # Define all dependencies
    all_deps = [
        # Core Media Tools
        {"name": "FFmpeg", "binary": binaries.FFMPEG, "required": True, "desc": "Media processing", "cat": "Core"},
        {"name": "FFprobe", "binary": binaries.FFProbe, "required": True, "desc": "Media analysis", "cat": "Core"},
        {"name": "MKVToolNix", "binary": binaries.MKVToolNix, "required": True, "desc": "MKV muxing", "cat": "Core"},
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
        # HDR Processing
        {"name": "dovi_tool", "binary": binaries.DoviTool, "required": False, "desc": "Dolby Vision", "cat": "HDR"},
        {
            "name": "HDR10Plus_tool",
            "binary": binaries.HDR10PlusTool,
            "required": False,
            "desc": "HDR10+ metadata",
            "cat": "HDR",
        },
        # Downloaders
        {"name": "aria2c", "binary": binaries.Aria2, "required": False, "desc": "Multi-thread DL", "cat": "Download"},
        {
            "name": "N_m3u8DL-RE",
            "binary": binaries.N_m3u8DL_RE,
            "required": False,
            "desc": "HLS/DASH/ISM",
            "cat": "Download",
        },
        # Subtitle Tools
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
        # Media Players
        {"name": "FFplay", "binary": binaries.FFPlay, "required": False, "desc": "Simple player", "cat": "Player"},
        {"name": "MPV", "binary": binaries.MPV, "required": False, "desc": "Advanced player", "cat": "Player"},
        # Network Tools
        {
            "name": "HolaProxy",
            "binary": binaries.HolaProxy,
            "required": False,
            "desc": "Proxy service",
            "cat": "Network",
        },
        {"name": "Caddy", "binary": binaries.Caddy, "required": False, "desc": "Web server", "cat": "Network"},
    ]

    # Track overall status
    all_required_installed = True
    total_installed = 0
    total_required = 0
    missing_required = []

    # Create a single table
    table = Table(
        title="Environment Dependencies", title_style="bold", show_header=True, header_style="bold", expand=False
    )
    table.add_column("Category", style="bold cyan", width=10)
    table.add_column("Tool", width=16)
    table.add_column("Status", justify="center", width=10)
    table.add_column("Req", justify="center", width=4)
    table.add_column("Purpose", style="bright_black", width=20)

    last_cat = None
    for dep in all_deps:
        path = dep["binary"]

        # Category column (only show when it changes)
        category = dep["cat"] if dep["cat"] != last_cat else ""
        last_cat = dep["cat"]

        # Status
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

        # Required column (compact)
        req = "[red]Y[/red]" if dep["required"] else "[bright_black]-[/bright_black]"

        # Add row
        table.add_row(category, dep["name"], status, req, dep["desc"])

    console.print(Padding(table, (1, 2)))

    # Compact summary
    summary_parts = [f"[bold]Total:[/bold] {total_installed}/{len(all_deps)}"]

    if all_required_installed:
        summary_parts.append("[green]All required tools installed ✓[/green]")
    else:
        summary_parts.append(f"[red]Missing required: {', '.join(missing_required)}[/red]")

    console.print(Padding("  ".join(summary_parts), (1, 2)))


@env.command()
def info() -> None:
    """Displays information about the current environment."""
    log = logging.getLogger("env")

    if config_path:
        log.info(f"Config loaded from {config_path}")
    else:
        tree = Tree("No config file found, you can use any of the following locations:")
        for i, path in enumerate(POSSIBLE_CONFIG_PATHS, start=1):
            tree.add(f"[repr.number]{i}.[/] [text2]{path.resolve()}[/]")
        console.print(Padding(tree, (0, 5)))

    table = Table(title="Directories", title_style="bold", expand=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("Path", no_wrap=False, overflow="fold")

    path_vars = {
        x: Path(os.getenv(x))
        for x in ("TEMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE")
        if sys.platform == "win32" and os.getenv(x)
    }

    for name in sorted(dir(config.directories)):
        if name.startswith("__") or name == "app_dirs":
            continue
        attr_value = getattr(config.directories, name)

        # Handle both single Path objects and lists of Path objects
        if isinstance(attr_value, list):
            # For lists, show each path on a separate line
            paths_str = "\n".join(str(path.resolve()) for path in attr_value)
            table.add_row(name.title(), paths_str)
        else:
            # For single Path objects, use the original logic
            path = attr_value.resolve()
            for var, var_path in path_vars.items():
                if path.is_relative_to(var_path):
                    path = rf"%{var}%\{path.relative_to(var_path)}"
                    break
            table.add_row(name.title(), str(path))

    console.print(Padding(table, (1, 5)))


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
    files_count = len(list(cache_dir.glob("**/*")))
    if not files_count:
        log.info("No files to delete")
    else:
        log.info(f"Deleting {files_count} files...")
        shutil.rmtree(cache_dir)
        log.info("Cleared")


@clear.command()
def temp() -> None:
    """Clear the environment temp directory."""
    log = logging.getLogger("env")
    log.info(f"Clearing temp directory: {config.directories.temp}")
    files_count = len(list(config.directories.temp.glob("**/*")))
    if not files_count:
        log.info("No files to delete")
    else:
        log.info(f"Deleting {files_count} files...")
        shutil.rmtree(config.directories.temp)
        log.info("Cleared")
