"""Download job admission: one checked, merged and allowed job from a request body, or one refusal."""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import click
from aiohttp import web

from unshackle.core.api import handlers
from unshackle.core.api.download_manager import plan_keys, stamped_keys
from unshackle.core.api.errors import APIError
from unshackle.core.config import config
from unshackle.core.dl_options import DEFAULT_DOWNLOAD_PARAMS, DL_OPTION_API_NAMES
from unshackle.core.remote_protocol import APIErrorCode
from unshackle.core.run_plan import PlanError, api_plan
from unshackle.core.services import ROUTING_PARAMS, Services
from unshackle.core.utils.collections import as_list, ci_get
from unshackle.core.utils.service_params import service_options

ENVELOPE_KEYS = ("service", "title_id", "owner_key")
log = logging.getLogger("api")
SERVER_STAMPS = ("client_keys", "server_proxy")
LANGUAGE_LIST_KEYS = (
    "lang",
    "v_lang",
    "a_lang",
    "s_lang",
    "forced_s_lang",
    "require_audio",
    "require_video",
    "require_subs",
)
POSITIVE_INT_KEYS = (
    "vbitrate",
    "abitrate",
    "workers",
    "download_processes",
    "downloads",
    "tmdb_id",
    "tvdb_id",
    "latest_episodes",
)


@dataclass(frozen=True)
class JobSpec:
    """A download job that passed admission: its service tag, its title and the merged parameters it runs with."""

    service: str
    title_id: str
    params: dict[str, Any]


async def admit_job(data: dict[str, Any], request: web.Request | None = None) -> JobSpec:
    """The job that the request body ``data`` asks for, checked, merged with the server defaults and allowed for the
    caller's API key. Raises one APIError for the first refusal. A create and a retry both come here."""
    handlers.require_fields(data, "service", "title_id")
    if not isinstance(data["title_id"], str):
        raise APIError(APIErrorCode.INVALID_INPUT, "title_id must be a string", details={"title_id": data["title_id"]})
    if refusal := handlers.wanted_refusal(data.get("wanted")):
        raise APIError(APIErrorCode.INVALID_INPUT, refusal, details={"wanted": data.get("wanted")})

    server, info, data = await handlers.relay_target(data, request)
    service = info["tag"] if info else handlers.validate_service(data["service"], request)
    if not service:
        raise APIError(
            APIErrorCode.INVALID_SERVICE,
            f"Invalid or unavailable service: {data['service']}",
            details={"service": data["service"]},
        )
    return await asyncio.to_thread(job_spec, {**data, "service": service}, server, request)


def job_spec(data: dict[str, Any], server: str | None, request: web.Request | None) -> JobSpec:
    """The blocking part of :func:`admit_job`, for a request with a resolved service tag; ``server`` names the
    remote server of a relayed job. The server stamps ``server_proxy``, so a client value cannot grant it."""
    data = check_parameters(data)
    service = data["service"]
    sent = {k: v for k, v in data.items() if k not in ENVELOPE_KEYS}
    serve_defaults = {k: v for k, v in (config.serve or {}).items() if k in DEFAULT_DOWNLOAD_PARAMS and v is not None}
    # A relayed job gets no local service overrides, so none of its keys may count as defaults.
    params = {
        **DEFAULT_DOWNLOAD_PARAMS,
        **serve_defaults,
        **({} if server else service_dl_defaults(service)),
        **sent,
        "client_keys": sorted(sent),
    }
    merged = {**params, "service": service, "title_id": data["title_id"]}
    check_plan(merged)
    enforce_download_gates(merged, request)

    # Service options travel nested, so a flat key the worker reads stays a dl value; click fills the defaults
    # when the worker builds the service. A remote service applies its own on the remote server.
    cli_params = None if server else getattr(getattr(Services.load(service), "cli", None), "params", None)
    if cli_params is not None:
        options = service_options(data, {param.name for param in cli_params}, handlers.DL_TRANSPORT_KEYS)
        handlers.cast_or_refuse(cli_params, click.Context(click.Command(service)), options)
        params["service_params"] = options
    params["server_proxy"] = handlers.request_policy(request).allows("server_proxy")
    return JobSpec(service, data["title_id"], params)


