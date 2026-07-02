import inspect
import logging
import time
from typing import Any, Iterator, Optional, Union
from uuid import UUID

from unshackle.core.config import config
from unshackle.core.utilities import get_debug_logger, import_module_by_path
from unshackle.core.vault import Vault

log = logging.getLogger(__name__)

_VAULTS = sorted(
    (path for path in config.directories.vaults.glob("*.py") if path.stem.lower() != "__init__"), key=lambda x: x.stem
)

_MODULES = {path.stem: getattr(import_module_by_path(path), path.stem) for path in _VAULTS}


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
        module = _MODULES.get(type_)
        if not module:
            raise ValueError(f"Unable to find vault command by the name '{type_}'.")
        # only pass the global default to vaults that declare a timeout param (per-vault config still wins)
        if "timeout" not in kwargs and "timeout" in inspect.signature(module).parameters:
            kwargs["timeout"] = config.vault_timeout
        try:
            vault = module(**kwargs)
            self.vaults.append(vault)
            return True
        except Exception:
            return False

    def load_critical(self, type_: str, **kwargs: Any) -> None:
        """Load a critical Vault that must succeed or raise an exception."""
        module = _MODULES.get(type_)
        if not module:
            raise ValueError(f"Unable to find vault command by the name '{type_}'.")
        vault = module(**kwargs)
        self.vaults.append(vault)

    def get_key(self, kid: Union[UUID, str]) -> tuple[Optional[str], Optional[Vault]]:
        """Get Key from the first Vault it can by KID (Key ID) and Service."""
        dl = get_debug_logger()
        for vault in self.vaults:
            start = time.monotonic()
            try:
                key = vault.get_key(kid, self.service)
            except (PermissionError, NotImplementedError):
                continue
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
                continue
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
            if found:
                return key, vault
        return None, None

    def add_key(self, kid: Union[UUID, str], key: str, excluding: Optional[Vault] = None) -> int:
        """Add a KID:KEY to all Vaults, optionally with an exclusion."""
        dl = get_debug_logger()
        success = 0
        for vault in self.vaults:
            if vault != excluding and not vault.no_push:
                try:
                    added = vault.add_key(self.service, kid, key)
                    success += added
                    if dl:
                        dl.log_vault_query(vault.name, "add_key", kid=str(kid), success=bool(added))
                except (PermissionError, NotImplementedError):
                    pass
                except Exception as e:
                    log.warning(f"Failed to add key to Vault '{vault.name}': {e}")
                    if dl:
                        dl.log_vault_query(vault.name, "add_key", kid=str(kid), success=False, error=e)
        return success

    def add_keys(self, kid_keys: dict[Union[UUID, str], str]) -> int:
        """
        Add multiple KID:KEYs to all Vaults. Duplicate Content Keys are skipped.
        PermissionErrors when the user cannot create Tables are absorbed and ignored.
        Vaults with no_push=True are skipped.
        """
        success = 0
        for vault in self.vaults:
            if not vault.no_push:
                try:
                    # Count each vault that successfully processes the keys (whether new or existing)
                    vault.add_keys(self.service, kid_keys)
                    success += 1
                except (PermissionError, NotImplementedError):
                    pass
                except Exception as e:
                    log.warning(f"Failed to add keys to Vault '{vault.name}': {e}")
        return success


__all__ = ("Vaults",)
