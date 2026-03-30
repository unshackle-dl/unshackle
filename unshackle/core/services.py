from __future__ import annotations

import re
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

    _remote_services_cache: list[dict] | None = None

    # Click-specific methods

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Preprocess --slow to support optional range value before Click parses args."""
        processed = []
        i = 0
        while i < len(args):
            if args[i] == "--slow":
                if i + 1 < len(args) and re.match(r"^\d+-\d+$", args[i + 1]):
                    processed.append(f"--slow={args[i + 1]}")
                    i += 2
                else:
                    processed.append("--slow=60-120")
                    i += 1
            else:
                processed.append(args[i])
                i += 1
        return super().parse_args(ctx, processed)

    def list_commands(self, ctx: click.Context) -> list[str]:
        """Returns a list of all available Services as command names for Click.

        In remote mode, fetches the service list from the remote server
        so the user sees exactly what's available remotely.
        """
        remote = ctx.params.get("remote") or (ctx.parent and ctx.parent.params.get("remote"))
        if remote:
            remote_services = Services._fetch_remote_services(ctx)
            if remote_services is not None:
                return [s["tag"] for s in remote_services]
            tags = Services.get_tags()
            for svc_cfg in config.remote_services.values():
                for remote_tag in svc_cfg.get("services", {}).keys():
                    if remote_tag not in tags:
                        tags.append(remote_tag)
            return tags
        return Services.get_tags()

    def get_command(self, ctx: click.Context, name: str) -> click.Command:
        """Load the Service and return the Click CLI method."""
        tag = Services.get_tag(name)

        remote = ctx.params.get("remote") or (ctx.parent and ctx.parent.params.get("remote"))
        if remote:
            return Services._make_remote_command(tag, ctx)

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

    @staticmethod
    def _fetch_remote_services(ctx: click.Context) -> list[dict] | None:
        """Fetch the service list from the remote server (cached per process)."""
        if Services._remote_services_cache is not None:
            return Services._remote_services_cache
        try:
            from unshackle.core.remote_service import RemoteClient, resolve_server

            server_name = ctx.params.get("server")
            server_url, api_key, _ = resolve_server(server_name)
            client = RemoteClient(server_url, api_key)
            result = client.get("/api/services")
            Services._remote_services_cache = result.get("services", [])
            return Services._remote_services_cache
        except Exception:
            return None

    @staticmethod
    def _make_remote_command(tag: str, ctx: click.Context) -> click.Command:
        """Create a Click command for a remote service with server-provided options."""
        svc_info = Services._fetch_remote_service_info(tag, ctx)
        short_help = svc_info.get("url") if svc_info else None
        cli_params = svc_info.get("cli_params") if svc_info else None

        @click.command(name=tag, short_help=short_help)
        @click.argument("title", type=str)
        @click.pass_context
        def remote_cli(ctx: click.Context, title: str, **kwargs: object) -> object:
            from unshackle.core.remote_service import RemoteService, resolve_server

            server_name = ctx.parent.params.get("server") if ctx.parent else None
            server_url, api_key, services_config = resolve_server(server_name)
            service_params = {k: v for k, v in kwargs.items() if v is not None and v is not False}
            return RemoteService(ctx, tag, title, server_url, api_key, services_config, service_params=service_params)

        if cli_params:
            for param in cli_params:
                if param.get("kind") == "option":
                    opts = param.get("opts", [f"--{param['name']}"])
                    kwargs: dict = {}
                    if param.get("is_flag"):
                        kwargs["is_flag"] = True
                        kwargs["default"] = param.get("default", False)
                    else:
                        kwargs["default"] = param.get("default")
                        kwargs["type"] = str
                    if param.get("help"):
                        kwargs["help"] = param["help"]
                    remote_cli = click.option(*opts, **kwargs)(remote_cli)

        return remote_cli

    @staticmethod
    def _fetch_remote_service_info(tag: str, ctx: click.Context) -> dict | None:
        """Fetch service info for a specific service from the remote server."""
        try:
            services = Services._fetch_remote_services(ctx)
            if services:
                for svc in services:
                    if svc.get("tag") == tag:
                        return svc
        except Exception:
            pass
        return None

    # Methods intended to be used anywhere

    @staticmethod
    def get_tags() -> list[str]:
        """Returns a list of service tags from all available Services."""
        return [x.parent.stem for x in _SERVICES]

    @staticmethod
    def get_path(name: str) -> Path:
        """Get the directory path of a command."""
        tag = Services.get_tag(name)

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

        for path in _SERVICES:
            tag = path.parent.stem
            if value in (tag.lower(), *_ALIASES.get(tag, [])):
                return tag

        return original_value

    @staticmethod
    def load(tag: str) -> Service:
        """Load a Service module by Service tag."""
        module = _MODULES.get(tag)
        if module:
            return module

        raise KeyError(f"There is no Service added by the Tag '{tag}'")


__all__ = ("Services",)
