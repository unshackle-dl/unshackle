from __future__ import annotations

import inspect
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import click

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.service import Service
from unshackle.core.service_repo import DirtyServiceRepo, head, is_repo_spec, refresh_repo, resolve_service_repo
from unshackle.core.utilities import import_module_by_path
from unshackle.core.utils.redact import redact_path

log = logging.getLogger("services")

DIRTY_REPOS: list[str] = []
SHADOWED: list[str] = []


def discover_services() -> tuple[list[Path], list[str]]:
    """Resolve the configured service dirs and glob every ``TAG/__init__.py``.

    Priority IS list order: the first source to define a tag is the source, and discovery
    shadows a later source (local or repo) with the same tag. List local last to make it a
    fallback.
    Returns the sorted service paths and the human-readable shadow lines.
    """
    raw = config.directories.services
    if not isinstance(raw, list):
        raw = [raw]
    service_dirs: list[Path] = []
    for entry in raw:
        if isinstance(entry, str) and is_repo_spec(entry):
            try:
                resolved = resolve_service_repo(entry, force=config.services_repo_force)
            except DirtyServiceRepo as e:
                # local edits in the clone - record it and let check_load_errors() exit cleanly
                if (dirty := redact_path(str(e.path))) not in DIRTY_REPOS:
                    DIRTY_REPOS.append(dirty)
                resolved = None
            if resolved:
                service_dirs.append(resolved)
        else:
            service_dirs.append(Path(entry))

    seen: dict[str, Path] = {}
    shadowed: list[str] = []
    for service_dir in service_dirs:
        for path in service_dir.glob("*/__init__.py"):
            tag = path.parent.stem
            if tag in seen:
                shadowed.append(
                    f"{tag}: using {redact_path(str(seen[tag]))}, ignoring duplicate {redact_path(str(path))}"
                )
            else:
                seen[tag] = path
    return sorted(seen.values(), key=lambda x: x.parent.stem), shadowed


SERVICES, SHADOWED = discover_services()


def load_service(path: Path) -> object:
    """Load one Service module, returning its tag-named class.

    Raises a concise, single-line error naming the Service and the real cause so
    a broken Service never surfaces as a raw traceback pointing at the loader.
    """
    tag = path.parent.stem
    try:
        module = import_module_by_path(path)
    except Exception as e:
        raise RuntimeError(f"{tag}: failed to import - {type(e).__name__}: {e} ({path})") from e
    try:
        return getattr(module, tag)
    except AttributeError as e:
        raise RuntimeError(
            f"{tag}: no class named '{tag}' found in {path} - the class name must match the directory name"
        ) from e


def load_services(paths: list[Path]) -> tuple[dict[str, object], list[str]]:
    """Load every Service, returning the good ones plus a list of load errors.

    Importing this module must never raise: several commands import it, and Python
    does not cache a failed import, so raising here would re-run and re-report for
    every command. Instead we collect failures and let the caller surface them once,
    cleanly, when it uses the services.
    """
    modules: dict[str, object] = {}
    errors: list[str] = []
    for path in paths:
        try:
            modules[path.parent.stem] = load_service(path)
        except Exception as e:
            errors.append(str(e))
    return modules, errors


def register_service_binaries(modules: dict[str, object]) -> None:
    """Register custom binary dependencies declared by services via get_binaries()."""

    for tag, service_cls in modules.items():
        get_binaries_fn = getattr(service_cls, "get_binaries", None)
        if callable(get_binaries_fn):
            try:
                for dep in get_binaries_fn():
                    name = dep.get("name")
                    if not name:
                        continue
                    candidates = dep.get("candidates") or (name.lower(),)
                    desc = dep.get("desc", f"{tag} dependency")
                    binaries.register(name, *candidates, desc=desc)
            except Exception as e:
                log.warning(f"Failed to register custom binaries for service {tag}: {e}")


MODULES, LOAD_ERRORS = load_services(SERVICES)
register_service_binaries(MODULES)

ALIASES = {tag: getattr(module, "ALIASES", ()) for tag, module in MODULES.items()}

PENDING: set[str] = set()
PENDING_SINCE: dict[str, float] = {}
LOADED_COMMITS: dict[str, str] = {}
RELOAD_LOCK = threading.Lock()


