"""What one API key may do on this server, read from its ``serve.users`` entry."""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from unshackle.core.cdm.loader import device_system
from unshackle.core.config import config
from unshackle.core.launch import load_service_config
from unshackle.core.services import Services
from unshackle.core.utils.collections import ci_get, merge_dict

log = logging.getLogger("api")

Grant = Literal["server_cdm", "server_vault", "server_accounts", "server_proxy"]


def _serve(serve: dict[str, Any] | None) -> dict[str, Any]:
    return (config.serve if serve is None else serve) or {}


def users_table(serve: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ``serve.users`` mapping of ``serve`` (the live config when None); empty when it is not a mapping."""
    users = _serve(serve).get("users")
    return users if isinstance(users, dict) else {}


def api_keys(serve: dict[str, Any] | None = None) -> list[str]:
    """Every API key that has a ``serve.users`` entry."""
    return [str(key) for key in users_table(serve)]


def named_services(serve: dict[str, Any] | None = None) -> list[str]:
    """The service tags the config names: ``serve.services``, else every key's ``services`` list together.

    An API key with no ``services`` list adds nothing, and the function skips a malformed entry.
    """
    serve = _serve(serve)
    if serve.get("services"):
        return list({Services.get_tag(s) for s in serve["services"]})
    lists = [entry.get("services") for entry in users_table(serve).values() if isinstance(entry, dict)]
    return [Services.get_tag(s) for names in lists if isinstance(names, list) for s in names if isinstance(s, str)]


def is_whole_number(value: Any) -> bool:
    """True for a non-negative int that is not a bool."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def rate_limit_error(where: str, value: Any) -> str | None:
    """Why *value* cannot serve as a rate limit, or None when it can. Unset is fine.

    :attr:`KeyPolicy.rate_limit` reads anything else as "no limit", so a mistyped value would silently remove the cap.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return f"{where}.rate_limit must be a positive whole number of requests per hour, not {value!r}"
    return None


class KeyCdmConfig:
    """The app config as one API key sees it: ``cdm`` is the key's map, every other setting is the server's."""

    def __init__(self, cdm: dict[str, Any]) -> None:
        self.cdm = cdm

    def __getattr__(self, name: str) -> Any:
        return getattr(config, name)


@dataclass(frozen=True)
class KeyPolicy:
    """What one API key may do, built from its ``serve.users`` entry and the ``serve`` block around it.

    ``entry`` is None for an API key with no entry: the ``api_secret``, any caller in no-key mode, and a call with
    no request. Such an API key is admin and holds every grant except ``server_proxy``, which only an entry gives.
    """

    entry: dict[str, Any] | None = None
    serve: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def of(cls, api_key: str | None, serve: dict[str, Any] | None = None) -> KeyPolicy:
        """The policy of ``api_key`` in ``serve`` (the live config when None); a malformed entry grants nothing."""
        serve = _serve(serve)
        table = users_table(serve)
        if not api_key or api_key not in table:
            return cls(None, serve)
        entry = table[api_key]
        return cls(entry if isinstance(entry, dict) else {}, serve)

    @property
    def _entry(self) -> dict[str, Any]:
        return self.entry or {}

    @property
    def configured(self) -> bool:
        """Whether the API key has a ``serve.users`` entry."""
        return self.entry is not None

    @property
    def admin(self) -> bool:
        """Whether the API key may run server maintenance and see admin-only fields."""
        return not self.configured or self._entry.get("admin") is True

    @property
    def services(self) -> list[str] | None:
        """The services the API key may use: ``serve.services`` and its own ``services`` together; None for all."""
        sets = [
            {Services.get_tag(s) for s in names}
            for names in (self.serve.get("services"), self._entry.get("services"))
            if names
        ]
        return list(set.intersection(*sets)) if sets else None

    def grant(self, name: Grant) -> bool | list[str]:
        """The ``name`` setting as enforced: True for every service, False, or the service tags it covers.

        ``server_proxy`` is a plain bool that only a real yaml ``true`` on an entry sets.
        """
        if name == "server_proxy":
            return self.configured and self._entry.get(name) is True
        if not self.configured:
            return True
        value = self._entry.get(name, False)
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return list(value)
        return bool(value)

    def allows(self, name: Grant, service: str | None = None) -> bool:
        """Whether the API key holds ``name`` for ``service``; ``server_cdm`` gives ``server_vault`` too."""
        if name == "server_vault" and self.allows("server_cdm", service):
            return True
        value = self.grant(name)
        if isinstance(value, list):
            return service is not None and Services.get_tag(service).upper() in {
                Services.get_tag(s).upper() for s in value
            }
        return value

    @property
    def max_height_setting(self) -> Any:
        """``server_cdm_max_height`` as configured: one height, a map of service tag to height, or None."""
        return self._entry.get("server_cdm_max_height")

    def max_height(self, service: str | None) -> int | None:
        """The tallest video track the server CDM licenses live for ``service``; None for no cap.

        In a map, ``default`` covers a missing or null tag.
        """
        cap = self.max_height_setting
        if isinstance(cap, dict):
            own = ci_get(cap, Services.get_tag(service)) if service else None
            cap = own if own is not None else ci_get(cap, "default")
        return cap if is_whole_number(cap) else None

    def devices(self, kind: Literal["devices", "playready_devices"]) -> list[str]:
        """The API key's device names of ``kind``; every device the server loaded for an API key with no entry."""
        if self.configured:
            return list(self._entry.get(kind) or [])
        return [d.stem if hasattr(d, "stem") else str(d) for d in (self.serve.get(kind) or [])]

    @property
    def _own_cdm(self) -> dict[str, Any]:
        own = self._entry.get("cdm")
        return own if isinstance(own, dict) else {}

    def cdm_config(self) -> Any:
        """The config the CDM lookups read: ``config`` itself unless the API key maps a device of its own.

        Each service entry of the key's own ``cdm`` replaces the server's whole entry for that service.
        """
        own = self._own_cdm
        if not own:
            return config
        tags = {str(tag).lower() for tag in own}
        return KeyCdmConfig({**{t: e for t, e in (config.cdm or {}).items() if str(t).lower() not in tags}, **own})

    def service_config(self, service: str) -> dict[str, Any]:
        """The key's own ``service_config`` block for ``service``; empty when it sets none."""
        blocks = self._entry.get("service_config")
        block = ci_get(blocks, Services.get_tag(service)) if isinstance(blocks, dict) else None
        return block if isinstance(block, dict) else {}

    def owns_identity(self, service: str) -> bool:
        """Whether the API key sets its own device or service config for ``service``."""
        return bool(self.service_config(service)) or ci_get(self._own_cdm, service) is not None

    def service_yaml(self, service: str) -> dict:
        """:func:`load_service_config` with the key's own block merged over it.

        A top-level section that sets one of the block's plain values (a profile section with its own identity) takes
        the key's value too, so a client cannot pick the server's identity back through the profile it names.
        """
        service_config = load_service_config(service)
        block = self.service_config(service)
        if block:
            merge_dict(copy.deepcopy(block), service_config)
            scalars = {key for key, value in block.items() if not isinstance(value, (dict, list))}
            for name, section in service_config.items():
                if isinstance(section, dict) and name not in block:
                    for key in scalars & section.keys():
                        section[key] = block[key]
        return service_config

    @property
    def username(self) -> str | None:
        """The entry's ``username``, when it sets one."""
        name = self._entry.get("username")
        return str(name) if name else None

    @property
    def shared(self) -> bool:
        """Whether a gateway shares the API key, so its ``X-Client-Id`` header names the person behind each call."""
        return self._entry.get("shared") is True

    @property
    def tier(self) -> str | None:
        """The tier name the entry references."""
        tier = self._entry.get("tier")
        return str(tier) if tier else None

    @property
    def rate_limit(self) -> int | None:
        """Requests per hour: the entry's own ``rate_limit``, else its tier's, else None for no limit."""
        own = self._entry.get("rate_limit")
        if isinstance(own, int) and own > 0:
            return own
        tiers = self.serve.get("tiers")
        tier = tiers.get(self._entry["tier"]) if isinstance(tiers, dict) and self._entry.get("tier") else None
        limit = tier.get("rate_limit") if isinstance(tier, dict) else None
        return limit if isinstance(limit, int) and limit > 0 else None

    def entry_error(self, where: str, app_config: Any = None) -> str | None:
        """Why the entry cannot work, or None when it can; serve runs it on load and on reload.

        Devices are looked up in ``app_config`` (a reload passes the new file).
        """
        entry = self._entry
        tier = entry.get("tier")
        if tier and tier not in (self.serve.get("tiers") or {}):
            return f"{where}.tier: no such tier '{tier}' under serve.tiers"
        return (
            rate_limit_error(where, entry.get("rate_limit"))
            or _max_height_error(where, entry.get("server_cdm_max_height"))
            or _identity_error(where, entry, app_config or config)
        )


def _max_height_error(where: str, value: Any) -> str | None:
    """Why *value* cannot serve as ``server_cdm_max_height``, or None when it can (unset included)."""

    def bad(height: Any) -> bool:
        return height is not None and not is_whole_number(height)

    if isinstance(value, dict):
        if any(bad(height) for height in value.values()):
            return f"{where}.server_cdm_max_height values must be whole numbers of pixels, not {value!r}"
        return None
    if bad(value):
        return (
            f"{where}.server_cdm_max_height must be a whole number of pixels or a map of service to one, not {value!r}"
        )
    return None


def _identity_error(where: str, entry: dict[str, Any], app_config: Any) -> str | None:
    """Why the ``services``, ``cdm`` or ``service_config`` of an entry cannot work; None when it can.

    A tag that names no service the server has is logged, not refused: a later services refresh can add it.
    """
    if isinstance(entry.get("services"), dict):
        return f"{where}.services must be a list of service tags; per-service settings go under service_config"
    cdm, service_config = entry.get("cdm") or {}, entry.get("service_config") or {}
    for name, entries in (("cdm", cdm), ("service_config", service_config)):
        if not isinstance(entries, dict):
            return f"{where}.{name} must be a mapping of service tag to settings"
        if any(str(tag).lower() == "default" for tag in entries):
            return f"{where}.{name}.default: name each service tag; a key has no default entry"
    for tag, value in cdm.items():
        named = {"default": value} if isinstance(value, str) else value if isinstance(value, dict) else {}
        named = {str(system).lower(): device for system, device in named.items()}
        if (
            not named
            or not set(named) <= {"widevine", "playready", "default"}
            or not all(isinstance(device, str) for device in named.values())
        ):
            return f"{where}.cdm.{tag} must be a device name or a widevine/playready/default mapping of device names"
        for system, device in named.items():
            found = device_system(device, app_config)
            if found is None or system != "default" and found != system:
                return f"{where}.cdm.{tag}: no {'CDM' if system == 'default' else system} device named '{device}'"
    for tag, value in service_config.items():
        if not isinstance(value, dict):
            return f"{where}.service_config.{tag} must be a mapping of config key to value"
    if not cdm and not service_config:
        return None
    known = {tag.lower() for tag in Services.get_tags()}
    for tag in [*cdm, *service_config]:
        if str(tag).lower() not in known:
            log.warning(f"{where}: no service {tag} on this server")
    return None
