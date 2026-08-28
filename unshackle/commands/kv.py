import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Union
from uuid import UUID

import click
from rich.padding import Padding
from rich.progress import (
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text
from rich.tree import Tree

from unshackle.core.config import config
from unshackle.core.console import GradientPulseBarColumn, console
from unshackle.core.constants import context_settings
from unshackle.core.services import Services
from unshackle.core.vault import Vault
from unshackle.core.vaults import Vaults


def load_vaults(vault_names: list[str]) -> Vaults:
    """Load and validate vaults by name."""
    vaults = Vaults()
    for vault_name in vault_names:
        vault_config = next((x for x in config.key_vaults if x["name"] == vault_name), None)
        if not vault_config:
            raise click.ClickException(f"Vault ({vault_name}) is not defined in the config.")

        vault_type = vault_config["type"]
        vault_args = vault_config.copy()
        del vault_args["type"]

        if not vaults.load(vault_type, **vault_args):
            raise click.ClickException(f"Failed to load vault ({vault_name}).")

    return vaults


def process_service_keys(from_vault: Vault, service: str, log: logging.Logger) -> dict[str, str]:
    """Get and validate keys from a vault for a specific service."""
    content_keys = list(from_vault.get_keys(service))

    bad_keys = {kid: key for kid, key in content_keys if not key or key.count("0") == len(key)}
    for kid, key in bad_keys.items():
        log.warning(f"Skipping NULL key: {kid}:{key}")

    return {kid: key for kid, key in content_keys if kid not in bad_keys}


def add_keys_with_progress(vault: Vault, service: str, kid_keys: dict[str, str], log: logging.Logger) -> int:
    """Add content keys to a vault in batches, with a progress bar of counts and time left."""
    chunk = 1 if type(vault).__name__ == "HTTP" and getattr(vault, "api_mode", None) != "json" else 500
    kids = list(kid_keys)
    added = 0
    console.print()
    with Progress(
        SpinnerColumn(finished_text=""),
        TextColumn("[bold]{task.description}"),
        GradientPulseBarColumn(),
        MofNCompleteColumn(),
        "•",
        TextColumn("[green]{task.fields[added]} new"),
        "•",
        TimeElapsedColumn(),
        "•",
        TimeRemainingColumn(compact=True),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"{service} → {vault}", total=len(kids), added=0)
        for i in range(0, len(kids), chunk):
            batch: dict[Union[UUID, str], str] = {kid: kid_keys[kid] for kid in kids[i : i + chunk]}
            added += vault.add_keys(service, batch)
            progress.update(task, advance=len(batch), added=added)
    return added


def copy_service_data(to_vault: Vault, from_vault: Vault, service: str, log: logging.Logger) -> int:
    """Copy data for a single service between vaults."""
    try:
        content_keys = process_service_keys(from_vault, service, log)
    except Exception as e:
        log.warning(f"{service}: Could not read from {from_vault} ({e}), skipped")
        return 0

    total_count = len(content_keys)

    if total_count == 0:
        log.info(f"{service}: No keys found in {from_vault}")
        return 0

    try:
        added = add_keys_with_progress(to_vault, service, content_keys, log)
    except PermissionError:
        log.warning(f"{service}: No permission to create table in {to_vault}, skipped")
        return 0

    existed = total_count - added

    if added > 0 and existed > 0:
        log.info(f"{service}: {added} added, {existed} skipped ({total_count} total)")
    elif added > 0:
        log.info(f"{service}: {added} added ({total_count} total)")
    else:
        log.info(f"{service}: {existed} skipped (all existed)")

    return added


@click.group(short_help="Manage and configure Key Vaults.", context_settings=context_settings)
def kv() -> None:
    """Manage and configure Key Vaults."""


@kv.command()
@click.argument("to_vault_name", type=str)
@click.argument("from_vault_names", nargs=-1, type=click.UNPROCESSED)
@click.option("-s", "--service", type=str, default=None, help="Only copy data to and from a specific service.")
@click.option(
    "-l",
    "--local-only",
    is_flag=True,
    default=False,
    help="Only copy data for services installed locally (skip vault tables for services not present in the configured services path).",
)
def copy(
    to_vault_name: str,
    from_vault_names: list[str],
    service: Optional[str] = None,
    local_only: bool = False,
) -> None:
    """
    Copy data from multiple Key Vaults into a single Key Vault.
    The copy skips rows with matching KIDs, unless the row has no content key.
    Existing data is not deleted or altered.

    The `to_vault_name` argument is the key vault you wish to copy data to.
    It should be the name of a Key Vault defined in the config.

    The `from_vault_names` argument is the key vault(s) you wish to take
    data from. You may supply multiple key vaults.
    """
    if not from_vault_names:
        raise click.ClickException("No Vaults were specified to copy data from.")

    log = logging.getLogger("kv")

    all_vault_names = [to_vault_name] + list(from_vault_names)
    vaults = load_vaults(all_vault_names)

    to_vault = vaults.vaults[0]
    from_vaults = vaults.vaults[1:]

    vault_names = ", ".join([v.name for v in from_vaults])
    log.info(f"Copying data from {vault_names} → {to_vault.name}")

    if service and local_only:
        raise click.UsageError("--service and --local-only are mutually exclusive.")

    if service:
        service = Services.get_vault_tag(service)
        log.info(f"Filtering by service: {service}")

    installed: Optional[set[str]] = None
    if local_only:
        installed = {t.upper() for t in Services.get_tags()}
        log.info(f"Filtering by locally installed services ({len(installed)} found)")

    total_added = 0
    for from_vault in from_vaults:
        if service:
            services_to_copy = [service]
        else:
            try:
                services_to_copy = list(from_vault.get_services())
            except Exception as e:
                log.warning(f"{from_vault.name}: cannot list services ({e}), skipped")
                continue

        if installed is not None:
            before = len(services_to_copy)
            services_to_copy = [s for s in services_to_copy if s and Services.get_tag(s).upper() in installed]
            skipped = before - len(services_to_copy)
            if skipped:
                log.info(f"{from_vault.name}: skipping {skipped} service(s) not installed locally")

        for service_tag in services_to_copy:
            added = copy_service_data(to_vault, from_vault, service_tag, log)
            total_added += added

    if total_added > 0:
        log.info(f"Successfully added {total_added} new keys to {to_vault}")
    else:
        log.info("Copy completed - no new keys to add")


@kv.command()
@click.argument("vaults", nargs=-1, type=click.UNPROCESSED)
@click.option("-s", "--service", type=str, default=None, help="Only sync data to and from a specific service.")
@click.option(
    "-l",
    "--local-only",
    is_flag=True,
    default=False,
    help="Only sync data for services installed locally (skip vault tables for services not present in the configured services path).",
)
@click.pass_context
def sync(
    ctx: click.Context,
    vaults: list[str],
    service: Optional[str] = None,
    local_only: bool = False,
) -> None:
    """
    Make sure that every Key Vault has copies of all the content keys of the others.
    It is essentially a bi-way copy between each vault.
    To see the precise details of what it does between each
    provided vault, see the documentation for the `copy` command.
    """
    if not len(vaults) > 1:
        raise click.ClickException("You must provide more than one Vault to sync.")

    ctx.invoke(
        copy.callback,
        to_vault_name=vaults[0],
        from_vault_names=vaults[1:],
        service=service,
        local_only=local_only,
    )
    for i in range(1, len(vaults)):
        ctx.invoke(
            copy.callback,
            to_vault_name=vaults[i],
            from_vault_names=[vaults[i - 1]],
            service=service,
            local_only=local_only,
        )


@kv.command()
@click.argument("file", type=Path)
@click.argument("service", type=str)
@click.argument("vaults", nargs=-1, type=click.UNPROCESSED)
def add(file: Path, service: str, vaults: list[str]) -> None:
    """
    Add new Content Keys to Key Vault(s) by service.

    File should contain one content key per line in the format KID:KEY (HEX:HEX).
    Each line should have nothing else within it except for the KID:KEY.
    unshackle reads the file as UTF8.
    """
    if not file.exists():
        raise click.ClickException(f"File provided ({file}) does not exist.")
    if not file.is_file():
        raise click.ClickException(f"File provided ({file}) is not a file.")
    if not service or not isinstance(service, str):
        raise click.ClickException(f"Service provided ({service}) is invalid.")
    if len(vaults) < 1:
        raise click.ClickException("You must provide at least one Vault.")

    log = logging.getLogger("kv")
    service = Services.get_vault_tag(service)

    vaults_ = load_vaults(list(vaults))

    data = file.read_text(encoding="utf8")
    kid_keys: dict[str, str] = {}
    for line in data.splitlines(keepends=False):
        line = line.strip()
        match = re.search(r"^(?P<kid>[0-9a-fA-F]{32}):(?P<key>[0-9a-fA-F]{32})$", line)
        if not match:
            continue
        kid = match.group("kid").lower()
        key = match.group("key").lower()
        kid_keys[kid] = key

    total_count = len(kid_keys)

    for vault in vaults_:
        log.info(f"Adding {total_count} Content Keys to {vault}")
        added_count = add_keys_with_progress(vault, service, kid_keys, log)
        existed_count = total_count - added_count
        log.info(f"{vault}: {added_count} newly added, {existed_count} already existed (skipped)")

    log.info("Done!")


def search_vault(vault: Vault, kid: str, services: list[str], log: logging.Logger) -> Optional[tuple[str, str]]:
    """Return the service and content key from the first service table in a vault holding the KID."""

    def probe(svc: str) -> Optional[tuple[str, str]]:
        try:
            key = vault.get_key(kid, svc)
        except Exception as e:
            log.debug(f"{vault} [{svc}]: lookup error ({e})")
            return None
        if key and key.count("0") != len(key):
            return svc, key
        return None

    if len(services) == 1:
        return probe(services[0])

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(probe, svc) for svc in services]
        try:
            for future in as_completed(futures):
                found = future.result()
                if found:
                    return found
        finally:
            for future in futures:
                future.cancel()

    return None