def record_loaded_commits(tags: Iterable[str] | None = None) -> None:
    """Stamp the repo HEAD each tag was imported from, so a staged update is visibly different.

    ``refresh_and_reload`` pulls before it defers a reload, so the working tree already holds
    the new commit while the import is still the old one; without this the two are the same
    hash and the staged state cannot be told apart.
    """
    wanted = set(tags) if tags is not None else None
    by_dir: dict[Path, str | None] = {}
    for path in SERVICES:
        tag = path.parent.stem
        if wanted is not None and tag not in wanted:
            continue
        source = path.parent.parent
        if source not in by_dir:
            by_dir[source] = head(source) if (source / ".git").exists() else None
        commit = by_dir[source]
        if commit:
            LOADED_COMMITS[tag] = commit
        else:
            LOADED_COMMITS.pop(tag, None)


def service_source_dir(tag: str) -> Path | None:
    """The directory a tag was discovered in: its repo clone, or a local services dir."""
    for path in SERVICES:
        if path.parent.stem == tag:
            return path.parent.parent
    return None


def reload_services(tags: Iterable[str]) -> list[str]:
    """Re-import the given service tags after a repo refresh; returns load errors.

    Mutates every module global in place so importers that bound ``SERVICES``,
    ``MODULES`` or ``ALIASES`` by name see the swap. Drops a tag that discovery no
    longer finds instead of reloading it.
    """
    wanted = set(tags)
    if not wanted:
        return []
    with RELOAD_LOCK:
        for tag in wanted:
            for name in [n for n in sys.modules if n == tag or n.startswith(f"{tag}.")]:
                del sys.modules[name]
            LOAD_ERRORS[:] = [err for err in LOAD_ERRORS if not err.startswith(f"{tag}: ")]

        paths, shadowed = discover_services()
        SERVICES[:] = paths
        SHADOWED[:] = shadowed

        errors: list[str] = []
        imported: list[str] = []
        found = {path.parent.stem: path for path in paths}
        for tag in wanted:
            if tag not in found:
                MODULES.pop(tag, None)
                ALIASES.pop(tag, None)
                continue
            try:
                MODULES[tag] = load_service(found[tag])
                ALIASES[tag] = getattr(MODULES[tag], "ALIASES", ())
                imported.append(tag)
            except Exception as e:
                errors.append(str(e))
        LOAD_ERRORS.extend(errors)
        register_service_binaries(MODULES)
        PENDING.difference_update(wanted)
        for tag in wanted:
            PENDING_SINCE.pop(tag, None)
        # Only the tags that imported: a failed re-import keeps the old module running, so
        # stamping it with the new HEAD would name code that is not running.
        record_loaded_commits(imported)
        return errors


def repo_specs() -> list[str]:
    """The git repo specs configured under ``directories.services``."""
    raw = config.directories.services
    entries: list[Any] = raw if isinstance(raw, list) else [raw]
    return [e for e in entries if isinstance(e, str) and is_repo_spec(e)]


def refresh_and_reload(busy: set[str] | None = None) -> list[dict[str, Any]]:
    """Force-sync every configured service repo and re-import the services that changed.

    Tags in ``busy`` (services with a download job that has not finished) go to ``PENDING``
    instead and appear as ``deferred``; ``apply_pending`` swaps them in later.
    Blocking (git subprocess): callers wrap it in ``asyncio.to_thread``.
    """
    busy = busy or set()
    repos: list[dict[str, Any]] = []
    for spec in repo_specs():
        dest, changes = refresh_repo(spec)
        tags = {line[1:] for line in changes if line[:1] in "+~-!"}
        if dest and "cloned (new)" in changes:
            tags.update(p.parent.stem for p in dest.glob("*/__init__.py"))
        deferred = sorted(tags & busy)
        PENDING.update(deferred)
        now = time.time()
        for tag in deferred:
            PENDING_SINCE.setdefault(tag, now)
        errors = reload_services(tags - busy)
        repos.append(
            {
                "spec": spec,
                "updated": dest is not None,
                "changes": list(changes),
                "deferred": deferred,
                "load_errors": errors,
            }
        )
    return repos


def failed_tags(errors: Iterable[str]) -> set[str]:
    """The tags named by a list of load errors, which are all formatted ``TAG: reason``."""
    return {err.split(":", 1)[0] for err in errors if ":" in err}


