import warnings

# Suppress SyntaxWarning from unmaintained tinycss package (dependency of subby)
# Must be set before any imports that might trigger tinycss loading
warnings.filterwarnings("ignore", category=SyntaxWarning, module="tinycss")

import atexit
import logging
from pathlib import Path

import click
import urllib3
from rich import traceback
from rich.console import Group
from rich.padding import Padding
from rich.text import Text
from urllib3.exceptions import InsecureRequestWarning

from unshackle.core import __version__
from unshackle.core.commands import Commands
from unshackle.core.config import config
from unshackle.core.console import ComfyRichHandler, console
from unshackle.core.constants import context_settings
from unshackle.core.update_checker import UpdateChecker
from unshackle.core.utilities import rotate_log_file

LOGGING_PATH = None


@click.command(cls=Commands, invoke_without_command=True, context_settings=context_settings)
@click.option("-v", "--version", is_flag=True, default=False, help="Print version information.")
@click.option("-d", "--debug", is_flag=True, default=False, help="Enable DEBUG level logs.")
@click.option(
    "--log",
    "log_path",
    type=Path,
    default=config.directories.logs / config.filenames.log,
    help="Log path (or filename). Path can contain the following f-string args: {name} {time}.",
)
def main(version: bool, debug: bool, log_path: Path) -> None:
    """unshackle—Modular Movie, TV, and Music Archival Software."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(message)s",
        handlers=[
            ComfyRichHandler(
                show_time=False,
                show_path=debug,
                console=console,
                rich_tracebacks=True,
                tracebacks_suppress=[click],
                log_renderer=console._log_render,  # noqa
            )
        ],
    )

    if log_path:
        global LOGGING_PATH
        console.record = True
        new_log_path = rotate_log_file(log_path)
        LOGGING_PATH = new_log_path

    urllib3.disable_warnings(InsecureRequestWarning)

    traceback.install(console=console, width=80, suppress=[click])

    console.print(
        Padding(
            Group(
                Text(
                    r"▄• ▄▌ ▐ ▄ .▄▄ ·  ▄ .▄ ▄▄▄·  ▄▄· ▄ •▄ ▄▄▌  ▄▄▄ ." + "\n"
                    r"█▪██▌•█▌▐█▐█ ▀. ██▪▐█▐█ ▀█ ▐█ ▌▪█▌▄▌▪██•  ▀▄.▀·" + "\n"
                    r"█▌▐█▌▐█▐▐▌▄▀▀▀█▄██▀▐█▄█▀▀█ ██ ▄▄▐▀▀▄·██▪  ▐▀▀▪▄" + "\n"
                    r"▐█▄█▌██▐█▌▐█▄▪▐███▌▐▀▐█ ▪▐▌▐███▌▐█.█▌▐█▌▐▌▐█▄▄▌" + "\n"
                    r" ▀▀▀ ▀▀ █▪ ▀▀▀▀ ▀▀▀ · ▀  ▀ ·▀▀▀ ·▀  ▀.▀▀▀  ▀▀▀ ",
                    style="ascii.art",
                ),
                f"v [repr.number]{__version__}[/] - © 2025 - github.com/unshackle-dl/unshackle",
            ),
            (1, 11, 1, 10),
            expand=True,
        ),
        justify="center",
    )

    if version:
        return

    if config.update_checks:
        try:
            latest_version = UpdateChecker.check_for_updates_sync(__version__)
            if latest_version:
                console.print(
                    f"\n[yellow]⚠️  Update available![/yellow] "
                    f"Current: {__version__} → Latest: [green]{latest_version}[/green]",
                    justify="center",
                )
                console.print(
                    "Visit: https://github.com/unshackle-dl/unshackle/releases/latest\n",
                    justify="center",
                )
        except Exception:
            pass


@atexit.register
def save_log():
    if console.record and LOGGING_PATH:
        # TODO: Currently semi-bust. Everything that refreshes gets duplicated.
        console.save_text(LOGGING_PATH)


if __name__ == "__main__":
    main()
