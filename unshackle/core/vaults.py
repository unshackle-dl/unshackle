import inspect
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterator, Optional, Union
from uuid import UUID

from unshackle.core.config import config
from unshackle.core.utilities import get_debug_logger, import_module_by_path
from unshackle.core.vault import Vault

log = logging.getLogger(__name__)

VAULTS = sorted(
    (path for path in config.directories.vaults.glob("*.py") if path.stem.lower() != "__init__"), key=lambda x: x.stem
)

MODULES = {path.stem: getattr(import_module_by_path(path), path.stem) for path in VAULTS}


class Vaults:
    """Keeps hold of Key Vaults with convenience functions, e.g. searching all vaults."""

    def __init__(self, service: Optional[str] = None):
        self.service = service or ""
        self.vaults = []

    def __iter__(self) -> Iterator[Vault]:
        return iter(self.vaults)

    def __len__(self) -> int:
        return len(self.vaults)

    def load(self, type_: str, **kwargs: Any) -> bool:
        """Load a Vault into the vaults list. Returns True if successful, False otherwise."""
        module = MODULES.get(type_)
        if not module:
            raise ValueError(f"Unable to find vault command by the name '{type_}'.")
        # only pass the global default to vaults that declare a timeout param (per-vault config still wins)
        if "timeout" not in kwargs and "timeout" in inspect.signature(module).parameters:
            kwargs["timeout"] = config.vault_timeout
        try:
            vault = module(**kwargs)
            self.vaults.append(vault)
            return True
        # one bad vault config must not stop the others from loading
        except Exception as e:
            log.debug(f"Vault '{type_}' failed to load: {e!r}")
            return False

    def load_critical(self, type_: str, **kwargs: Any) -> None:
        """Load a critical Vault that must succeed or raise an exception."""
        module = MODULES.get(type_)
        if not module:
            raise ValueError(f"Unable to find vault command by the name '{type_}'.")
        vault = module(**kwargs)
        self.vaults.append(vault)

    def get_key(self, kid: Union[UUID, str]) -> tuple[Optional[str], Optional[Vault]]:
        """Get Key from the first Vault it can by KID (Key ID) and Service.

        Local vaults go first, one at a time: a hit there costs nothing and skips the network.
        The remaining vaults run at the same time, and the first one with the content key wins.
        """
        local = [v for v in self.vaults if v.local]
        remote = [v for v in self.vaults if not v.local]
        for vault in local:
            key = self.query_vault(vault, kid)
            if key:
                return key, vault
        if not remote:
            return None, None
        pool = ThreadPoolExecutor(len(remote))
        try:
            futures = {pool.submit(self.query_vault, vault, kid): vault for vault in remote}
            for future in as_completed(futures):
                key = future.result()
                if key:
                    return key, futures[future]
            return None, None
        finally:
            # a slow vault must not hold up a content key another vault already returned
            pool.shutdown(wait=False, cancel_futures=True)

    def query_vault(self, vault: Vault, kid: Union[UUID, str]) -> Optional[str]:
        """Ask one vault for a content key. A failure logs a warning and returns None."""
        dl = get_debug_logger()
        start = time.monotonic()
        try:
            key = vault.get_key(kid, self.service)
        except (PermissionError, NotImplementedError):
            return None
        except Exception as e:
            log.warning(f"Failed to get key from Vault '{vault.name}': {e}")
            if dl:
                dl.log_vault_query(
                    vault.name,
                    "get_key",
                    kid=str(kid),
                    reachable=False,
                    error=e,
                    duration_ms=round((time.monotonic() - start) * 1000, 1),
                )
            return None
        found = bool(key and key.count("0") != len(key))
        if dl:
            dl.log_vault_query(
                vault.name,
                "get_key",
                kid=str(kid),
                key_found=found,
                reachable=True,
                duration_ms=round((time.monotonic() - start) * 1000, 1),
            )
        return key if found else None

    def add_key(self, kid: Union[UUID, str], key: str, excluding: Optional[Vault] = None) -> int:
        """Add a KID:KEY to all Vaults, optionally with an exclusion.

        This method pushes to all Vaults at the same time, so one unreachable Vault costs only its
        own timeout and does not delay the others.
        """
        vaults = [vault for vault in self.vaults if vault != excluding and not vault.no_push]
        if not vaults:
            return 0
        with ThreadPoolExecutor(len(vaults), thread_name_prefix="vault-push") as pool:
            return sum(pool.map(lambda vault: self.push_key(vault, kid, key), vaults))

    def push_key(self, vault: Vault, kid: Union[UUID, str], key: str) -> int:
        """Push one Content Key to one Vault. Returns 1 if the Vault stored it, 0 otherwise."""
        dl = get_debug_logger()
        try:
            added = vault.add_key(self.service, kid, key)
            if dl:
                dl.log_vault_query(vault.name, "add_key", kid=str(kid), success=bool(added))
            return int(added)
        except (PermissionError, NotImplementedError):
            return 0
        except Exception as e:
            log.warning(f"Failed to add key to Vault '{vault.name}': {e}")
            if dl:
                dl.log_vault_query(vault.name, "add_key", kid=str(kid), success=False, error=e)
            return 0

    def add_keys(self, kid_keys: dict[Union[UUID, str], str]) -> int:
        """
        Add multiple KID:KEYs to all Vaults. The Vaults skip duplicate Content Keys.
        This method absorbs and ignores the PermissionError a Vault raises when it cannot make Tables.
        This method also skips Vaults with no_push=True.

        This method pushes to all Vaults at the same time, so one unreachable Vault costs only its
        own timeout and does not delay the others.
        """
        vaults = [vault for vault in self.vaults if not vault.no_push]
        if not vaults:
            return 0
        with ThreadPoolExecutor(len(vaults), thread_name_prefix="vault-push") as pool:
            return sum(pool.map(lambda vault: self.push_keys(vault, kid_keys), vaults))

    def push_keys(self, vault: Vault, kid_keys: dict[Union[UUID, str], str]) -> int:
        """Push Content Keys to one Vault. Returns 1 if the Vault accepted them, 0 on any failure."""
        try:
            vault.add_keys(self.service, kid_keys)
            return 1
        except (PermissionError, NotImplementedError):
            return 0
        except Exception as e:
            log.warning(f"Failed to add keys to Vault '{vault.name}': {e}")
            return 0


__all__ = ("Vaults",)
