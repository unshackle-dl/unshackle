from typing import Optional

import click

from unshackle.core.config import config
from unshackle.core.utilities import import_module_by_path

_COMMANDS = sorted(
    (path for path in config.directories.commands.glob("*.py") if path.stem.lower() != "__init__"), key=lambda x: x.stem
)
_COMMAND_PATHS = {path.stem: path for path in _COMMANDS}
_MODULES = {}


def _load_command(name: str):
    """Import and cache a command module the first time it is requested."""
    module = _MODULES.get(name)
    if module:
        return module

    path = _COMMAND_PATHS.get(name)
    if not path:
        return None

    module = getattr(import_module_by_path(path), path.stem)
    _MODULES[name] = module
    return module


class Commands(click.MultiCommand):
    """Lazy-loaded command group of project commands."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        """Returns a list of command names from the command filenames."""
        return [x.stem for x in _COMMANDS]

    def get_command(self, ctx: click.Context, name: str) -> Optional[click.Command]:
        """Load the command code and return the main click command function."""
        module = _load_command(name)
        if not module:
            raise click.ClickException(f"Unable to find command by the name '{name}'")

        if hasattr(module, "cli"):
            return module.cli

        return module


# Hide direct access to commands from quick import form, they shouldn't be accessed directly
__all__ = ("Commands",)
