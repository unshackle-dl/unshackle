from __future__ import annotations

import logging
import re
from pathlib import Path

import click

from unshackle.core.config import config
from unshackle.core.service import Service
from unshackle.core.utilities import import_module_by_path

log = logging.getLogger("services")

_service_dirs = config.directories.services
if not isinstance(_service_dirs, list):
    _service_dirs = [_service_dirs]

_SERVICES = sorted(
    (path for service_dir in _service_dirs for path in service_dir.glob("*/__init__.py")),
    key=lambda x: x.parent.stem,
)


def load_service(path: Path) -> object:
    """Load one Service module, returning its tag-named class.

    Raises a concise, single-line error naming the Service and the real cause so
    a broken Service never surfaces as a raw traceback pointing at the loader.
    """
    tag = path.parent.stem
    try:
        module = import_module_by_path(path)
    except Exception as e:
        raise RuntimeError(f"{tag}: failed to import — {type(e).__name__}: {e} ({path})") from e
    try:
        return getattr(module, tag)
    except AttributeError as e:
        raise RuntimeError(
            f"{tag}: no class named '{tag}' found in {path} — the class name must match the directory name"
        ) from e


def load_services(paths: list[Path]) -> tuple[dict[str, object], list[str]]:
    """Load every Service, returning the good ones plus a list of load errors.

    Importing this module must never raise: it is imported by several commands,
    and a failed import is not cached by Python, so raising here would re-run and
    re-report for every command. Instead we collect failures and let the caller
    surface them once, cleanly, at the point services are actually used.
    """
    modules: dict[str, object] = {}
    errors: list[str] = []
    for path in paths:
        try:
            modules[path.parent.stem] = load_service(path)
        except Exception as e:
            errors.append(str(e))
    return modules, errors


_MODULES, LOAD_ERRORS = load_services(_SERVICES)

_ALIASES = {tag: getattr(module, "ALIASES", ()) for tag, module in _MODULES.items()}


def check_load_errors() -> None:
    """Raise a single clean error if any Service failed to load.

    Called when services are actually needed (listing/resolving) so the message
    is rendered once by Click, without a traceback and without cascading through
    every command that imports this module.
    """
    if LOAD_ERRORS:
        joined = "\n".join(f"  - {err}" for err in LOAD_ERRORS)
        raise click.ClickException(f"Failed to load {len(LOAD_ERRORS)} service(s):\n{joined}")


class Services(click.Group):
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
        check_load_errors()
        return Services.get_tags()

    def get_command(self, ctx: click.Context, name: str) -> click.Command:
        """Load the Service and return the Click CLI method."""
        check_load_errors()
        tag = Services.get_tag(name)

        import_file = ctx.params.get("import_file") or (ctx.parent and ctx.parent.params.get("import_file"))
        if import_file:
            return Services._make_import_command(tag, ctx)

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
    def _make_import_command(tag: str, ctx: click.Context) -> click.Command:
        """Create a synthetic command that yields an ImportService from an export JSON.

        Mirrors how remote services are wired so dl.py's result() runs unchanged.
        """

        @click.command(name=tag, short_help="Reconstruct a download from an export JSON.")
        @click.argument("title", type=str, required=False, default="")
        @click.pass_context
        def import_cli(ctx: click.Context, title: str, **kwargs: object) -> object:
            from unshackle.core.import_service import ImportService

            import_file = ctx.params.get("import_file") or (ctx.parent and ctx.parent.params.get("import_file"))
            return ImportService(ctx, tag, title, import_file)

        return import_cli

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

    @staticmethod
    def get_vault_tag(name: str) -> str:
        """Resolve the key-vault namespace tag for a service.

        Returns the service's VAULT_TAG override when set, otherwise its own tag.
        Falls back to the resolved tag for non-local services (remote/import).
        """
        tag = Services.get_tag(name)
        try:
            service = Services.load(tag)
        except KeyError:
            return tag
        return getattr(service, "VAULT_TAG", None) or tag


__all__ = ("Services",)
