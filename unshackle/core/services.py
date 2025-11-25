from pathlib import Path

import click

from unshackle.core.config import config
from unshackle.core.service import Service
from unshackle.core.utilities import import_module_by_path

_service_dirs = config.directories.services
if not isinstance(_service_dirs, list):
    _service_dirs = [_service_dirs]

_SERVICES = sorted(
    (path for service_dir in _service_dirs for path in service_dir.glob("*/__init__.py")),
    key=lambda x: x.parent.stem,
)

_MODULES = {path.parent.stem: getattr(import_module_by_path(path), path.parent.stem) for path in _SERVICES}

_ALIASES = {tag: getattr(module, "ALIASES") for tag, module in _MODULES.items()}


class Services(click.MultiCommand):
    """Lazy-loaded command group of project services."""

    # Click-specific methods

    @staticmethod
    def _get_remote_services():
        """Get remote services from the manager (lazy import to avoid circular dependency)."""
        try:
            from unshackle.core.remote_services import get_remote_service_manager

            manager = get_remote_service_manager()
            return manager.get_all_services()
        except Exception:
            return {}

    def list_commands(self, ctx: click.Context) -> list[str]:
        """Returns a list of all available Services as command names for Click."""
        return Services.get_tags()

    def get_command(self, ctx: click.Context, name: str) -> click.Command:
        """Load the Service and return the Click CLI method."""
        tag = Services.get_tag(name)
        try:
            service = Services.load(tag)
        except KeyError as e:
            available_services = self.list_commands(ctx)
            if not available_services:
                raise click.ClickException(
                    f"There are no Services added yet, therefore the '{name}' Service could not be found."
                )
            raise click.ClickException(f"{e}. Available Services: {', '.join(available_services)}")

        if hasattr(service, "cli"):
            return service.cli

        raise click.ClickException(f"Service '{tag}' has no 'cli' method configured.")

    # Methods intended to be used anywhere

    @staticmethod
    def get_tags() -> list[str]:
        """Returns a list of service tags from all available Services (local + remote)."""
        local_tags = [x.parent.stem for x in _SERVICES]
        remote_services = Services._get_remote_services()
        remote_tags = list(remote_services.keys())
        return local_tags + remote_tags

    @staticmethod
    def get_path(name: str) -> Path:
        """Get the directory path of a command."""
        tag = Services.get_tag(name)

        # Check if it's a remote service
        remote_services = Services._get_remote_services()
        if tag in remote_services:
            # Remote services don't have local paths
            # Return a dummy path or raise an appropriate error
            # For now, we'll raise KeyError to indicate no path exists
            raise KeyError(f"Remote service '{tag}' has no local path")

        for service in _SERVICES:
            if service.parent.stem == tag:
                return service.parent
        raise KeyError(f"There is no Service added by the Tag '{name}'")

    @staticmethod
    def get_tag(value: str) -> str:
        """
        Get the Service Tag (e.g. DSNP, not DisneyPlus/Disney+, etc.) by an Alias.
        Input value can be of any case-sensitivity.
        Original input value is returned if it did not match a service tag.
        """
        original_value = value
        value = value.lower()

        # Check local services
        for path in _SERVICES:
            tag = path.parent.stem
            if value in (tag.lower(), *_ALIASES.get(tag, [])):
                return tag

        # Check remote services
        remote_services = Services._get_remote_services()
        for tag, service_class in remote_services.items():
            if value == tag.lower():
                return tag
            if hasattr(service_class, "ALIASES"):
                if value in (alias.lower() for alias in service_class.ALIASES):
                    return tag

        return original_value

    @staticmethod
    def load(tag: str) -> Service:
        """Load a Service module by Service tag (local or remote)."""
        # Check local services first
        module = _MODULES.get(tag)
        if module:
            return module

        # Check remote services
        remote_services = Services._get_remote_services()
        if tag in remote_services:
            return remote_services[tag]

        raise KeyError(f"There is no Service added by the Tag '{tag}'")


__all__ = ("Services",)