def resubmission(service: str, title_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """The request body that made a job with these stored values, so a retry passes the admission a create passes."""
    body = {k: params[k] for k in stamped_keys(params) if k in params and k not in SERVER_STAMPS}
    return {**body, "service": service, "title_id": title_id}


def invalid(message: str, data: dict[str, Any]) -> APIError:
    """The INVALID_PARAMETERS refusal of a job request."""
    return APIError(
        APIErrorCode.INVALID_PARAMETERS,
        message,
        details={"service": data.get("service"), "title_id": data.get("title_id")},
    )


def check_parameters(data: dict[str, Any]) -> dict[str, Any]:
    """``data`` with ``output_dir`` resolved under the downloads directory, or INVALID_PARAMETERS for the first value
    that the API does not take. ``data`` stays as it is."""

    for banned in ("postscript", "post_script", "post_scripts"):
        if data.get(banned):
            raise invalid(f"'{banned}' is not accepted over the API, only from unshackle.yaml", data)

    # The worker reads cookies/{service}/{profile}.txt, so a profile must not be able to name a path.
    try:
        handlers.client_profile(data)
    except APIError as e:
        raise invalid(e.message, data) from None

    checked = dict(data)
    # The serve user would create and write any directory a client names; keep it under downloads.
    if data.get("output_dir"):
        root = config.directories.downloads.resolve()
        raw = str(data["output_dir"])
        if "\x00" in raw:
            raise invalid("output_dir is not a usable path.", data)
        try:
            target = (root / raw).resolve()
        except (OSError, ValueError):
            raise invalid("output_dir is not a usable path.", data) from None
        if not target.is_relative_to(root):
            raise invalid("output_dir must be a path under the server's downloads directory.", data)
        checked["output_dir"] = str(target)

    if data.get("all_drm") and data.get("cdm"):
        raise invalid(
            "all_drm needs a CDM for each DRM system, so it cannot be used with cdm, which selects one device.", data
        )

    for name in POSITIVE_INT_KEYS:
        value = data.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise invalid(f"{name} must be a positive integer", data)

    if data.get("quality") is not None:
        try:
            handlers.requested_heights(data["quality"])
        except APIError as e:
            raise invalid(e.message, data) from None

    channels = data.get("channels")
    if channels is not None and (not isinstance(channels, (int, float)) or channels <= 0):
        raise invalid("channels must be a positive number", data)

    imdb_id = data.get("imdb_id")
    if imdb_id is not None and (not isinstance(imdb_id, str) or not re.fullmatch(r"tt\d+", imdb_id)):
        raise invalid("imdb_id must be an IMDB ID like 'tt1375666'", data)

    for key, default in DEFAULT_DOWNLOAD_PARAMS.items():
        if isinstance(default, bool) and data.get(key) is not None and not isinstance(data[key], bool):
            raise invalid(f"{key} must be a boolean", data)

    for key in LANGUAGE_LIST_KEYS:
        value = data.get(key)
        if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
            raise invalid(f"{key} must be an array of language strings", data)

    return checked


def check_plan(params: dict[str, Any]) -> None:
    """INVALID_PARAMETERS when dl would refuse the job's merged parameters, as the worker plans them."""
    from unshackle.commands.dl import subtitle_look

    try:
        plan = api_plan(params, plan_keys(params)).params
    except PlanError as e:
        raise invalid(e.message, params) from None
    if plan.get("sub_font"):
        try:
            subtitle_look(plan.get("sub_format"), plan["sub_font"], None)
        except ValueError as e:
            raise invalid(f"Invalid sub_font: {e}", params) from None


def enforce_download_gates(params: dict[str, Any], request: web.Request | None = None) -> None:
    """Refuse a job that the caller's API key or the ``serve`` config does not allow, from its merged parameters.

    A download job licenses with the server CDM, so the API key needs ``server_cdm`` for the service, and an API
    key with its own device config for the service gets a refusal. Under ``server_cdm_max_height`` the job must set a quality at or
    under the cap, no ``best_available`` and no HYBRID range. Without ``server_proxy`` a proxy must be a full URI or
    nothing, as in :func:`resolve_handler_proxy`. A ``cdm`` override needs ``serve.cdm_overrides`` (a device list,
    or ``true`` for any), and a ``credential`` needs ``serve.allow_job_credentials``.
    """
    service = params.get("service")
    policy = handlers.request_policy(request)
    if not policy.allows("server_cdm", service):
        raise APIError(
            APIErrorCode.FORBIDDEN,
            "Download jobs license with the server CDM, which is not enabled for this API key on this service",
        )
    if service and policy.owns_identity(service):
        raise APIError(
            APIErrorCode.FORBIDDEN,
            "Download jobs run on the server's own device config, and this API key has its own for this service",
        )
    cap = policy.max_height(service)
    if cap is not None:
        capped = f"Download jobs license with the server CDM, up to {cap}p for this API key"
        wanted = handlers.requested_height(params.get("quality"))
        if wanted is None or wanted > cap:
            raise handlers.capped_error(f"{capped}: pass a quality", cap)
        if params.get("best_available"):
            raise handlers.capped_error(f"{capped}: drop best_available", cap)
        if any(str(getattr(r, "value", r)).upper() == "HYBRID" for r in as_list(params.get("range") or [])):
            raise handlers.capped_error(f"{capped}: drop HYBRID", cap)

    if params.get("proxy_download") is not None and not isinstance(params["proxy_download"], str):
        raise APIError(APIErrorCode.INVALID_INPUT, "proxy_download must be a string.")

    if any(str(params.get(key) or "").startswith("tunnel:") for key in ("proxy", "proxy_download")):
        raise APIError(
            APIErrorCode.INVALID_PROXY,
            "A download job cannot use a tunnel: proxy, because a tunnel belongs to one --remote client run.",
        )

    if not policy.allows("server_proxy"):
        handlers.resolve_handler_proxy(params, service or "", request)
        if params.get("proxy_download"):
            # the download proxy is gated like the main one: a full URI or nothing, never a server provider
            handlers.resolve_handler_proxy(
                {**params, "proxy": params["proxy_download"], "client_region": None}, service or "", request
            )

    requested_cdm = params.get("cdm")
    if requested_cdm:
        allowed = (config.serve or {}).get("cdm_overrides")
        permitted = allowed is True or (isinstance(allowed, (list, tuple, set)) and requested_cdm in allowed)
        if not permitted:
            raise APIError(
                APIErrorCode.FORBIDDEN,
                "The requested CDM is not permitted for API downloads.",
                details={"cdm": requested_cdm},
            )

    if (params.get("credential") or params.get("credentials")) and not (config.serve or {}).get(
        "allow_job_credentials"
    ):
        raise APIError(APIErrorCode.FORBIDDEN, "Per-request credentials are not permitted for API downloads.")


def service_dl_defaults(service: str) -> dict[str, Any]:
    """The ``services.<TAG>.dl`` values that name an API download parameter, keyed as the API names them."""
    result: dict[str, Any] = {}
    for key, value in ((ci_get(config.services, service) or {}).get("dl") or {}).items():
        key = DL_OPTION_API_NAMES.get(key, key)
        if value is None or key in ROUTING_PARAMS:
            continue
        if key not in DEFAULT_DOWNLOAD_PARAMS:
            log.debug("Ignoring services.%s.dl key %r: not an API download parameter", service, key)
            continue
        result[key] = value
    return result
