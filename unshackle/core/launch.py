"""The launch context of a Service: ``ctx.obj`` and the launch keys in ``ctx.parent.params``."""

import copy
from pathlib import Path
from typing import Any, TypedDict, cast

import click

from unshackle.core.config import config, load_yaml
from unshackle.core.utils.click_types import ContextData
from unshackle.core.utils.collections import ci_get, merge_dict


class LaunchKeys(TypedDict, total=False):
    """The ``ctx.parent.params`` keys that exist only for the launch. ``dl_options.DL_OPTIONS`` holds the dl options.

    - ``served``: a serve runs the service for an API client, so the region it checks is the server's.
    - ``cookies_supplied``: ``authenticate()`` gets a cookie jar that is not in the ``cookies/`` directory.
    - ``client_device``: the service licenses through the remote client's own device.
    - ``client_identity``: the service config holds the device identity that the remote client sent.
    - ``client_argv``: the command line that the remote server's dashboard shows for this client.
    - ``proxy_arg``: the ``--proxy`` value as given, before it resolves to a URI.
    - ``proxy_query``: the region part of ``--proxy``, which a service ``proxy_map`` keys on.
    - ``proxy_provider``: the proxy provider that ``--proxy`` names, or None.
    """

    served: bool
    cookies_supplied: bool
    client_device: bool
    client_identity: bool
    client_argv: str
    proxy_arg: str | None
    proxy_query: str | None
    proxy_provider: str | None


def launch(ctx: click.Context, keys: LaunchKeys, obj: ContextData | None = None) -> None:
    """Write the launch ``keys`` into ``ctx.params``, and ``obj`` as ``ctx.obj`` when given."""
    ctx.params.update(keys)
    if obj is not None:
        ctx.obj = obj


def served_context(obj: ContextData, params: dict[str, Any], keys: LaunchKeys) -> click.Context:
    """A new parent context for a service that a serve runs for an API client, with the dl options in ``params``."""
    ctx = click.Context(click.Command("serve"))
    # dl defaults -q to [], and services iterate it
    ctx.params = {**params, "quality": params.get("quality") or []}
    launch(ctx, {**keys, "served": True}, obj)
    return ctx


SERVICE_YAML: dict[Path, tuple[tuple[int, int], dict]] = {}


def load_service_config(tag: str, local: bool = True) -> dict:
    """The service's ``config.yaml`` with the user's ``services.<TAG>`` block merged over it.

    *local* False leaves out the file, for a service that runs on a remote server. The parsed file is kept
    until its mtime or size changes; each caller gets its own copy to change.
    """
    from unshackle.core.services import Services

    service_config: dict = {}
    if local:
        try:
            path = Services.get_path(tag) / config.filenames.config
            stat = path.stat()
        except (KeyError, FileNotFoundError):
            pass
        else:
            stamp = (stat.st_mtime_ns, stat.st_size)
            hit = SERVICE_YAML.get(path)
            if not hit or hit[0] != stamp:
                hit = SERVICE_YAML[path] = (stamp, load_yaml(path) or {})
            service_config = copy.deepcopy(hit[1])
    merge_dict(copy.deepcopy(ci_get(config.services, tag) or {}), service_config)
    return service_config


def launch_keys(ctx: click.Context) -> LaunchKeys:
    """The launch keys that a service context reads from its parent, or none without a parent."""
    return cast(LaunchKeys, ctx.parent.params) if ctx.parent else {}
