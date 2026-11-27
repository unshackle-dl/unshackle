import inspect
import logging
import time
from collections.abc import Collection, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from functools import partial
from threading import Lock
from typing import Any, Protocol, cast
from uuid import UUID

from unshackle.core.config import config
from unshackle.core.utilities import get_debug_logger, import_module_by_path
from unshackle.core.vault import UNREACHABLE, RateLimited, Vault, brief, is_null_key

log = logging.getLogger(__name__)

VAULTS = sorted(
    (path for path in config.directories.vaults.glob("*.py") if path.stem.lower() != "__init__"), key=lambda x: x.stem
)

MODULES = {path.stem: getattr(import_module_by_path(path), path.stem) for path in VAULTS}


class KeySource(Protocol):
    """Where a content key came from: a loaded vault, or the server's vaults over --remote."""

    name: str
    local: bool
    report_now: bool

    def flag_bad_key(self, service: str, kid: UUID | str, key: str, source: str) -> None: ...


class Vaults:
    """The key vaults of a run, and the ledger of where each content key in use came from.

    A source offers a content key (``get_key``, ``offer``, ``supplied``). A decode then proves it
    (``proven``) or proves it wrong (``flag_bad_key``), or a CDM licenses it (``licensed``). From these
    events the ledger decides which vault hears of a bad pair, and when.
    """

    def __init__(self, service: str | None = None) -> None:
        self.service = service or ""
        self.vaults: list[Vault] = []
        self.sources: dict[UUID | str, tuple[str, KeySource]] = {}
        self.flagged: set[tuple[UUID | str, str]] = set()
        self.candidates: dict[UUID | str, list[tuple[str, KeySource]]] = {}
        self.warned: set[tuple[str, UUID | str, str]] = set()
        self.failed: set[tuple[str, str]] = set()
        self.unreachable: set[Vault] = set()
        self.unreported: dict[tuple[UUID | str, str], KeySource] = {}
        self.lock = Lock()

    def __iter__(self) -> Iterator[Vault]:
        return iter(self.vaults)

    def __len__(self) -> int:
        return len(self.vaults)

    def load(self, type_: str, **kwargs: Any) -> None:
        """Load a Vault into the vaults list, or raise when it cannot be built."""
        module = MODULES.get(type_)
        if not module:
            raise ValueError(f"Unable to find vault command by the name '{type_}'.")
        # only pass the global default to vaults that declare a timeout param (per-vault config still wins)
        if "timeout" not in kwargs and "timeout" in inspect.signature(module).parameters:
            kwargs["timeout"] = config.vault_timeout
        self.vaults.append(module(**kwargs))

    def get_key(self, kid: UUID | str) -> tuple[str | None, KeySource | None]:
        """Get Key from the first Vault it can by KID (Key ID) and Service.

        Local vaults go first, one at a time: a hit there costs nothing and skips the network.
        A remote answer kept from an earlier call comes next, so a retry after a flagged pair
        reuses it instead of querying again. The remaining vaults then run at the same time,
        and the first one with the content key wins.
        """
        local = [v for v in self.vaults if v.local]
        remote = [v for v in self.live() if not v.local]
        for vault in local:
            key = self.query_vault(vault, kid)
            if key:
                return self.supplied(kid, key, vault)
        with self.lock:
            candidates = list(self.candidates.get(kid, []))
        for key, source in candidates:
            if not self.is_flagged(kid, key):
                return self.supplied(kid, key, source)
        if not remote:
            return None, None
        pool = ThreadPoolExecutor(len(remote))
        try:
            futures = {pool.submit(self.query_vault, vault, kid): vault for vault in remote}
            for future, vault in futures.items():
                future.add_done_callback(partial(self.remember_candidate, kid, vault))
            for future in as_completed(futures):
                key = future.result()
                if key:
                    return self.supplied(kid, key, futures[future])
            return None, None
        finally:
            # a slow vault must not hold up a content key another vault already returned; the
            # ones already running still finish and land in candidates through the callback
            pool.shutdown(wait=False, cancel_futures=True)

    def remember_candidate(self, kid: UUID | str, vault: Vault, future: Future) -> None:
        """Keep a remote answer for a later retry. Cancelled or failed queries leave nothing."""
        if future.cancelled() or future.exception():
            return
        key = future.result()
        if key:
            self.offer(kid, key, vault)

    def offer(self, kid: UUID | str, key: str, source: KeySource) -> None:
        """Keep a content key ``source`` offered for ``kid``; ``get_key`` gives it after the local vaults miss."""
        with self.lock:
            offered = self.candidates.setdefault(kid, [])
            if (key, source) not in offered:
                offered.append((key, source))

    def supplied(self, kid: UUID | str, key: str, source: KeySource) -> tuple[str, KeySource]:
        """Record that the content key in use for ``kid`` came from ``source``, which a failed decode then tells."""
        with self.lock:
            self.sources[kid] = (key, source)
        return key, source

    def source_of(self, kid: UUID | str, key: str) -> KeySource | None:
        """The source that supplied ``key`` for ``kid``, or None when no vault or server vault did."""
        with self.lock:
            source = self.sources.get(kid)
        return source[1] if source and source[0] == key else None

    def proved_wrong(self, kid: UUID | str, key: str) -> bool:
        """True when a decode in this run proved the pair wrong and no licence has confirmed it since."""
        with self.lock:
            return (kid, key) in self.flagged

    def is_flagged(self, kid: UUID | str, key: str) -> bool:
        """True when a local vault has flagged the KID:KEY as wrong."""
        return any(v.is_bad_key(kid, key) for v in self.vaults if v.local)

    def query_vault(self, vault: Vault, kid: UUID | str) -> str | None:
        """Ask one vault for a content key. A failure logs a warning and returns None.

        A flagged pair counts as no content key, so no vault can serve one again.
        """
        dl = get_debug_logger()
        start = time.monotonic()
        try:
            key = vault.get_key(kid, self.service)
        except (PermissionError, NotImplementedError):
            return None
        except Exception as e:
            self.warn_failure(vault, "get key from", e)
            self.mark_unreachable(vault, e)
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
        found = not is_null_key(key)
        if found and key and self.is_flagged(kid, key):
            found = False
            if self.first((vault.name, kid, key), self.warned):
                log.warning(f"{key} from {vault.name} is flagged bad, skipping")
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

    def flag_bad_key(self, kid: UUID | str, key: str) -> None:
        """Flag a KID:KEY in every local vault. A failed decode is no proof, so the remote vault that
        served it hears of it from ``proven``, once another key replaced the pair, or at once when it sets ``report_now``."""
        with self.lock:
            self.flagged.add((kid, key))
            source = self.sources.pop(kid, None)
            remote = source[1] if source and not source[1].local else None
            if remote and not remote.report_now:
                self.unreported[kid, key] = remote
        name = source[1].name if source else "unknown"
        for vault in self.live():
            if vault.local:
                self.tell(vault, kid, key, name)
        if remote and remote.report_now:
            self.tell(remote, kid, key, name)

    def proven(self, content_keys: dict[UUID | str, str], checked: dict[UUID, KeySource]) -> None:
        """A track decrypted with ``content_keys``, and a decode check passed the content key of each KID in ``checked``.

        Each checked pair goes to the vaults other than the source ``checked`` names, unless a decode
        proved it wrong since. Each remote vault whose flagged pair ``content_keys`` replaced with
        another key now hears of it.
        """
        for proven_kid, source in checked.items():
            if not self.proved_wrong(proven_kid, content_keys[proven_kid]):
                self.add_key(proven_kid, content_keys[proven_kid], excluding=source)
        live = self.live()
        with self.lock:
            due = [
                (kid, key, vault)
                for (kid, key), vault in self.unreported.items()
                if content_keys.get(kid) not in (None, key)
            ]
            for kid, key, _ in due:
                del self.unreported[kid, key]
        for kid, key, vault in due:
            if vault in live:
                self.tell(vault, kid, key, vault.name)

    def tell(self, vault: KeySource, kid: UUID | str, key: str, source: str) -> None:
        """Flag one pair in one vault; a failure is only a debug line."""
        try:
            vault.flag_bad_key(self.service, kid, key, source)
        except Exception as e:
            log.debug(f"Could not flag {key} as bad in Vault '{vault.name}': {e!r}")

    def clear_licensed_flags(self, content_keys: dict[UUID, str]) -> list[UUID]:
        """Unflag each pair a licence returned, since the decode check was wrong. Returns their KIDs."""
        cleared = [kid for kid, key in content_keys.items() if self.proved_wrong(kid, key) or self.is_flagged(kid, key)]
        for kid in cleared:
            self.unflag_bad_key(kid, content_keys[kid])
        return cleared

    def licensed(self, content_keys: dict[UUID, str]) -> int:
        """A CDM licensed ``content_keys``: unflag each flagged pair, then store them all. Returns the number of vaults that stored them."""
        self.clear_licensed_flags(content_keys)
        return self.add_keys(cast(dict[UUID | str, str], content_keys))

    def unflag_bad_key(self, kid: UUID | str, key: str) -> None:
        """Remove a flag from every Vault."""
        with self.lock:
            self.flagged.discard((kid, key))
            self.unreported.pop((kid, key), None)
        for vault in self.live():
            try:
                vault.unflag_bad_key(kid, key)
            except Exception as e:
                log.debug(f"Could not unflag {key} in Vault '{vault.name}': {e!r}")

    def add_key(self, kid: UUID | str, key: str, excluding: KeySource | None = None) -> int:
        """Add a KID:KEY to all Vaults, optionally with an exclusion.

        This method pushes to all Vaults at the same time, so one unreachable Vault costs only its
        own timeout and does not delay the others.
        """
        vaults = [v for v in self.live() if v != excluding and not v.no_push]
        if not vaults:
            return 0
        with ThreadPoolExecutor(len(vaults), thread_name_prefix="vault-push") as pool:
            return sum(pool.map(lambda vault: self.push_key(vault, kid, key), vaults))

    def push_key(self, vault: Vault, kid: UUID | str, key: str) -> int:
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
            self.warn_failure(vault, "add key to", e)
            self.mark_unreachable(vault, e)
            if dl:
                dl.log_vault_query(vault.name, "add_key", kid=str(kid), success=False, error=e)
            return 0

    def add_keys(self, kid_keys: dict[UUID | str, str]) -> int:
        """
        Add multiple KID:KEYs to all Vaults. The Vaults skip duplicate Content Keys.
        This method absorbs and ignores the PermissionError a Vault raises when it cannot make Tables.
        This method also skips Vaults with no_push=True.

        This method pushes to all Vaults at the same time, so one unreachable Vault costs only its
        own timeout and does not delay the others. This method drops null keys, because a Vault
        rejects the whole batch when it contains one.
        """
        kid_keys = {kid: key for kid, key in kid_keys.items() if not is_null_key(key)}
        vaults = [vault for vault in self.live() if not vault.no_push]
        if not vaults or not kid_keys:
            return 0
        with ThreadPoolExecutor(len(vaults), thread_name_prefix="vault-push") as pool:
            return sum(pool.map(lambda vault: self.push_keys(vault, kid_keys), vaults))

    def push_keys(self, vault: Vault, kid_keys: dict[UUID | str, str]) -> int:
        """Push Content Keys to one Vault. Returns 1 if the Vault accepted them, 0 on any failure."""
        try:
            vault.add_keys(self.service, kid_keys)
            return 1
        except (PermissionError, NotImplementedError):
            return 0
        except Exception as e:
            self.warn_failure(vault, "add keys to", e)
            self.mark_unreachable(vault, e)
            return 0

    def live(self) -> list[Vault]:
        """The vaults this run still uses."""
        return [vault for vault in self.vaults if vault not in self.unreachable]

    def held_locally(self, kid: UUID | str, key: str) -> bool:
        """True when a local vault already holds this KID:KEY."""
        return any(self.query_vault(vault, kid) == key for vault in self.vaults if vault.local)

    def warn_failure(self, vault: Vault, action: str, error: Exception) -> None:
        """Warn once per vault for each distinct failure; a repeat goes to the debug log."""
        text = brief(error)
        message = f"Failed to {action} Vault '{vault.name}': {text}"
        if self.first((vault.name, text), self.failed):
            log.warning(message)
        else:
            log.debug(message)

    def mark_unreachable(self, vault: Vault, error: Exception) -> None:
        """Skip a vault that is down or still rate limited for the rest of the run, so it costs one
        timeout, not one per KID."""
        if isinstance(error, (RateLimited, *UNREACHABLE)) and self.first(vault, self.unreachable):
            log.warning(f"Skipping Vault '{vault.name}' for the rest of this run")

    def first(self, item: Any, seen: set) -> bool:
        """Add ``item`` to ``seen``; True only for the thread that added it first."""
        with self.lock:
            if item in seen:
                return False
            seen.add(item)
            return True


