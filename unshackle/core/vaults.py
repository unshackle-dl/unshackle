import logging
from typing import Any, Iterator, Optional, Union
from uuid import UUID

from unshackle.core.config import config
from unshackle.core.utilities import import_module_by_path
from unshackle.core.vault import Vault

_VAULTS = sorted(
    (path for path in config.directories.vaults.glob("*.py") if path.stem.lower() != "__init__"), key=lambda x: x.stem
)

_MODULES = {path.stem: getattr(import_module_by_path(path), path.stem) for path in _VAULTS}


class Vaults:
    """Keeps hold of Key Vaults with convenience functions, e.g. searching all vaults."""

    def __init__(self, service: Optional[str] = None):
        self.service = service or ""
        self.vaults = []
        self.log = logging.getLogger("vaults")

    def __iter__(self) -> Iterator[Vault]:
        return iter(self.vaults)

    def __len__(self) -> int:
        return len(self.vaults)

    def load(self, type_: str, **kwargs: Any) -> bool:
        """Load a Vault into the vaults list. Returns True if successful, False otherwise."""
        module = _MODULES.get(type_)
        if not module:
            raise ValueError(f"Unable to find vault command by the name '{type_}'.")
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
        for vault in self.vaults:
            try:
                key = vault.get_key(kid, self.service)
                if key and key.count("0") != len(key):
                    return key, vault
            except (PermissionError, NotImplementedError):
                pass
            except Exception:
                self.log.warning(f"Failed to get key from Vault '{vault.name}'")
                continue
        return None, None

    def add_key(self, kid: Union[UUID, str], key: str, excluding: Optional[Vault] = None) -> int:
        """Add a KID:KEY to all Vaults, optionally with an exclusion."""
        success = 0
        for vault in self.vaults:
            if vault != excluding and not vault.no_push:
                try:
                    success += vault.add_key(self.service, kid, key)
                except (PermissionError, NotImplementedError):
                    pass
                except Exception:
                    self.log.warning(f"Failed to add key to Vault '{vault.name}'")
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
                    vault.add_keys(self.service, kid_keys)
                    success += 1
                except (PermissionError, NotImplementedError):
                    pass
                except Exception:
                    self.log.warning(f"Failed to add keys to Vault '{vault.name}'")
        return success