@kv.command()
@click.argument("kid", type=str)
@click.option("-s", "--service", type=str, default=None, help="Limit search to a specific service tag.")
@click.option(
    "-v", "--vault", "vault_name", type=str, default=None, help="Limit search to a specific configured vault by name."
)
def search(kid: str, service: Optional[str], vault_name: Optional[str]) -> None:
    """
    Examine configured Key Vault(s) for a KID and report the content key it finds.

    KID must be 32 hex characters (no dashes). If you do not give --service,
    unshackle scans every service table in each vault. For a vault that cannot
    show its tables, unshackle probes every locally installed service tag
    instead, so --service is much faster. If you do not give --vault, unshackle
    examines every vault in the config.
    """
    log = logging.getLogger("kv")

    kid_norm = kid.replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", kid_norm):
        raise click.ClickException(f"KID '{kid}' is not 32 hex characters.")

    if vault_name:
        vault_names = [vault_name]
    else:
        vault_names = [v["name"] for v in config.key_vaults]
    if not vault_names:
        raise click.ClickException("No Key Vaults are configured.")

    vaults_ = load_vaults(vault_names)

    service_tag = Services.get_vault_tag(service) if service else None

    hits: list[tuple[str, str, str]] = []
    for vault in vaults_:
        probed = False
        if service_tag:
            services_to_check: list[str] = [service_tag]
        else:
            try:
                services_to_check = list(vault.get_services())
            except Exception as e:
                log.debug(f"{vault}: get_services() failed ({e})")
                services_to_check = []
            if not services_to_check:
                services_to_check = list(Services.get_tags())
                probed = True
                log.debug(f"{vault}: cannot list tables, probing {len(services_to_check)} local service tag(s)")

        found = search_vault(vault, kid_norm, services_to_check, log)
        if found:
            svc_hit, key_hit = found
            hits.append((vault.name, f"{svc_hit}?" if probed else svc_hit, key_hit))

    if not hits:
        log.info(f"KID {kid_norm} not found in {len(vaults_)} vault(s).")
        return

    for svc in dict.fromkeys(svc for _, svc, _ in hits):
        tree = Tree(Text.assemble((svc, "cyan"), overflow="fold"))
        for vname, hit_svc, key in hits:
            if hit_svc == svc:
                tree.add(f"[text2]{kid_norm}:{key} from {vname}")
        console.print(Padding(tree, (1, 5)))


@kv.command()
@click.argument("vaults", nargs=-1, type=click.UNPROCESSED)
def prepare(vaults: list[str]) -> None:
    """Make Service Tables on Vaults if they do not exist yet."""
    log = logging.getLogger("kv")

    vaults_ = load_vaults(vaults)

    for vault in vaults_:
        if hasattr(vault, "resolve_table") and hasattr(vault, "create_table"):
            for service_tag in dict.fromkeys(Services.get_vault_tag(tag) for tag in Services.get_tags()):
                existing = vault.resolve_table(service_tag)
                if existing:
                    log.info(f"{vault} already has a {existing} Table")
                else:
                    try:
                        vault.create_table(service_tag)
                        log.info(f"{vault}: Created {service_tag} Table")
                    except PermissionError:
                        log.error(f"{vault} user has no create table permission, skipping...")
                        continue
        else:
            log.info(f"{vault} does not use tables, skipping...")

    log.info("Done!")