def apply_pending(busy: set[str] | None = None) -> list[str]:
    """Reload every staged tag whose service is no longer busy; returns the tags now running.

    A tag whose re-import failed is not one of them: the old module keeps serving, so calling
    it applied would tell the dashboard that code is live when it is not.
    """
    ready = sorted(PENDING - (busy or set()))
    if not ready:
        return []
    errors = reload_services(ready)
    for err in errors:
        log.error("Service reload failed: %s", err)
    failed = failed_tags(errors)
    return [tag for tag in ready if tag not in failed]


SUMMARY_LOGGED = False


def log_load_issues() -> None:
    """Log every service load problem without raising - for long-running hosts like serve.

    The loader skips broken services (the rest keep working), so the admin needs the log
    line the CLI would otherwise render as an error box.
    """
    log.info(f"Loaded {len(MODULES)} services" + (f" ({len(SHADOWED)} duplicate(s) ignored)" if SHADOWED else ""))
    for shadow in SHADOWED:
        log.debug("%s", shadow)
    for path in DIRTY_REPOS:
        log.warning(f"Service repo has local changes, not refreshed: {path}")
    for err in LOAD_ERRORS:
        log.error(f"Service skipped: {err}")


def check_load_errors() -> None:
    """Log a one-line load summary (once) and raise a single clean error on failures.

    unshackle calls this when it needs the services (listing/resolving), so Click
    renders the message once, without a traceback and without cascading through every
    command that imports this module. This function summarises the duplicate services
    (a tag that an earlier, higher-priority source also gives, and which unshackle
    ignores). The full list - naming the path used and the duplicate ignored - is
    debug-only.
    """
    if DIRTY_REPOS:
        joined = "\n".join(f"  - {p}" for p in DIRTY_REPOS)
        raise click.ClickException(
            "Service repo has local changes - refusing to refresh so your edits are not lost.\n"
            "Commit and push them to the upstream repo (or revert them), then retry:\n" + joined
        )
    global SUMMARY_LOGGED
    if not SUMMARY_LOGGED:
        SUMMARY_LOGGED = True
        summary = f"Loaded {len(MODULES)} services"
        if SHADOWED:
            summary += f" ({len(SHADOWED)} duplicate(s) ignored)"
        log.info(summary)
        for shadow in SHADOWED:
            log.debug("%s", shadow)
    if LOAD_ERRORS:
        joined = "\n".join(f"  - {err}" for err in LOAD_ERRORS)
        raise click.ClickException(f"Failed to load {len(LOAD_ERRORS)} service(s):\n{joined}")


