import logging
from pathlib import Path
from typing import Optional

import click

from unshackle.core.config import config
from unshackle.core.utilities import import_module_by_path

log = logging.getLogger("commands")

_COMMANDS = sorted(
    (path for path in config.directories.commands.glob("*.py") if path.stem.lower() != "__init__"), key=lambda x: x.stem
)


def load_command(path: Path) -> object:
    """Load one command module, returning its stem-named attribute.

    Raises a concise, single-line error naming the command and the real cause so
    a broken command never surfaces as a raw traceback pointing at the loader.
    """
    try:
        module = import_module_by_path(path)
    except Exception as e:
        raise RuntimeError(f"{path.stem}: failed to import — {type(e).__name__}: {e} ({path})") from e
    try:
        return getattr(module, path.stem)
    except AttributeError as e:
        raise RuntimeError(
            f"{path.stem}: no object named '{path.stem}' found in {path} — it must match the filename"
        ) from e


def load_commands(paths: list[Path]) -> tuple[dict[str, object], list[str]]:
    """Load every command, returning the good ones plus a list of load errors.

    Importing this module must never raise (it runs at CLI startup, before Rich
    is installed, so a raise here prints an ugly pre-setup traceback). Instead we
    collect failures and surface them once, cleanly, when the CLI is used.
    """
    modules: dict[str, object] = {}
    errors: list[str] = []
    for path in paths:
        try:
            modules[path.stem] = load_command(path)
        except Exception as e:
            errors.append(str(e))
    return modules, errors


_MODULES, LOAD_ERRORS = load_commands(_COMMANDS)


def check_load_errors() -> None:
    """Raise a single clean error if any command failed to load."""
    if LOAD_ERRORS:
        joined = "\n".join(f"  - {err}" for err in LOAD_ERRORS)
        raise click.ClickException(f"Failed to load {len(LOAD_ERRORS)} command(s):\n{joined}")


class Commands(click.Group):
    """Lazy-loaded command group of project commands."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        """Returns a list of command names from the command filenames."""
        check_load_errors()
        return [x.stem.replace("_", "-") for x in _COMMANDS]

    def get_command(self, ctx: click.Context, name: str) -> Optional[click.Command]:
        """Load the command code and return the main click command function."""
        check_load_errors()
        module = _MODULES.get(name) or _MODULES.get(name.replace("-", "_"))
        if not module:
            raise click.ClickException(f"Unable to find command by the name '{name}'")

        if hasattr(module, "cli"):
            return module.cli

        return module


# Hide direct access to commands from quick import form, they shouldn't be accessed directly
__all__ = ("Commands",)
