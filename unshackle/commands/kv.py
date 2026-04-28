import logging
import re
from pathlib import Path
from typing import Optional

import click
from rich.padding import Padding
from rich.text import Text
from rich.tree import Tree

from unshackle.core.config import config
from unshackle.core.console import console
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


def copy_service_data(to_vault: Vault, from_vault: Vault, service: str, log: logging.Logger) -> int:
    """Copy data for a single service between vaults."""
    content_keys = process_service_keys(from_vault, service, log)
    total_count = len(content_keys)

    if total_count == 0:
        log.info(f"{service}: No keys found in {from_vault}")
        return 0

    try:
        added = to_vault.add_keys(service, content_keys)
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
def copy(to_vault_name: str, from_vault_names: list[str], service: Optional[str] = None) -> None:
    """
    Copy data from multiple Key Vaults into a single Key Vault.
    Rows with matching KIDs are skipped unless there's no KEY set.
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

    if service:
        service = Services.get_tag(service)
        log.info(f"Filtering by service: {service}")

    total_added = 0
    for from_vault in from_vaults:
        services_to_copy = [service] if service else from_vault.get_services()

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
@click.pass_context
def sync(ctx: click.Context, vaults: list[str], service: Optional[str] = None) -> None:
    """
    Ensure multiple Key Vaults copies of all keys as each other.
    It's essentially just a bi-way copy between each vault.
    To see the precise details of what it's doing between each
    provided vault, see the documentation for the `copy` command.
    """
    if not len(vaults) > 1:
        raise click.ClickException("You must provide more than one Vault to sync.")

    ctx.invoke(copy, to_vault_name=vaults[0], from_vault_names=vaults[1:], service=service)
    for i in range(1, len(vaults)):
        ctx.invoke(copy, to_vault_name=vaults[i], from_vault_names=[vaults[i - 1]], service=service)


@kv.command()
@click.argument("file", type=Path)
@click.argument("service", type=str)
@click.argument("vaults", nargs=-1, type=click.UNPROCESSED)
def add(file: Path, service: str, vaults: list[str]) -> None:
    """
    Add new Content Keys to Key Vault(s) by service.

    File should contain one key per line in the format KID:KEY (HEX:HEX).
    Each line should have nothing else within it except for the KID:KEY.
    Encoding is presumed to be UTF8.
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
    service = Services.get_tag(service)

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
        added_count = vault.add_keys(service, kid_keys)
        existed_count = total_count - added_count
        log.info(f"{vault}: {added_count} newly added, {existed_count} already existed (skipped)")

    log.info("Done!")


@kv.command()
@click.argument("kid", type=str)
@click.option("-s", "--service", type=str, default=None, help="Limit search to a specific service tag.")
@click.option(
    "-v", "--vault", "vault_name", type=str, default=None, help="Limit search to a specific configured vault by name."
)
def search(kid: str, service: Optional[str], vault_name: Optional[str]) -> None:
    """
    Search configured Key Vault(s) for a KID and report any matching KEY.

    KID must be 32 hex characters (no dashes). If --service is omitted, every
    service table in each vault is scanned. If --vault is omitted, every
    vault in the config is searched.
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

    service_tag = Services.get_tag(service) if service else None

    hit: Optional[tuple[str, str, str]] = None
    for vault in vaults_:
        if service_tag:
            services_to_check: list[str] = [service_tag]
        else:
            try:
                services_to_check = list(vault.get_services())
            except Exception as e:
                log.debug(f"{vault}: get_services() failed ({e})")
                services_to_check = []
            if not services_to_check:
                log.warning(f"{vault}: cannot search without a service (remote vault requires --service). Skipping.")
                continue

        for svc in services_to_check:
            try:
                key = vault.get_key(kid_norm, svc)
            except Exception as e:
                log.debug(f"{vault} [{svc}]: lookup error ({e})")
                continue
            if key and key.count("0") != len(key):
                hit = (vault.name, svc, key)
                break
        if hit:
            break

    if hit:
        vname, svc, key = hit
        tree = Tree(Text.assemble((svc, "cyan"), (f"({vname})", "text"), overflow="fold"))
        tree.add(f"[text2]{kid_norm}:{key}")
        console.print(Padding(tree, (1, 5)))
    else:
        log.info(f"KID {kid_norm} not found in {len(vaults_)} vault(s).")


@kv.command()
@click.argument("vaults", nargs=-1, type=click.UNPROCESSED)
def prepare(vaults: list[str]) -> None:
    """Create Service Tables on Vaults if not yet created."""
    log = logging.getLogger("kv")

    vaults_ = load_vaults(vaults)

    for vault in vaults_:
        if hasattr(vault, "has_table") and hasattr(vault, "create_table"):
            for service_tag in Services.get_tags():
                if vault.has_table(service_tag):
                    log.info(f"{vault} already has a {service_tag} Table")
                else:
                    try:
                        vault.create_table(service_tag, commit=True)
                        log.info(f"{vault}: Created {service_tag} Table")
                    except PermissionError:
                        log.error(f"{vault} user has no create table permission, skipping...")
                        continue
        else:
            log.info(f"{vault} does not use tables, skipping...")

    log.info("Done!")