class Services(click.Group):
    """Lazy-loaded command group of project services."""

    remote_services_cache: list[dict] | None = None

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Name the commands panel ``Services``, because this group's commands are services."""
        config = getattr(formatter, "config", None)
        if config is not None:
            config.commands_panel_title = "Services"
        super().format_help(ctx, formatter)

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Preprocess ``--slow`` so it can take an optional range value before Click parses the arguments."""
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
            remote_services = Services.fetch_remote_services(ctx)
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
            return Services.make_import_command(tag, ctx)

        remote = ctx.params.get("remote") or (ctx.parent and ctx.parent.params.get("remote"))
        if remote:
            return Services.make_remote_command(name, ctx)

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
            cli = service.cli
            cli.name = tag
            doc = service.__doc__
            if doc and doc.strip() and cli.help in (None, "", doc):
                cli.help = Services.docstring_help(doc)
            return cli

        raise click.ClickException(f"Service '{tag}' has no 'cli' method configured.")

    @staticmethod
    def docstring_help(doc: str) -> str:
        """Format a service docstring for Click help, one \\b per paragraph so Click keeps the layout.

        The first paragraph stays unprefixed: it is the summary line, and a \\b there
        renders as a stray blank line and empties click's derived short help.
        """
        doc = inspect.cleandoc(doc)
        if "\b" in doc:
            return doc
        first, *rest = doc.split("\n\n")
        return "\n\n".join([first] + [f"\b\n{p}" for p in rest])

    @staticmethod
    def fetch_remote_services(ctx: click.Context) -> list[dict] | None:
        """Fetch the service list from the remote server (cached per process)."""
        if Services.remote_services_cache is not None:
            return Services.remote_services_cache
        try:
            from unshackle.core.remote_service import RemoteClient, resolve_server

            server_name = ctx.params.get("server")
            server_url, api_key, services_config = resolve_server(server_name)
            client = RemoteClient(server_url, api_key, services_config.get("_auth_headers"))
            result = client.get("/api/services")
            Services.remote_services_cache = result.get("services", [])
            return Services.remote_services_cache
        except click.ClickException:
            raise
        except Exception:
            return None

    @staticmethod
    def make_remote_command(name: str, ctx: click.Context) -> click.Command:
        """Make a Click command for a remote service, with the options the remote server gives."""
        # A failed fetch (None) means the remote server was unreachable, not that the name is
        # invalid - fall through to a bare stub so `-h` keeps working offline.
        services = Services.fetch_remote_services(ctx)
        if services is None:
            svc_info = None
            tag = Services.get_tag(name)
        else:
            # Resolve against the remote server's tags and aliases, not the local tables: the
            # two drift apart as soon as either side's services change. Tags win over aliases,
            # as in local get_tag.
            lowered = name.lower()
            svc_info = next((svc for svc in services if str(svc.get("tag", "")).lower() == lowered), None)
            if svc_info is None:
                svc_info = next(
                    (svc for svc in services if lowered in (str(a).lower() for a in svc.get("aliases") or [])), None
                )
            if svc_info is None:
                if services:
                    available = ", ".join(sorted(svc["tag"] for svc in services))
                    raise click.ClickException(
                        f"The remote server does not offer a service named '{name}'. "
                        f"It offers these services: {available}"
                    )
                raise click.ClickException("The remote server offers no services to your API key.")
            tag = svc_info["tag"]
        short_help = svc_info.get("url") if svc_info else None
        help_text = svc_info.get("help") if svc_info else None
        if help_text:
            help_text = Services.docstring_help(help_text)
        cli_params = svc_info.get("cli_params") if svc_info else None

        title_arg = next(
            (p for p in cli_params or [] if p.get("kind") == "argument" and p.get("name") == "title"), None
        )

        @click.command(name=tag, short_help=short_help, help=help_text)
        @click.argument("title", type=str, required=bool(title_arg.get("required", True)) if title_arg else True)
        @click.pass_context
        def remote_cli(ctx: click.Context, title: str, **kwargs: object) -> object:
            from unshackle.core.remote_service import RemoteService, resolve_server

            server_name = ctx.parent.params.get("server") if ctx.parent else None
            server_url, api_key, services_config = resolve_server(server_name)
            services_config["_server_accounts"] = svc_info.get("server_accounts") if svc_info else None
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
                        choices = param.get("choices")
                        kwargs["type"] = click.Choice(choices, case_sensitive=False) if choices else str
                        if param.get("multiple"):
                            kwargs["multiple"] = True
                    if param.get("help"):
                        kwargs["help"] = param["help"]
                    remote_cli = click.option(*opts, **kwargs)(remote_cli)

        return remote_cli

    @staticmethod
    def make_import_command(tag: str, ctx: click.Context) -> click.Command:
        """Make a synthetic command that yields an ImportService from an export JSON.

        Mirrors how unshackle wires remote services, so dl.py's result() runs unchanged.
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
    def get_tags() -> list[str]:
        """Returns a list of service tags from all available Services."""
        return [x.parent.stem for x in SERVICES]

    @staticmethod
    def get_path(name: str) -> Path:
        """Get the directory path of a command."""
        tag = Services.get_tag(name)

        for service in SERVICES:
            if service.parent.stem == tag:
                return service.parent
        raise KeyError(f"There is no Service added by the Tag '{name}'")

    @staticmethod
    def get_tag(value: str) -> str:
        """
        Get the Service Tag (the name of the service's directory, not one of its aliases) by an Alias.
        Input value can be of any case-sensitivity, and so can the alias it matches.
        A real tag always wins over an alias: services do declare aliases that collide with another service's tag.
        This method returns the original input value if it does not match a service tag.
        """
        value_lower = value.lower()
        tags = [path.parent.stem for path in SERVICES]

        for tag in tags:
            if value_lower == tag.lower():
                return tag

        for tag in tags:
            if any(value_lower == alias.lower() for alias in ALIASES.get(tag, ())):
                return tag

        return value

    @staticmethod
    def load(tag: str) -> Service:
        """Load a Service module by Service tag."""
        module = MODULES.get(tag)
        if module:
            return module

        raise KeyError(f"There is no Service added by the Tag '{tag}'")

    @staticmethod
    def get_vault_tag(name: str) -> str:
        """Find the key-vault namespace tag for a service.

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