__all__ = ("KeySource", "Vaults", "load_config_vaults")


def load_config_vaults(
    service: str | None = None,
    configs: Iterable[dict[str, Any]] | None = None,
    critical: Collection[str] = (),
) -> tuple[Vaults, dict[str, BaseException]]:
    """Load vault configs (the config's ``key_vaults`` by default) into one Vaults for a service.

    A vault whose lower-case type is in ``critical`` raises when it fails to load; any other vault
    that fails is left out and returned by name with its error. A DecryptLabs API vault with no
    token of its own takes the global ``decrypt_labs_api_key``.
    """
    vaults = Vaults(service)
    failed: dict[str, BaseException] = {}
    for vault_config in config.key_vaults if configs is None else configs:
        cfg = dict(vault_config)
        type_ = str(cfg.pop("type", ""))
        name = str(cfg.get("name") or type_)
        if type_.lower() == "api" and "decrypt_labs" in name.lower() and not cfg.get("token"):
            if config.decrypt_labs_api_key:
                cfg["token"] = config.decrypt_labs_api_key
            else:
                log.warning(f"No token for DecryptLabs vault '{name}' and no global decrypt_labs_api_key")
        try:
            vaults.load(type_, **cfg)
        except (Exception, SystemExit) as e:
            if type_.lower() in critical:
                log.error(f"vault failure: {name} ({type_}) - {e}")
                raise
            failed[name] = e
            log.debug(f"Failed to load vault: {name} ({type_}): {e!r}")
        else:
            log.debug(f"Successfully loaded vault: {name} ({type_})")
    return vaults, failed
