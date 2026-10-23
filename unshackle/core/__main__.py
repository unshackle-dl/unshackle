import atexit
import logging
import signal
import sys
from datetime import datetime
from typing import Optional

import click
import urllib3
from rich import traceback
from rich.console import Group
from rich.padding import Padding
from rich.text import Text
from urllib3.exceptions import InsecureRequestWarning

from unshackle.core import __code_hash__, __version__
from unshackle.core import console as console_module
from unshackle.core.commands import Commands
from unshackle.core.config import config
from unshackle.core.console import ComfyRichHandler, Gradient, PinnedLive, console
from unshackle.core.constants import context_settings
from unshackle.core.update_checker import UpdateChecker
from unshackle.core.utilities import close_debug_logger, init_debug_logger


def banner() -> Padding:
    """The ASCII banner with the version line."""
    return Padding(
        Group(
            Gradient(
                Text(
                    r"▄• ▄▌ ▐ ▄ .▄▄ ·  ▄ .▄ ▄▄▄·  ▄▄· ▄ •▄ ▄▄▌  ▄▄▄ ." + "\n"
                    r"█▪██▌•█▌▐█▐█ ▀. ██▪▐█▐█ ▀█ ▐█ ▌▪█▌▄▌▪██•  ▀▄.▀·" + "\n"
                    r"█▌▐█▌▐█▐▐▌▄▀▀▀█▄██▀▐█▄█▀▀█ ██ ▄▄▐▀▀▄·██▪  ▐▀▀▪▄" + "\n"
                    r"▐█▄█▌██▐█▌▐█▄▪▐███▌▐▀▐█ ▪▐▌▐███▌▐█.█▌▐█▌▐▌▐█▄▄▌" + "\n"
                    r" ▀▀▀ ▀▀ █▪ ▀▀▀▀ ▀▀▀ · ▀  ▀ ·▀▀▀ ·▀  ▀.▀▀▀  ▀▀▀ ",
                    style="ascii.art",
                    justify="center",
                ),
                "banner",
                "ascii.art",
            ),
            Text.from_markup(
                f"v [repr.number]{__version__}[/]{f' ({__code_hash__})' if __code_hash__ else ''}"
                f" - © 2025-{datetime.now().year} github.com/unshackle-dl/unshackle",
                justify="center",
            ),
        ),
        (1, 11, 1, 10),
        expand=True,
    )


def print_banner() -> None:
    """Print the ASCII banner with the version line."""
    console.print(banner())


def pins_banner(subcommand: Optional[str], args: list[str]) -> bool:
    """Whether to pin the moving banner over this command's output: only dl, and not for its help.

    It needs a theme with a banner gradient and a colour terminal. Help prints while Click parses the arguments,
    and the pinned area would wrap it again at its own width; usage errors print after the pinned area closes.
    """
    if subcommand != "dl" or not console_module.theme_gradients.get("banner") or not PinnedLive.wanted(console):
        return False
    help_options: list[str] = context_settings["help_option_names"]  # type: ignore[assignment]
    return not set(args) & set(help_options)


class Main(Commands):
    """The top-level group. Its help shows the banner, because its callback does not run for --help."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        print_banner()
        super().format_help(ctx, formatter)


@click.command(cls=Main, invoke_without_command=True, context_settings=context_settings)
@click.option("-v", "--version", is_flag=True, default=False, help="Print version information.")
@click.option("-d", "--debug", is_flag=True, default=False, help="Enable DEBUG level logs and JSON debug logging.")
@click.pass_context
def main(ctx: click.Context, version: bool, debug: bool) -> None:
    """unshackle: Modular Movie, TV, and Music Archival Software."""
    debug_logging_enabled = debug or config.debug

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

    if debug_logging_enabled:
        init_debug_logger(enabled=True)

    if debug and not config.debug_requests:
        for noisy in ("urllib3", "urllib3.connectionpool", "requests", "rnet", "httpx", "httpcore", "hpack", "h2"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    urllib3.disable_warnings(InsecureRequestWarning)

    traceback.install(console=console, width=80, suppress=[click])

    if "serve" in sys.argv[1:]:
        serve_args = sys.argv[sys.argv.index("serve") + 1 :]
        if "--quiet" in serve_args or "-q" in serve_args:
            return

    if pins_banner(ctx.invoked_subcommand, sys.argv[1:]):
        pinned = PinnedLive(banner(), console)
        pinned.start(refresh=True)
        ctx.call_on_close(pinned.close)
        if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
            signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    else:
        print_banner()

    if version:
        return

    if config.update_checks:
        try:
            latest_version = UpdateChecker.check_for_updates_sync(__version__)
            if latest_version:
                console.print(
                    f"\n[yellow]Update available![/yellow] "
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
def cleanup():
    """Clean up resources on exit."""
    close_debug_logger()


if __name__ == "__main__":
    main()
