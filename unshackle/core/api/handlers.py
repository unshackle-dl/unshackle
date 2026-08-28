import asyncio
import base64
import enum
import json
import logging
import re
from datetime import date as date_
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from aiohttp import web

from unshackle.core.api.compression import safe_inflate
from unshackle.core.api.errors import APIError, APIErrorCode, handle_api_exception
from unshackle.core.api.input_bridge import AuthStatus, InputBridge
from unshackle.core.api.sanitize import safe_cache_key, sanitize_log
from unshackle.core.config import config
from unshackle.core.constants import AUDIO_CODEC_MAP, DYNAMIC_RANGE_MAP, VIDEO_CODEC_MAP
from unshackle.core.providers.anilist import parse_anilist_ref
from unshackle.core.proxies.resolve import initialize_proxy_providers, resolve_proxy
from unshackle.core.services import Services
from unshackle.core.titles import Episode, Movie, Song, Title_T
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video
from unshackle.core.utils.collections import ci_get
from unshackle.core.utils.redact import REDACTED, URL_USERINFO_RE

log = logging.getLogger("api")


DEFAULT_DOWNLOAD_PARAMS = {
    "profile": None,
    "quality": [],
    "vcodec": None,
    "acodec": None,
    "vbitrate": None,
    "abitrate": None,
    "vbitrate_range": None,
    "abitrate_range": None,
    "range": ["SDR"],
    "channels": None,
    "no_atmos": False,
    "wanted": [],
    "latest_episode": False,
    "lang": ["orig"],
    "v_lang": [],
    "a_lang": [],
    "s_lang": ["all"],
    "require_audio": [],
    "require_video": [],
    "require_subs": [],
    "forced_subs": False,
    "forced_s_lang": [],
    "exact_lang": False,
    "sub_format": None,
    "video_only": False,
    "audio_only": False,
    "subs_only": False,
    "chapters_only": False,
    "no_subs": False,
    "no_audio": False,
    "no_chapters": False,
    "no_video": False,
    "no_attachments": False,
    "audio_description": False,
    "slow": None,
    "split_audio": None,
    "skip_dl": False,
    "export": False,
    "cdm_only": None,
    "proxy": None,
    "no_proxy": False,
    "no_proxy_download": False,
    "no_folder": False,
    "no_source": False,
    "no_mux": False,
    "workers": None,
    "adaptive_workers": False,
    "download_processes": 1,
    "continue_downloads": False,
    "downloads": 1,
    "worst": False,
    "best_available": False,
    "repack": False,
    "tag": None,
    "tmdb_id": None,
    "imdb_id": None,
    "tvdb_id": None,
    "tvdb_order": None,
    "anilist_id": None,
    "enrich": False,
    "daily": False,
    "output_dir": None,
    "no_cache": False,
    "reset_cache": False,
}


# Keys that are part of the API transport envelope, not service.cli options.
# Used by instantiate_service to avoid passing them as kwargs to a service.
LIST_HANDLER_TRANSPORT_KEYS = {
    "service",
    "title_id",
    "profile",
    "season",
    "episode",
    "part",
    "wanted",
    "proxy",
    "no_proxy",
    "query",
}


def load_full_cdm(service: str, profile: Optional[str], cdm_type: Optional[str] = None) -> Optional[Any]:
    """Load a real CDM object for the given service.

    Services often touch ``ctx.obj.cdm.security_level`` / ``.device_type`` / ``.system_id``
    inside ``__init__``, so the lightweight ``resolve_server_cdm`` stub is not enough
    for list_titles / list_tracks / search. Mirrors ``dl.get_cdm`` selection logic but
    skips the quality-tier shortcuts (no track context yet) and falls back to the stub
    if the configuration has no device, or if loading fails.
    """
    from unshackle.core.cdm import load_cdm
    from unshackle.core.config import config as app_config

    cdm_name = ci_get(app_config.cdm, service) or ci_get(app_config.cdm, "default")
    if isinstance(cdm_name, dict):
        lower_keys = {k.lower(): v for k, v in cdm_name.items()}
        if {"widevine", "playready"} & lower_keys.keys():
            drm_key = None
            if cdm_type:
                drm_key = {"wv": "widevine", "widevine": "widevine", "pr": "playready", "playready": "playready"}.get(
                    cdm_type.lower()
                )
            cdm_name = lower_keys.get(drm_key or "widevine") or lower_keys.get("playready")
        else:
            cdm_name = cdm_name.get(profile) or cdm_name.get("default") or ci_get(app_config.cdm, "default")

    if not cdm_name or not isinstance(cdm_name, str):
        return resolve_server_cdm(service, profile, cdm_type)

    try:
        return load_cdm(cdm_name, service_name=service)
    except Exception as exc:  # noqa: BLE001 - fall back to stub on load failure
        log.warning(
            f"load_cdm({sanitize_log(cdm_name)!r}) failed for {sanitize_log(service)}: {exc}; using lightweight stub"
        )
        return resolve_server_cdm(service, profile, cdm_type)


def load_service_yaml(normalized_service: str) -> dict:
    """Load a service's config.yaml and merge it with the global override block."""
    import yaml

    from unshackle.core.utils.collections import merge_dict

    service_config_path = Services.get_path(normalized_service) / config.filenames.config
    if service_config_path.exists():
        service_config = yaml.safe_load(service_config_path.read_text(encoding="utf8")) or {}
    else:
        service_config = {}
    merge_dict(config.services.get(normalized_service), service_config)
    return service_config


def build_parent_ctx(
    profile: Optional[str],
    cdm: Any,
    proxy_param: Optional[str],
    no_proxy: bool,
    proxy_providers: list,
    service_config: dict,
    extra_params: Optional[Dict[str, Any]] = None,
) -> Any:
    """Assemble a parent click Context for invoking a service.cli through ctx.invoke().

    The service's CLI callback uses ``ctx.parent.params`` (proxy, range_, vcodec, and the
    other dl options) and ``ctx.obj`` (ContextData). Both flow through Click's parent chain.
    """
    import click

    from unshackle.core.utils.click_types import ContextData

    @click.command()
    @click.pass_context
    def dummy(ctx: click.Context) -> None:
        pass

    parent = click.Context(dummy)
    parent.obj = ContextData(config=service_config, cdm=cdm, proxy_providers=proxy_providers, profile=profile)
    params = {"proxy": proxy_param, "no_proxy": no_proxy}
    if extra_params:
        params.update(extra_params)
    parent.params = params
    return parent


def instantiate_service(
    parent_ctx: Any,
    service_module: Any,
    title: str,
    data: Optional[Dict[str, Any]] = None,
    transport_keys: Optional[set] = None,
) -> Any:
    """Instantiate a service by invoking its click cli through Click.

    Click fills option defaults through ``param.get_default()`` and runs type coercion,
    so we no longer have to inspect ``__init__`` or stitch defaults by hand. This function
    pulls extra kwargs from the ``data`` entries whose names match a cli option name
    and are not in the transport-key blocklist.
    """
    cli_params = getattr(getattr(service_module, "cli", None), "params", []) or []
    cli_param_names = {p.name for p in cli_params if hasattr(p, "name") and p.name}
    transport_keys = transport_keys or set()
    extras: Dict[str, Any] = {}
    if data:
        for k, v in data.items():
            if k in cli_param_names and k not in transport_keys and k != "title":
                extras[k] = v
    return parent_ctx.invoke(service_module.cli, title=title, **extras)


def setup_list_service(
    data: Dict[str, Any],
    normalized_service: str,
    profile: Optional[str],
    title_id: str,
    request: Optional[web.Request] = None,
) -> Any:
    """Assemble and authenticate a service instance for list_titles / list_tracks.

    Runs the shared preamble: load yaml → get proxy → load CDM → assemble ctx →
    instantiate → authenticate. Raises APIError on proxy failure.
    """
    from unshackle.commands.dl import dl

    service_config = load_service_yaml(normalized_service)

    no_proxy = data.get("no_proxy", False)
    proxy_param, proxy_providers = resolve_handler_proxy(data, normalized_service, request)

    cdm = load_full_cdm(normalized_service, profile, data.get("cdm_type"))
    parent_ctx = build_parent_ctx(profile, cdm, proxy_param, no_proxy, proxy_providers, service_config)
    service_module = Services.load(normalized_service)
    service_instance = instantiate_service(parent_ctx, service_module, title_id, data, LIST_HANDLER_TRANSPORT_KEYS)

    cookies = dl.get_cookie_jar(normalized_service, profile)
    credential = dl.get_credentials(normalized_service, profile)
    service_instance.authenticate(cookies, credential)
    return service_instance


def get_allowed_services(request: Optional[web.Request] = None) -> Optional[List[str]]:
    """Get effective service allowlist considering global + per-key config.

    Returns None if no allowlist restricts the services.
    """
    global_allowed = config.serve.get("services")
    global_set: Optional[set[str]] = None
    if global_allowed:
        global_set = {Services.get_tag(s) for s in global_allowed}

    key_set: Optional[set[str]] = None
    if request:
        secret_key = request_secret_key(request)
        if secret_key:
            users = config.serve.get("users", {})
            user_config = users.get(secret_key, {})
            user_services = user_config.get("services")
            if user_services:
                key_set = {Services.get_tag(s) for s in user_services}

    if global_set and key_set:
        result = global_set & key_set
    elif global_set:
        result = global_set
    elif key_set:
        result = key_set
    else:
        return None

    return list(result)


def server_cdm_allowed(request: Optional[web.Request] = None, service: Optional[str] = None) -> bool:
    """Whether the calling API key may have the server operate the CDM licensing for ``service``.

    Configured API keys opt in with ``server_cdm: true`` for every service, or with a list of
    service tags for only those. An API key absent from ``serve.users`` (the admin secret) keeps
    full access.
    """
    if not request:
        return True
    secret_key = request_secret_key(request)
    user_config = config.serve.get("users", {}).get(secret_key)
    if user_config is None:
        return True
    allowed = user_config.get("server_cdm", False)
    if isinstance(allowed, str):
        allowed = [allowed]
    if isinstance(allowed, (list, tuple, set)):
        return service is not None and Services.get_tag(service).upper() in {
            Services.get_tag(s).upper() for s in allowed
        }
    return bool(allowed)


def server_proxy_allowed(request: Optional[web.Request] = None) -> bool:
    """Whether the calling API key may use the server's own configured proxy providers.

    Configured API keys opt in with ``server_proxy: true``, a real yaml boolean, so a
    quoted ``"true"`` or ``"no"`` does not count. It is a plain bool, not a per-service
    list, because a proxy is not tied to a service. No API key has implicit access: an
    API key absent from ``serve.users`` gets none, and neither does a call without a
    request, so a handler that does not pass ``request`` through fails closed.
    """
    if not request:
        return False
    secret_key = request_secret_key(request)
    user_config = (config.serve or {}).get("users", {}).get(secret_key)
    if not isinstance(user_config, dict):
        return False
    return user_config.get("server_proxy") is True


JOB_EVENTS_ROUTE = "/api/download/jobs/{job_id}/events"

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Secret-Key, Authorization",
    "Access-Control-Max-Age": "3600",
}


def request_secret_key(request: web.Request) -> Optional[str]:
    """The caller's API key: the X-Secret-Key header, or on the events route the secret_key
    query param (EventSource cannot send headers)."""
    key = request.headers.get("X-Secret-Key")
    if not key:
        resource = request.match_info.route.resource
        if resource is not None and resource.canonical == JOB_EVENTS_ROUTE:
            key = request.query.get("secret_key")
    return key


def caller_key(request: Optional[web.Request] = None) -> str:
    """The authenticating API key for a request, or 'anonymous' when unauthenticated."""
    if not request:
        return "anonymous"
    return request_secret_key(request) or "anonymous"


def owns_job(job: Any, request: Optional[web.Request] = None) -> bool:
    """Whether the calling API key owns this job.

    Jobs created without an owner (no-key mode / legacy) stay shared. Otherwise, only the
    API key that created the job can see it (constant-time compare).
    """
    import hmac

    owner = getattr(job, "owner_key", None)
    if owner is None:
        return True
    return hmac.compare_digest(owner, caller_key(request))


def validate_service(service_tag: str, request: Optional[web.Request] = None) -> Optional[str]:
    """Validate and normalise the service tag, and examine the allowlist."""
    if not isinstance(service_tag, str):
        return None
    try:
        normalized = Services.get_tag(service_tag)
        service_path = Services.get_path(normalized)
        if not service_path.exists():
            return None
        allowed = get_allowed_services(request)
        if allowed is not None and normalized not in allowed:
            return None
        return normalized
    except KeyError:
        return None


def require_fields(data: Dict[str, Any], *names: str) -> None:
    """Raise INVALID_INPUT for the first missing/falsy required field."""
    for name in names:
        if not data.get(name):
            raise APIError(
                APIErrorCode.INVALID_INPUT,
                f"Missing required parameter: {name}",
                details={"missing_parameter": name},
            )


def part_key_suffix(part: Optional[int]) -> str:
    """`.2` selection-syntax suffix for a part-ful episode, empty otherwise."""
    return f".{part}" if part is not None else ""


def serialize_title(title: Title_T) -> Dict[str, Any]:
    """Convert a title object to JSON-serializable dict."""
    title_language = str(title.language) if hasattr(title, "language") and title.language else None
    # Optional display metadata a service may provide: a synopsis (title.description) and a
    # release/air date or poster image URL stashed in title.data. Surfaced so a client can show
    # a richer listing without re-fetching the page.
    description = getattr(title, "description", None) or None
    _data = getattr(title, "data", None)
    date = _data.get("date") if isinstance(_data, dict) else None
    cover_url = _data.get("cover_url") if isinstance(_data, dict) else None

    is_episode = isinstance(title, Episode)
    is_song = isinstance(title, Song)
    episode_part = title.part if isinstance(title, Episode) else None
    if is_episode:
        # no part suffix here: remote_service.build_title rebuilds the Episode from this dict, so a
        # suffixed name plus the structural `part` below would render the part twice in the filename
        name = title.name if title.name else f"Episode {title.number:02d}"
    else:
        name = str(title.name) if hasattr(title, "name") else str(title)

    result = {
        "type": "episode" if is_episode else "movie" if isinstance(title, Movie) else "song" if is_song else "other",
        "name": name,
        "id": str(title.id) if hasattr(title, "id") else None,
        "language": title_language,
        "description": description,
        "date": date,
        "cover_url": cover_url,
    }
    # "other" titles carry no year; only Episode/Movie/Song do.
    if isinstance(title, (Episode, Movie, Song)):
        result["year"] = title.year
    if isinstance(title, Episode):
        result["series_title"] = str(title.title)
        result["season"] = title.season
        result["number"] = title.number
        # every key below is conditional, so JSON for a title without them is unchanged
        if episode_part is not None:
            result["part"] = episode_part
        if title.air_date is not None:
            result["air_date"] = (
                title.air_date.isoformat() if isinstance(title.air_date, date_) else str(title.air_date)
            )
        if title.absolute is not None:
            result["absolute"] = title.absolute
        if getattr(title, "daily", None) is not None:
            result["daily"] = title.daily
    if is_song:
        for field in (
            "artist",
            "album",
            "track",
            "disc",
            "album_artist",
            "release_type",
            "total_tracks",
            "total_discs",
            "genre",
            "explicit",
            "isrc",
            "upc",
            "copyright",
            "label",
            "lyrics",
            "artwork_url",
        ):
            result[field] = getattr(title, field, None)
    if isinstance(title, (Episode, Movie)) and getattr(title, "anime", None) is not None:
        result["anime"] = title.anime

    return result


def stamp_service_flags(serialized: Dict[str, Any], service_instance: Any) -> Dict[str, Any]:
    """Fill anime/daily from the service class for titles that set neither.

    The client rebuilds titles from this JSON against a synthetic service class, so a
    class-level ANIME/DAILY would otherwise be lost on the way across.
    """
    for key, attr in (("anime", "ANIME"), ("daily", "DAILY")):
        if key == "daily" and serialized.get("type") != "episode":
            continue
        if serialized.get(key) is None and getattr(type(service_instance), attr, False):
            serialized[key] = True
    return serialized


def extract_manifests(tracks) -> List[Dict[str, Any]]:
    """Extract manifest data from tracks for client-side re-parsing.

    Serializes DASH and ISM manifest XML as zlib-compressed base64 strings
    so the client can reconstruct track.data locally. HLS tracks download
    directly from their URL, so this function does not serialise their manifest.
    """
    import base64
    import zlib

    from lxml import etree

    from unshackle.core.config import config as app_config

    compression_level = app_config.serve.get("compression_level", 1)

    seen: set[str] = set()
    manifests: List[Dict[str, Any]] = []

    for track in list(tracks.videos) + list(tracks.audio) + list(tracks.subtitles):
        manifest_url = str(track.url) if track.url else None
        if not manifest_url or manifest_url in seen:
            continue

        if track.data.get("dash") and track.data["dash"].get("manifest"):
            seen.add(manifest_url)
            xml_bytes = etree.tostring(track.data["dash"]["manifest"], xml_declaration=True, encoding="UTF-8")
            compressed = zlib.compress(xml_bytes, compression_level) if compression_level else xml_bytes
            manifests.append(
                {
                    "type": "dash",
                    "url": manifest_url,
                    "data": base64.b64encode(compressed).decode("ascii"),
                }
            )
        elif track.data.get("ism") and track.data["ism"].get("manifest"):
            seen.add(manifest_url)
            xml_bytes = etree.tostring(track.data["ism"]["manifest"], xml_declaration=True, encoding="UTF-8")
            compressed = zlib.compress(xml_bytes, compression_level) if compression_level else xml_bytes
            manifests.append(
                {
                    "type": "ism",
                    "url": manifest_url,
                    "data": base64.b64encode(compressed).decode("ascii"),
                }
            )

    return manifests


def serialize_drm(drm_list) -> Optional[List[Dict[str, Any]]]:
    """Serialise DRM objects to JSON-serializable list."""
    if not drm_list:
        return None

    if not isinstance(drm_list, list):
        drm_list = [drm_list]

    result = []
    for drm in drm_list:
        drm_info = {}
        drm_class = drm.__class__.__name__
        drm_info["type"] = drm_class.lower()

        # pywidevine exposes dumps(); pyplayready's PSSH does not, so PlayReady falls back
        # to the base64 it was built from. Without it the client drops every PlayReady
        # entry and licenses the title with the wrong CDM.
        pssh_obj = getattr(drm, "_pssh", None)
        pssh_b64 = (getattr(drm, "data", None) or {}).get("pssh_b64")
        if pssh_obj is not None and hasattr(pssh_obj, "dumps"):
            try:
                drm_info["pssh"] = pssh_obj.dumps()
            except (ValueError, TypeError, KeyError):
                log.warning(
                    "Failed to serialize PSSH for DRM type=%s pssh_type=%s",
                    drm_class,
                    type(pssh_obj).__name__,
                    exc_info=True,
                )
        if not drm_info.get("pssh") and pssh_b64:
            drm_info["pssh"] = pssh_b64

        if hasattr(drm, "kids") and drm.kids:
            drm_info["kids"] = [str(kid) for kid in drm.kids]

        if hasattr(drm, "content_keys") and drm.content_keys:
            drm_info["content_keys"] = {str(k): v for k, v in drm.content_keys.items()}

        # Get license URL - essential for remote licensing
        if hasattr(drm, "license_url") and drm.license_url:
            drm_info["license_url"] = str(drm.license_url)
        elif hasattr(drm, "_license_url") and drm._license_url:
            drm_info["license_url"] = str(drm._license_url)

        result.append(drm_info)

    return result if result else None


def enum_name(value: Any) -> str:
    """Return an enum-like value's .name, falling back to str()."""
    return value.name if hasattr(value, "name") else str(value)


def descriptor_name(track: Any) -> Optional[str]:
    """Manifest descriptor (HLS/DASH/URL) name for a track, or None."""
    descriptor = getattr(track, "descriptor", None)
    return enum_name(descriptor) if descriptor else None


def drm_preference_name(track: Any) -> Optional[str]:
    """The track's drm_preference as a full DRM system name, "widevine" or "playready"; None when unset."""
    preference = getattr(track, "drm_preference", None)
    if not preference:
        return None
    return {"wv": "widevine", "pr": "playready"}.get(preference, preference)


MANIFEST_DATA_KEYS = ("hls", "dash", "ism")


def serialize_track_data(track: Any) -> Optional[Dict[str, Any]]:
    """The JSON-safe part of a service-set ``track.data``.

    This drops the manifest parsers' own keys: the client rebuilds those from the
    ``manifests`` payload, and they hold live lxml and m3u8 objects that JSON cannot carry.
    """
    data = getattr(track, "data", None)
    if not isinstance(data, dict):
        return None
    safe: Dict[str, Any] = {}
    for key, value in data.items():
        if key in MANIFEST_DATA_KEYS:
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        safe[key] = value
    return safe or None


def serialize_track_common(track: Any) -> Dict[str, Any]:
    """The Track fields every track type shares, a superset of ``Track.to_dict()``."""
    return {
        "id": str(track.id),
        "language": str(track.language) if track.language else None,
        "is_original_lang": bool(getattr(track, "is_original_lang", False)),
        "name": getattr(track, "name", None),
        "needs_repack": bool(getattr(track, "needs_repack", False)),
        "edition": list(getattr(track, "edition", None) or []),
        "descriptor": descriptor_name(track),
        "drm": serialize_drm(track.drm) if getattr(track, "drm", None) else None,
        "drm_preference": getattr(track, "drm_preference", None),
        "data": serialize_track_data(track),
    }


def serialize_video_track(track: Video, include_url: bool = False) -> Dict[str, Any]:
    """Convert video track to JSON-serializable dict."""
    codec_name = enum_name(track.codec)
    range_name = enum_name(track.range)

    result = {
        "id": str(track.id),
        "codec": codec_name,
        "codec_display": VIDEO_CODEC_MAP.get(codec_name, codec_name),
        "bitrate": int(track.bitrate / 1000) if track.bitrate else None,
        "bitrate_bps": track.bitrate or None,
        "width": track.width,
        "height": track.height,
        "resolution": f"{track.width}x{track.height}" if track.width and track.height else None,
        "fps": track.fps if track.fps else None,
        "range": range_name,
        "range_display": DYNAMIC_RANGE_MAP.get(range_name, range_name),
        "scan_type": enum_name(track.scan_type) if track.scan_type else None,
        "closed_captions": list(getattr(track, "closed_captions", None) or []),
        "dv_compatible_bitstream": bool(getattr(track, "dv_compatible_bitstream", False)),
    }
    result.update(serialize_track_common(track))
    if include_url and hasattr(track, "url") and track.url:
        result["url"] = str(track.url)
    return result


def original_audio_ids(tracks: List[Audio], title: Title_T) -> set:
    """Return the ids of the audio tracks 'orig' resolves to, empty when the title has no language.

    This defers to Tracks.by_language so the flag agrees with the downloader. It asks
    exact mode first because CLDR rates a base tag and its paradigm regional variant as
    the same language, and only the RFC 4647 preference picks one ('en' over 'en-US'
    when both exist, 'pt-BR' over 'pt-PT' for a 'pt' title). The fuzzy fallback then
    catches the non-paradigm regionals exact mode drops, such as an 'es' title that
    carries only 'es-419'.
    """
    language = getattr(title, "language", None)
    if not language:
        return set()
    matches = Tracks.by_language(tracks, [str(language)], exact_match=True) or Tracks.by_language(
        tracks, [str(language)]
    )
    return {t.id for t in matches}


def serialize_audio_track(track: Audio, include_url: bool = False, is_original: bool = False) -> Dict[str, Any]:
    """Convert audio track to JSON-serializable dict.

    Get is_original from original_audio_ids so the flag always agrees with the
    track 'orig' would download.
    """
    codec_name = enum_name(track.codec)

    result = {
        "id": str(track.id),
        "codec": codec_name,
        "codec_display": AUDIO_CODEC_MAP.get(codec_name, codec_name),
        "bitrate": int(track.bitrate / 1000) if track.bitrate else None,
        "bitrate_bps": track.bitrate or None,
        "channels": track.channels if track.channels else None,
        "is_original": is_original,
        "atmos": track.atmos if hasattr(track, "atmos") else False,
        "joc": getattr(track, "joc", 0),
        "descriptive": track.descriptive if hasattr(track, "descriptive") else False,
    }
    result.update(serialize_track_common(track))
    if include_url and hasattr(track, "url") and track.url:
        result["url"] = str(track.url)
    return result


def serialize_subtitle_track(track: Subtitle, include_url: bool = False) -> Dict[str, Any]:
    """Convert subtitle track to JSON-serializable dict."""
    result = {
        "id": str(track.id),
        "codec": enum_name(track.codec),
        "forced": track.forced if hasattr(track, "forced") else False,
        "sdh": track.sdh if hasattr(track, "sdh") else False,
        "cc": track.cc if hasattr(track, "cc") else False,
    }
    result.update(serialize_track_common(track))
    if include_url and hasattr(track, "url") and track.url:
        result["url"] = str(track.url)
    return result


ATTACHMENT_INLINE_LIMIT = 20 * 1024 * 1024


def serialize_attachment(attachment: Any) -> Optional[Dict[str, Any]]:
    """One attachment as JSON, with a path-only attachment carried as base64 file bytes.

    A service can attach a file it already wrote to disk (a font, a poster). The client
    has no access to the server's filesystem, so those bytes travel inline.
    """
    result: Dict[str, Any] = {
        "url": attachment.url,
        "name": attachment.name,
        "mime_type": attachment.mime_type,
        "description": attachment.description,
    }
    if attachment.url:
        return result

    path = getattr(attachment, "path", None)
    if not path:
        return None
    path = Path(path)
    try:
        size = path.stat().st_size
        if size > ATTACHMENT_INLINE_LIMIT:
            log.warning(
                "Skipping attachment %s: %d bytes is over the %d byte inline limit",
                sanitize_log(path.name),
                size,
                ATTACHMENT_INLINE_LIMIT,
            )
            return None
        result["content"] = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as e:
        log.warning("Skipping attachment %s: %s", sanitize_log(path.name), e)
        return None
    result["file_name"] = path.name
    return result


async def search_handler(data: Dict[str, Any], request: Optional[web.Request] = None) -> web.Response:
    """Answer the request to find titles."""
    from unshackle.commands.dl import dl

    service_tag = data.get("service")
    query = data.get("query")

    if not service_tag:
        raise APIError(APIErrorCode.INVALID_INPUT, "Missing required 'service' field")
    if not query:
        raise APIError(APIErrorCode.INVALID_PARAMETERS, "Missing required 'query' field")

    # get_tag echoes an unknown tag back, so it can never be falsy; validate_service
    # is the check that actually resolves the service directory and the allowlist.
    normalized_service = validate_service(service_tag, request)
    if not normalized_service:
        raise APIError(
            APIErrorCode.INVALID_SERVICE,
            f"Service '{service_tag}' not found",
            details={"service": service_tag},
        )

    profile = data.get("profile")
    no_proxy = data.get("no_proxy", False)

    service_config = load_service_yaml(normalized_service)

    proxy_param, proxy_providers = await asyncio.to_thread(resolve_handler_proxy, data, normalized_service, request)

    cdm = load_full_cdm(normalized_service, profile, data.get("cdm_type"))
    parent_ctx = build_parent_ctx(profile, cdm, proxy_param, no_proxy, proxy_providers, service_config)
    service_module = Services.load(normalized_service)

    try:
        service_instance = instantiate_service(parent_ctx, service_module, query)
    except Exception as exc:
        raise APIError(
            APIErrorCode.SERVICE_ERROR,
            f"Failed to initialize service: {exc}",
            details={"service": normalized_service},
        )

    cookies = dl.get_cookie_jar(normalized_service, profile)
    credential = dl.get_credentials(normalized_service, profile)
    service_instance.authenticate(cookies, credential)

    results = []
    try:
        for result in service_instance.search():
            results.append(
                {
                    "id": result.id,
                    "title": result.title,
                    "description": result.description,
                    "label": result.label,
                    "url": result.url,
                }
            )
    except NotImplementedError:
        raise APIError(
            APIErrorCode.SERVICE_ERROR,
            f"Search is not supported by {normalized_service}",
            details={"service": normalized_service},
        )

    return web.json_response({"results": results, "count": len(results)})


async def list_titles_handler(data: Dict[str, Any], request: Optional[web.Request] = None) -> web.Response:
    """Answer the list-titles request."""
    require_fields(data, "service", "title_id")
    service_tag = data.get("service")
    title_id = data.get("title_id")
    profile = data.get("profile")

    normalized_service = validate_service(service_tag, request)
    if not normalized_service:
        raise APIError(
            APIErrorCode.INVALID_SERVICE,
            f"Invalid or unavailable service: {service_tag}",
            details={"service": service_tag},
        )

    try:
        service_instance = await asyncio.to_thread(
            setup_list_service, data, normalized_service, profile, title_id, request
        )
        titles = service_instance.get_titles()

        if hasattr(titles, "__iter__") and not isinstance(titles, str):
            title_list = [stamp_service_flags(serialize_title(t), service_instance) for t in titles]
        else:
            title_list = [stamp_service_flags(serialize_title(titles), service_instance)]

        return web.json_response({"titles": title_list})

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error listing titles")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "list_titles", "service": normalized_service, "title_id": title_id},
            debug_mode=debug_mode,
        )


async def list_tracks_handler(data: Dict[str, Any], request: Optional[web.Request] = None) -> web.Response:
    """Answer the list-tracks request."""
    require_fields(data, "service", "title_id")
    service_tag = data.get("service")
    title_id = data.get("title_id")
    profile = data.get("profile")

    normalized_service = validate_service(service_tag, request)
    if not normalized_service:
        raise APIError(
            APIErrorCode.INVALID_SERVICE,
            f"Invalid or unavailable service: {service_tag}",
            details={"service": service_tag},
        )

    try:
        service_instance = await asyncio.to_thread(
            setup_list_service, data, normalized_service, profile, title_id, request
        )
        titles = service_instance.get_titles()

        wanted_param = data.get("wanted")
        season = data.get("season")
        episode = data.get("episode")
        part = data.get("part")

        if hasattr(titles, "__iter__") and not isinstance(titles, str):
            titles_list = list(titles)

            wanted = None
            if wanted_param:
                from unshackle.core.utils.click_types import SeasonRange

                try:
                    season_range = SeasonRange()
                    if isinstance(wanted_param, list):
                        wanted = season_range.parse_tokens(*wanted_param)
                    else:
                        # convert(), not parse_tokens(), so a comma-separated string splits
                        # into tokens the way the CLI splits it
                        wanted = season_range.convert(wanted_param)
                    log.debug(
                        f"Parsed wanted '{sanitize_log(wanted_param)}' into {len(wanted)} episodes: {wanted[:10]}..."
                    )
                except (Exception, SystemExit) as e:
                    raise APIError(
                        APIErrorCode.INVALID_PARAMETERS,
                        f"Invalid wanted parameter: {e}",
                        details={"wanted": wanted_param, "service": normalized_service},
                    )
            elif season is not None and episode is not None:
                wanted = [f"{season}x{episode}{part_key_suffix(part)}"]

            if wanted:
                # Filter titles based on wanted episodes, similar to how dl.py does it
                matching_titles: list[Any] = []
                log.debug(f"Filtering {len(titles_list)} titles with {len(wanted)} wanted episodes")
                for title in titles_list:
                    if isinstance(title, Episode):
                        episode_key = f"{title.season}x{title.number}{part_key_suffix(title.part)}"
                        if title.matches_wanted(wanted):
                            log.debug(f"Episode {episode_key} matches wanted list")
                            matching_titles.append(title)
                        else:
                            log.debug(f"Episode {episode_key} not in wanted list")
                    elif isinstance(title, Song):
                        song_key = f"{title.disc}x{title.track}"
                        if title.matches_wanted(wanted):
                            log.debug(f"Song {song_key} matches wanted list")
                            matching_titles.append(title)
                        else:
                            log.debug(f"Song {song_key} not in wanted list")
                    else:
                        matching_titles.append(title)

                log.debug(f"Found {len(matching_titles)} matching titles")

                if not matching_titles:
                    raise APIError(
                        APIErrorCode.NO_CONTENT,
                        "No titles found matching wanted criteria",
                        details={
                            "service": normalized_service,
                            "title_id": title_id,
                            "wanted": wanted_param or wanted[0],
                        },
                    )

                # If multiple episodes match, return tracks for all episodes
                if len(matching_titles) > 1 and all(isinstance(t, Episode) for t in matching_titles):
                    episodes_data = []
                    failed_episodes = []

                    # Sort matching titles by season and episode number for consistent ordering
                    sorted_titles = sorted(matching_titles, key=lambda t: (t.season, t.number, t.part or 0))

                    for title in sorted_titles:
                        try:
                            tracks = service_instance.get_tracks(title)
                            video_tracks = sorted(tracks.videos, key=lambda t: t.bitrate or 0, reverse=True)
                            audio_tracks = sorted(tracks.audio, key=lambda t: t.bitrate or 0, reverse=True)

                            original_ids = original_audio_ids(audio_tracks, title)
                            episode_data = {
                                "title": serialize_title(title),
                                "video": [serialize_video_track(t) for t in video_tracks],
                                "audio": [
                                    serialize_audio_track(t, is_original=t.id in original_ids) for t in audio_tracks
                                ],
                                "subtitles": [serialize_subtitle_track(t) for t in tracks.subtitles],
                            }
                            episodes_data.append(episode_data)
                            log.debug(f"Successfully got tracks for {title.season}x{title.number}")
                        except SystemExit:
                            # Service calls sys.exit() for unavailable episodes - catch and skip
                            failed_episodes.append(f"S{title.season}E{title.number:02d}{part_key_suffix(title.part)}")
                            log.debug(f"Episode {title.season}x{title.number} not available, skipping")
                            continue
                        except (Exception, SystemExit) as e:
                            failed_episodes.append(f"S{title.season}E{title.number:02d}{part_key_suffix(title.part)}")
                            log.debug(f"Error getting tracks for {title.season}x{title.number}: {e}")
                            continue

                    if episodes_data:
                        response = {"episodes": episodes_data}
                        if failed_episodes:
                            response["unavailable_episodes"] = failed_episodes
                        return web.json_response(response)
                    else:
                        raise APIError(
                            APIErrorCode.NO_CONTENT,
                            f"No available episodes found. Unavailable: {', '.join(failed_episodes)}",
                            details={
                                "service": normalized_service,
                                "title_id": title_id,
                                "unavailable_episodes": failed_episodes,
                            },
                        )
                else:
                    first_title = matching_titles[0]
            else:
                first_title = titles_list[0]
        else:
            first_title = titles

        tracks = service_instance.get_tracks(first_title)

        video_tracks = sorted(tracks.videos, key=lambda t: t.bitrate or 0, reverse=True)
        audio_tracks = sorted(tracks.audio, key=lambda t: t.bitrate or 0, reverse=True)

        original_ids = original_audio_ids(audio_tracks, first_title)
        response = {
            "title": serialize_title(first_title),
            "video": [serialize_video_track(t) for t in video_tracks],
            "audio": [serialize_audio_track(t, is_original=t.id in original_ids) for t in audio_tracks],
            "subtitles": [serialize_subtitle_track(t) for t in tracks.subtitles],
        }

        return web.json_response(response)

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error listing tracks")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "list_tracks", "service": normalized_service, "title_id": title_id},
            debug_mode=debug_mode,
        )


VALID_VCODECS = ["H264", "H265", "H.264", "H.265", "AVC", "HEVC", "VC1", "VC-1", "VP8", "VP9", "AV1"]
VALID_ACODECS = ["AAC", "AC3", "EC3", "EAC3", "DD", "DD+", "AC4", "OPUS", "FLAC", "ALAC", "VORBIS", "OGG", "DTS"]


def check_codec(value: Any, allowed: List[str], name: str) -> Optional[str]:
    """Validate a comma-string or list of codec tokens against `allowed` (case-insensitive)."""
    if isinstance(value, str):
        tokens = [v.strip() for v in value.split(",") if v.strip()]
    elif isinstance(value, list):
        tokens = [str(v).strip() for v in value if str(v).strip()]
    else:
        return f"{name} must be a string or list"

    invalid = [token for token in tokens if token.upper() not in allowed]
    if invalid:
        return f"Invalid {name}: {', '.join(invalid)}. Must be one of: {', '.join(allowed)}"
    return None


def validate_download_parameters(data: Dict[str, Any]) -> Optional[str]:
    """
    Validate download parameters and return error message if invalid.

    Returns:
        None if valid, error message string if invalid
    """
    for banned in ("postscript", "post_script", "post_scripts"):
        if data.get(banned):
            return f"'{banned}' is not accepted over the API. Define post_scripts in unshackle.yaml."

    if "vcodec" in data and data["vcodec"]:
        err = check_codec(data["vcodec"], VALID_VCODECS, "vcodec")
        if err:
            return err

    if "acodec" in data and data["acodec"]:
        err = check_codec(data["acodec"], VALID_ACODECS, "acodec")
        if err:
            return err

    if "sub_format" in data and data["sub_format"]:
        valid_sub_formats = ["SRT", "VTT", "ASS", "SSA", "TTML", "STPP", "WVTT", "SMI", "SUB", "MPL2", "TMP"]
        if data["sub_format"].upper() not in valid_sub_formats:
            return f"Invalid sub_format: {data['sub_format']}. Must be one of: {', '.join(valid_sub_formats)}"

    if "vbitrate" in data and data["vbitrate"] is not None:
        if not isinstance(data["vbitrate"], int) or data["vbitrate"] <= 0:
            return "vbitrate must be a positive integer"

    if "abitrate" in data and data["abitrate"] is not None:
        if not isinstance(data["abitrate"], int) or data["abitrate"] <= 0:
            return "abitrate must be a positive integer"

    if "vbitrate_range" in data and data["vbitrate_range"] is not None:
        if not isinstance(data["vbitrate_range"], str) or "-" not in data["vbitrate_range"]:
            return "vbitrate_range must be a string in 'MIN-MAX' format (e.g., '6000-7000')"

    if "abitrate_range" in data and data["abitrate_range"] is not None:
        if not isinstance(data["abitrate_range"], str) or "-" not in data["abitrate_range"]:
            return "abitrate_range must be a string in 'MIN-MAX' format (e.g., '128-256')"

    if "channels" in data and data["channels"] is not None:
        if not isinstance(data["channels"], (int, float)) or data["channels"] <= 0:
            return "channels must be a positive number"

    if "workers" in data and data["workers"] is not None:
        if not isinstance(data["workers"], int) or data["workers"] <= 0:
            return "workers must be a positive integer"

    if "download_processes" in data and data["download_processes"] is not None:
        if not isinstance(data["download_processes"], int) or data["download_processes"] <= 0:
            return "download_processes must be a positive integer"

    if "downloads" in data and data["downloads"] is not None:
        if not isinstance(data["downloads"], int) or data["downloads"] <= 0:
            return "downloads must be a positive integer"

    for name in ("tmdb_id", "tvdb_id"):
        if data.get(name) is not None:
            value = data[name]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                return f"{name} must be a positive integer"

    if data.get("imdb_id") is not None:
        if not isinstance(data["imdb_id"], str) or not re.fullmatch(r"tt\d+", data["imdb_id"]):
            return "imdb_id must be an IMDB ID like 'tt1375666'"

    if data.get("anilist_id") is not None:
        value = data["anilist_id"]
        # same parser as --anilist so both surfaces accept and reject the same inputs
        valid = not isinstance(value, bool) and isinstance(value, (int, str)) and parse_anilist_ref(value)
        if not valid:
            return "anilist_id must be a positive integer, or a MyAnimeList ID like 'mal:21'"

    supplied_ids = [name for name in ("tmdb_id", "imdb_id", "tvdb_id") if data.get(name)]
    if len(supplied_ids) > 1:
        return (
            f"Cannot use multiple external IDs: {', '.join(supplied_ids)}. "
            "Give one ID and unshackle resolves the others from it."
        )

    exclusive_flags = []
    if data.get("video_only"):
        exclusive_flags.append("video_only")
    if data.get("audio_only"):
        exclusive_flags.append("audio_only")
    if data.get("subs_only"):
        exclusive_flags.append("subs_only")
    if data.get("chapters_only"):
        exclusive_flags.append("chapters_only")

    if len(exclusive_flags) > 1:
        return f"Cannot use multiple exclusive flags: {', '.join(exclusive_flags)}"

    if data.get("no_subs") and data.get("subs_only"):
        return "Cannot use both no_subs and subs_only"
    if data.get("no_audio") and data.get("audio_only"):
        return "Cannot use both no_audio and audio_only"

    for key in ("require_audio", "require_video", "require_subs"):
        value = data.get(key)
        if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
            return f"{key} must be an array of language strings"

    if "range" in data and data["range"]:
        # "HDR10P" is the canonical range value ("+" is awkward in scripts); "HDR10+" stays valid.
        valid_ranges = ["SDR", "HDR10", "HDR10P", "DV", "HLG", "HYBRID"]
        accepted = {*valid_ranges, "HDR10+"}
        values = data["range"] if isinstance(data["range"], list) else [data["range"]]
        for r in values:
            if r.upper() not in accepted:
                return f"Invalid range value: {r}. Must be one of: {', '.join(valid_ranges)}"

    return None


def enforce_download_gates(params: Dict[str, Any], request: Optional[web.Request] = None) -> None:
    """Enforce serve-config gates on per-job cdm overrides and client-supplied credentials.

    A per-request `cdm` selects a server-side device, so this function gates it here rather than
    honour it blindly. `serve.cdm_overrides` opts in: a list permits only those device names, or `true`
    permits any (for a single trusted client). Unset/false rejects every override.
    A per-request `credential` (or `credentials` map) authenticates the job with client-supplied
    secrets instead of the server-side credentials. Gate it behind `serve.allow_job_credentials`
    (default off) so a default deployment stays locked to its own credentials. This mirrors the
    CDM gate. A download job licenses DRM in-process with the server's own CDM, so an API key without
    ``server_cdm`` cannot submit or retry jobs. A job can also spend the server's proxy providers,
    through an explicit ``proxy`` country code or through the geofence auto-proxy, so the
    ``server_proxy`` policy from :func:`resolve_handler_proxy` applies here too: without the opt-in
    the job must carry a full proxy URI or no proxy at all.
    """
    if not server_cdm_allowed(request, params.get("service")):
        raise APIError(
            APIErrorCode.FORBIDDEN,
            "Download jobs license with the server CDM, which is not enabled for this key on this service.",
        )

    if not server_proxy_allowed(request):
        resolve_handler_proxy(params, params.get("service") or "", request)

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

    if params.get("credential") or params.get("credentials"):
        if not (config.serve or {}).get("allow_job_credentials"):
            raise APIError(
                APIErrorCode.FORBIDDEN,
                "Per-request credentials are not permitted for API downloads.",
            )


async def download_handler(data: Dict[str, Any], request: Optional[web.Request] = None) -> web.Response:
    """Answer the download request: make a download job and add it to the queue."""
    from unshackle.core.api.download_manager import get_download_manager

    require_fields(data, "service", "title_id")
    service_tag = data.get("service")
    title_id = data.get("title_id")

    normalized_service = validate_service(service_tag, request)
    if not normalized_service:
        raise APIError(
            APIErrorCode.INVALID_SERVICE,
            f"Invalid or unavailable service: {service_tag}",
            details={"service": service_tag},
        )

    validation_error = validate_download_parameters(data)
    if validation_error:
        raise APIError(
            APIErrorCode.INVALID_PARAMETERS,
            validation_error,
            details={"service": normalized_service, "title_id": title_id},
        )

    await asyncio.to_thread(enforce_download_gates, data, request)

    try:
        service_module = Services.load(normalized_service)
        service_specific_defaults = {}

        # Skip None defaults here: this dict overlays into job params; injecting
        # None for keys like `profile` would clobber serve-config overrides.
        # Missing required __init__ params are handled in download_manager.perform_download.
        if hasattr(service_module, "cli") and hasattr(service_module.cli, "params"):
            for param in service_module.cli.params:
                if hasattr(param, "name") and param.default is not None and not isinstance(param.default, enum.Enum):
                    service_specific_defaults[param.name] = param.default

        manager = get_download_manager()
        await manager.start_workers()

        filtered_params = {k: v for k, v in data.items() if k not in ["service", "title_id"]}
        # Overlay any dl-relevant keys from `serve:` config (e.g. downloads, workers) so the API
        # respects server-side defaults without each client having to send them.
        serve_overrides = {
            k: v for k, v in (config.serve or {}).items() if k in DEFAULT_DOWNLOAD_PARAMS and v is not None
        }
        params_with_defaults = {
            **DEFAULT_DOWNLOAD_PARAMS,
            **serve_overrides,
            **service_specific_defaults,
            **filtered_params,
        }
        # The download worker reads this to decide whether dl may load the server's proxy
        # providers. Always stamped server-side so a client-sent value cannot grant it.
        params_with_defaults["server_proxy"] = server_proxy_allowed(request)
        job = manager.create_job(normalized_service, title_id, owner_key=caller_key(request), **params_with_defaults)

        return web.json_response(
            {"job_id": job.job_id, "status": job.status.value, "created_time": job.created_time.isoformat()}, status=202
        )

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error creating download job")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "create_download_job", "service": normalized_service, "title_id": title_id},
            debug_mode=debug_mode,
        )


async def list_download_jobs_handler(data: Dict[str, Any], request: Optional[web.Request] = None) -> web.Response:
    """Answer the request to show download jobs, with optional filtering and sorting."""
    from unshackle.core.api.download_manager import get_download_manager

    try:
        manager = get_download_manager()
        jobs = [job for job in manager.list_jobs() if owns_job(job, request)]

        status_filter = data.get("status")
        if status_filter:
            jobs = [job for job in jobs if job.status.value == status_filter]

        service_filter = data.get("service")
        if service_filter:
            jobs = [job for job in jobs if job.service == service_filter]

        sort_by = data.get("sort_by", "created_time")
        sort_order = data.get("sort_order", "desc")

        valid_sort_fields = ["created_time", "started_time", "completed_time", "progress", "status", "service"]
        if sort_by not in valid_sort_fields:
            raise APIError(
                APIErrorCode.INVALID_PARAMETERS,
                f"Invalid sort_by: {sort_by}. Must be one of: {', '.join(valid_sort_fields)}",
                details={"sort_by": sort_by, "valid_values": valid_sort_fields},
            )

        if sort_order not in ["asc", "desc"]:
            raise APIError(
                APIErrorCode.INVALID_PARAMETERS,
                "Invalid sort_order: must be 'asc' or 'desc'",
                details={"sort_order": sort_order, "valid_values": ["asc", "desc"]},
            )

        reverse = sort_order == "desc"

        def get_sort_key(job):
            """Get the value to sort on, and give a None value a default."""
            value = getattr(job, sort_by, None)
            if value is None:
                if sort_by in ["created_time", "started_time", "completed_time"]:
                    from datetime import datetime

                    return datetime.min if not reverse else datetime.max
                elif sort_by == "progress":
                    return 0
                elif sort_by in ["status", "service"]:
                    return ""
            return value

        jobs = sorted(jobs, key=get_sort_key, reverse=reverse)

        include_full = str(data.get("full") or "").lower() == "true"
        job_list = [job.to_dict(include_full_details=include_full) for job in jobs]

        return web.json_response({"jobs": job_list})

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error listing download jobs")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "list_download_jobs"},
            debug_mode=debug_mode,
        )


async def get_download_job_handler(job_id: str, request: Optional[web.Request] = None) -> web.Response:
    """Answer the request for one specific download job."""
    from unshackle.core.api.download_manager import get_download_manager

    try:
        manager = get_download_manager()
        job = manager.get_job(job_id)

        if not job or not owns_job(job, request):
            raise APIError(
                APIErrorCode.JOB_NOT_FOUND,
                "Job not found",
                details={"job_id": job_id},
            )

        return web.json_response(job.to_dict(include_full_details=True))

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception(f"Error getting download job {sanitize_log(job_id)}")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "get_download_job", "job_id": job_id},
            debug_mode=debug_mode,
        )


async def download_job_events_handler(job_id: str, request: web.Request) -> web.StreamResponse:
    """Stream a download job's progress to the caller as Server-Sent Events."""
    import json

    from unshackle.core.api.download_manager import TERMINAL_STATUSES, get_download_manager

    manager = get_download_manager()
    job = manager.get_job(job_id)

    if not job or not owns_job(job, request):
        raise APIError(
            APIErrorCode.JOB_NOT_FOUND,
            "Job not found",
            details={"job_id": job_id},
        )

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            # Stops nginx from buffering the stream.
            "X-Accel-Buffering": "no",
            **CORS_HEADERS,
        }
    )
    await response.prepare(request)

    async def send(event: str, data: Dict[str, Any]) -> None:
        payload = json.dumps(data, separators=(",", ":"), default=str)
        await response.write(f"event: {event}\ndata: {payload}\n\n".encode())

    queue: Optional[asyncio.Queue] = None
    get_task: Optional[asyncio.Task] = None
    try:
        await send("snapshot", job.to_dict(include_full_details=True))

        if job.status not in TERMINAL_STATUSES:
            queue = manager.subscribe(job_id)
        if queue is None or job.status in TERMINAL_STATUSES:
            await send(job.status.value, job.to_dict(include_full_details=True))
            return response

        while True:
            if get_task is None:
                get_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({get_task}, timeout=15)
            if not done:
                await response.write(b": keep-alive\n\n")
                continue
            item = get_task.result()
            get_task = None
            if item is None:
                break
            await send(item["event"], item["data"])
    except (ConnectionResetError, asyncio.CancelledError):
        log.debug(f"SSE client disconnected from job {sanitize_log(job_id)}")
    finally:
        if get_task is not None:
            get_task.cancel()
        if queue is not None:
            manager.unsubscribe(job_id, queue)

    return response


async def cancel_download_job_handler(job_id: str, request: Optional[web.Request] = None) -> web.Response:
    """Answer the cancel/remove download job request."""
    from unshackle.core.api.download_manager import TERMINAL_STATUSES, get_download_manager

    try:
        manager = get_download_manager()

        job = manager.get_job(job_id)
        if not job or not owns_job(job, request):
            raise APIError(
                APIErrorCode.JOB_NOT_FOUND,
                "Job not found",
                details={"job_id": job_id},
            )

        # Terminal jobs can't be cancelled; DELETE removes them from the manager instead.
        if job.status in TERMINAL_STATUSES:
            manager.remove_job(job_id)
            return web.Response(status=204)

        success = manager.cancel_job(job_id)

        if success:
            return web.json_response({"status": "success", "message": "Job cancelled"})
        else:
            raise APIError(
                APIErrorCode.INVALID_PARAMETERS,
                "Job cannot be cancelled (already completed or failed)",
                details={"job_id": job_id},
            )

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception(f"Error cancelling download job {sanitize_log(job_id)}")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "cancel_download_job", "job_id": job_id},
            debug_mode=debug_mode,
        )


async def clear_finished_download_jobs_handler(request: Optional[web.Request] = None) -> web.Response:
    """Answer the clear finished download jobs request."""
    from unshackle.core.api.download_manager import get_download_manager

    try:
        manager = get_download_manager()
        removed = manager.clear_finished_jobs()
        return web.json_response({"removed": removed})

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error clearing finished download jobs")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "clear_finished_download_jobs"},
            debug_mode=debug_mode,
        )


async def retry_download_job_handler(job_id: str, request: Optional[web.Request] = None) -> web.Response:
    """Answer the retry download job request: enqueue a new job with the original's parameters."""
    from unshackle.core.api.download_manager import TERMINAL_STATUSES, get_download_manager

    try:
        manager = get_download_manager()

        job = manager.get_job(job_id)
        if not job or not owns_job(job, request):
            raise APIError(
                APIErrorCode.JOB_NOT_FOUND,
                "Job not found",
                details={"job_id": job_id},
            )

        if job.status not in TERMINAL_STATUSES:
            raise APIError(
                APIErrorCode.CONFLICT,
                "Only completed, failed, or cancelled jobs can be retried",
                details={"job_id": job_id, "status": job.status.value},
            )

        # Re-apply creation-time gates so retry cannot bypass the caller's service allowlist
        # or currently-disabled cdm_overrides / allow_job_credentials config.
        if not validate_service(job.service, request):
            raise APIError(
                APIErrorCode.INVALID_SERVICE,
                f"Invalid or unavailable service: {job.service}",
                details={"service": job.service},
            )
        await asyncio.to_thread(enforce_download_gates, {**job.parameters, "service": job.service}, request)

        await manager.start_workers()

        # Reuse the raw in-memory parameters; redaction only ever applies to serialized copies.
        new_job = manager.create_job(
            job.service,
            job.title_id,
            owner_key=caller_key(request),
            **{**job.parameters, "server_proxy": server_proxy_allowed(request)},
        )

        return web.json_response(
            {
                "job_id": new_job.job_id,
                "status": new_job.status.value,
                "created_time": new_job.created_time.isoformat(),
            },
            status=202,
        )

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception(f"Error retrying download job {sanitize_log(job_id)}")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "retry_download_job", "job_id": job_id},
            debug_mode=debug_mode,
        )


async def prioritize_download_job_handler(job_id: str, request: Optional[web.Request] = None) -> web.Response:
    """Answer the prioritise download job request: move a queued job to the front of the queue."""
    from unshackle.core.api.download_manager import JobStatus, get_download_manager

    try:
        manager = get_download_manager()

        job = manager.get_job(job_id)
        if not job or not owns_job(job, request):
            raise APIError(
                APIErrorCode.JOB_NOT_FOUND,
                "Job not found",
                details={"job_id": job_id},
            )

        if job.status != JobStatus.QUEUED:
            raise APIError(
                APIErrorCode.CONFLICT,
                "Only queued jobs can be prioritized",
                details={"job_id": job_id, "status": job.status.value},
            )

        manager.prioritize_job(job_id)

        return web.json_response({"job_id": job_id, "position": "front"})

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception(f"Error prioritizing download job {sanitize_log(job_id)}")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "prioritize_download_job", "job_id": job_id},
            debug_mode=debug_mode,
        )


CONFIG_SECRET_KEY_RE = re.compile(r"secret|password|token|api_key|credential", re.IGNORECASE)


def redact_config(value: Any) -> Any:
    """Recursively mask secret-looking config keys and URL userinfo. Stringify paths."""
    if isinstance(value, dict):
        return {
            str(k): (REDACTED if v and CONFIG_SECRET_KEY_RE.search(str(k)) else redact_config(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_config(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str) and "@" in value:
        return URL_USERINFO_RE.sub(f"{REDACTED}@", value)
    return value


async def profiles_handler(request: Optional[web.Request] = None) -> web.Response:
    """Answer the request for the credential profiles."""
    try:
        allowed = get_allowed_services(request)
        profiles: Dict[str, List[str]] = {}
        for service, creds in (config.credentials or {}).items():
            # a plain (non-dict) credential is unnamed; that service is omitted entirely
            if not isinstance(creds, dict):
                continue
            # get_tag returns its input unchanged when no service matches; str() covers non-str YAML keys
            tag = Services.get_tag(str(service))
            if allowed is not None and tag not in allowed:
                continue
            profiles[tag] = sorted(str(name) for name in creds)
        return web.json_response({"profiles": profiles})

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error listing profiles")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(e, context={"operation": "profiles"}, debug_mode=debug_mode)


async def server_config_handler(request: Optional[web.Request] = None) -> web.Response:
    """Answer the read-only effective server config request (secrets redacted)."""
    from unshackle.core.api.download_manager import get_download_manager

    try:
        manager = get_download_manager()
        serve_cfg = config.serve or {}
        allowed = get_allowed_services(request)
        service_tags = Services.get_tags()
        if allowed is not None:
            service_tags = [t for t in service_tags if t in allowed]

        payload = {
            "dl": redact_config(config.dl),
            "serve": {
                "max_concurrent_downloads": manager.max_concurrent_downloads,
                "job_retention_hours": manager.job_retention_hours,
                "history_limit": int(serve_cfg.get("history_limit", 100)),
                "services": serve_cfg.get("services") or None,
                "remote_only": bool(serve_cfg.get("remote_only", False)),
                "cdm_overrides": redact_config(serve_cfg.get("cdm_overrides")),
                "allow_job_credentials": bool(serve_cfg.get("allow_job_credentials", False)),
            },
            "directories": {
                "downloads": str(config.directories.downloads),
                "temp": str(config.directories.temp),
                "cache": str(config.directories.cache),
            },
            "services": service_tags,
        }
        return web.json_response({"config": payload})

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error building server config")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(e, context={"operation": "server_config"}, debug_mode=debug_mode)


async def download_history_handler(data: Dict[str, Any], request: Optional[web.Request] = None) -> web.Response:
    """Answer the persisted download history request."""
    from unshackle.core.api.download_manager import read_job_history

    try:
        limit = 100
        limit_raw = data.get("limit")
        if limit_raw is not None:
            try:
                limit = int(limit_raw)
            except (TypeError, ValueError):
                raise APIError(
                    APIErrorCode.INVALID_PARAMETERS, "limit must be an integer", details={"limit": limit_raw}
                )
            if limit < 1:
                raise APIError(APIErrorCode.INVALID_PARAMETERS, "limit must be >= 1", details={"limit": limit_raw})

        allowed = get_allowed_services(request)
        if allowed is None:
            history = read_job_history(limit=limit, service=data.get("service"))
        else:
            # Read unbounded, drop entries outside the caller's allowlist, then apply limit.
            allowed_upper = {a.upper() for a in allowed}
            entries = read_job_history(limit=0, service=data.get("service"))
            history = [e for e in entries if str(e.get("service") or "").upper() in allowed_upper][:limit]
        return web.json_response({"history": history, "count": len(history)})

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error reading download history")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(e, context={"operation": "download_history"}, debug_mode=debug_mode)


async def delete_history_handler(job_id: str, request: Optional[web.Request] = None) -> web.Response:
    """Delete one persisted history entry by job_id."""
    from unshackle.core.api.download_manager import delete_job_history

    try:
        allowed = get_allowed_services(request)
        allowed_upper = {a.upper() for a in allowed} if allowed is not None else None
        if not delete_job_history(job_id, allowed=allowed_upper):
            raise APIError(APIErrorCode.NOT_FOUND, "History entry not found", details={"job_id": job_id})
        return web.Response(status=204)

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error deleting download history")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(e, context={"operation": "delete_history"}, debug_mode=debug_mode)


def require_no_active_downloads(operation: str) -> None:
    """Raise 409 CONFLICT if any job is currently downloading."""
    from unshackle.core.api.download_manager import JobStatus, get_download_manager

    active = [j.job_id for j in get_download_manager().list_jobs() if j.status == JobStatus.DOWNLOADING]
    if active:
        raise APIError(
            APIErrorCode.CONFLICT,
            f"Cannot {operation} while downloads are active",
            details={"active_jobs": active},
        )


async def clear_cache_handler(request: Optional[web.Request] = None) -> web.Response:
    """Answer the clear cache directory request."""
    from unshackle.commands.env import clear_directory

    try:
        require_no_active_downloads("clear cache")
        _, freed_bytes = await asyncio.to_thread(clear_directory, config.directories.cache)
        return web.json_response({"cleared": True, "freed_bytes": freed_bytes})

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error clearing cache")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(e, context={"operation": "clear_cache"}, debug_mode=debug_mode)


async def clear_temp_handler(request: Optional[web.Request] = None) -> web.Response:
    """Answer the clear temp directory request."""
    from unshackle.commands.env import clear_directory

    try:
        require_no_active_downloads("clear temp")
        _, freed_bytes = await asyncio.to_thread(clear_directory, config.directories.temp)
        return web.json_response({"cleared": True, "freed_bytes": freed_bytes})

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error clearing temp")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(e, context={"operation": "clear_temp"}, debug_mode=debug_mode)


async def refresh_services_handler(request: Optional[web.Request] = None) -> web.Response:
    """Refresh the service repos configured in directories.services and reload the changed services."""
    from unshackle.core.api.download_manager import get_download_manager
    from unshackle.core.services import refresh_and_reload

    try:
        busy = get_download_manager().busy_services()
        repos = await asyncio.to_thread(refresh_and_reload, busy)
        return web.json_response({"refreshed": all(r["updated"] for r in repos), "repos": repos})

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error refreshing service repos")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(e, context={"operation": "refresh_services"}, debug_mode=debug_mode)


VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)*")


def binary_version(path: Any) -> Optional[str]:
    """Best-effort version probe of a binary. Returns None when nothing is parseable."""
    import subprocess

    for flag in ("--version", "-version"):
        try:
            proc = subprocess.run(
                [str(path), flag], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        match = VERSION_RE.search(proc.stdout or "") or VERSION_RE.search(proc.stderr or "")
        if match:
            return match.group(0)
    return None


async def env_check_handler(request: Optional[web.Request] = None) -> web.Response:
    """Answer the environment dependency check request."""
    from unshackle.commands.env import get_dependencies

    def run_checks() -> List[Dict[str, Any]]:
        checks = []
        for dep in get_dependencies():
            binary = dep["binary"]
            checks.append(
                {
                    "name": dep["name"],
                    "installed": binary is not None,
                    "version": binary_version(binary) if binary else None,
                    "required": dep["required"],
                }
            )
        return checks

    try:
        checks = await asyncio.to_thread(run_checks)
        return web.json_response({"checks": checks})

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error running env check")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(e, context={"operation": "env_check"}, debug_mode=debug_mode)


SESSION_TRANSPORT_KEYS = {
    "service",
    "title_id",
    "season",
    "episode",
    "part",
    "wanted",
    "proxy",
    "no_proxy",
    "credentials",
    "cookies",
    "cache",
    "client_region",
    "cdm_type",
    "range_",
    "vcodec",
    "quality",
    "best_available",
}


def create_service_instance(
    normalized_service: str,
    title_id: str,
    data: Dict[str, Any],
    proxy_param: Optional[str],
    proxy_providers: list,
    profile: Optional[str],
) -> Any:
    """Make and authenticate a service instance.

    Supports client-sent credentials/cookies (for remote-dl) with fallback
    to server-local config (for backward compatibility).
    """
    from unshackle.commands.dl import dl
    from unshackle.core.credential import Credential
    from unshackle.core.tracks import Video

    service_config = load_service_yaml(normalized_service)
    cdm = load_full_cdm(normalized_service, profile, data.get("cdm_type"))

    # Reconstruct enum track-selection params from client data so service code that reads
    # ctx.parent.params (Service.__init__ proxy/range/vcodec/best_available block) sees enums.
    range_names = data.get("range_")
    range_values: Optional[list] = None
    if range_names:
        range_values = []
        for name in range_names:
            try:
                range_values.append(Video.Range[name])
            except KeyError:
                pass
        range_values = range_values or None

    vcodec_names = data.get("vcodec")
    vcodec_values: Optional[list] = None
    if vcodec_names:
        vcodec_values = []
        for name in vcodec_names:
            try:
                vcodec_values.append(Video.Codec[name])
            except KeyError:
                pass
        vcodec_values = vcodec_values or None

    extra_params = {
        "range_": range_values,
        "vcodec": vcodec_values,
        "quality": data.get("quality"),
        "best_available": data.get("best_available", False),
    }

    parent_ctx = build_parent_ctx(
        profile,
        cdm,
        proxy_param,
        data.get("no_proxy", False),
        proxy_providers,
        service_config,
        extra_params=extra_params,
    )

    service_module = Services.load(normalized_service)
    service_instance = instantiate_service(parent_ctx, service_module, title_id, data, SESSION_TRANSPORT_KEYS)

    # Resolve credentials: client-sent > server-local
    cred_data = data.get("credentials")
    if cred_data and isinstance(cred_data, dict):
        credential = Credential(
            username=cred_data["username"],
            password=cred_data["password"],
            extra=cred_data.get("extra"),
        )
    else:
        credential = dl.get_credentials(normalized_service, profile)

    # Resolve cookies: client-sent > server-local
    cookie_text = data.get("cookies")
    if cookie_text and isinstance(cookie_text, str):
        import base64
        import tempfile
        from http.cookiejar import MozillaCookieJar

        cookie_str = safe_inflate(base64.b64decode(cookie_text)).decode("utf-8")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(cookie_str)
            tmp_path = f.name
        try:
            cookies = MozillaCookieJar(tmp_path)
            cookies.load(ignore_discard=True, ignore_expires=True)
        finally:
            import os
            from contextlib import suppress

            with suppress(OSError):
                os.unlink(tmp_path)
    else:
        cookies = dl.get_cookie_jar(normalized_service, profile)

    return service_instance, cookies, credential


async def session_create_handler(data: Dict[str, Any], request: Optional[web.Request] = None) -> web.Response:
    """Answer the remote session creation: authenticate + get titles + get tracks + get chapters.

    This is the main entry point for remote-dl clients. It creates a persistent
    remote session on the server with the authenticated service instance, fetches all
    titles and tracks, and returns everything the client needs for track selection.
    """
    from unshackle.core.api.session_store import get_session_store

    service_tag = data.get("service")
    title_id = data.get("title_id")
    profile = data.get("profile")

    if not service_tag:
        raise APIError(APIErrorCode.INVALID_INPUT, "Missing required parameter: service")
    if not title_id:
        raise APIError(APIErrorCode.INVALID_INPUT, "Missing required parameter: title_id")

    normalized_service = validate_service(service_tag, request)
    if not normalized_service:
        raise APIError(
            APIErrorCode.INVALID_SERVICE,
            f"Invalid or unavailable service: {service_tag}",
            details={"service": service_tag},
        )

    try:
        proxy_param, proxy_providers = await asyncio.to_thread(resolve_handler_proxy, data, normalized_service, request)

        import hashlib
        import uuid as uuid_mod

        from unshackle.core.cacher import Cacher
        from unshackle.core.config import config as app_config

        session_id = str(uuid_mod.uuid4())
        api_key = request.headers.get("X-Secret-Key", "anonymous") if request else "anonymous"
        api_key_hash = hashlib.pbkdf2_hmac("sha256", api_key.encode(), b"unshackle-session-ns", 100_000).hex()[:12]
        session_cache_tag = f"_sessions/{api_key_hash}/{session_id}/{normalized_service}"

        service_instance, cookies, credential = create_service_instance(
            normalized_service,
            title_id,
            data,
            proxy_param,
            proxy_providers,
            profile,
        )

        service_instance.cache = Cacher(session_cache_tag)

        cache_data = data.get("cache", {})
        if cache_data:
            import base64

            cache_dir = app_config.directories.cache / session_cache_tag
            cache_dir.mkdir(parents=True, exist_ok=True)
            for key, content in cache_data.items():
                safe_name = safe_cache_key(key)
                if not safe_name:
                    log.warning(f"Rejecting unsafe session cache key: {sanitize_log(key)}")
                    continue
                decompressed = safe_inflate(base64.b64decode(content)).decode("utf-8")
                (cache_dir / safe_name).with_suffix(".json").write_text(decompressed, encoding="utf-8")

        bridge = InputBridge()
        service_instance._input_bridge = bridge

        store = get_session_store()
        session = await store.create(
            normalized_service,
            service_instance,
            session_id=session_id,
        )
        session.creator_ip = request.remote if request else None
        session.owner_key = api_key
        session.cache_tag = session_cache_tag
        session.input_bridge = bridge
        session.auth_status = AuthStatus.AUTHENTICATING

        async def run_auth() -> None:
            try:
                await asyncio.to_thread(service_instance.authenticate, cookies, credential)
                session.auth_status = AuthStatus.AUTHENTICATED
                bridge.status = AuthStatus.AUTHENTICATED
            except (Exception, SystemExit) as e:
                log.exception("Auth failed for session %s", session_id)
                session.auth_status = AuthStatus.FAILED
                session.auth_error = str(e)
                bridge.status = AuthStatus.FAILED

        asyncio.create_task(run_auth())

        return web.json_response(
            {
                "session_id": session.session_id,
                "service": normalized_service,
                "status": "authenticating",
            }
        )

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error creating session")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "session_create", "service": service_tag, "title_id": title_id},
            debug_mode=debug_mode,
        )


async def session_titles_handler(session_id: str, request: Optional[web.Request] = None) -> web.Response:
    """Get titles for the authenticated remote session.

    Called after `session/create`. This is separate from auth so that
    interactive auth flows (OTP, captcha) can complete before the server
    fetches titles.
    """
    session = await get_validated_session(session_id, request)
    require_authenticated(session)

    try:
        service_instance = session.service_instance
        titles = service_instance.get_titles()
        session.titles = titles

        if hasattr(titles, "__iter__") and not isinstance(titles, str):
            titles_list = list(titles)
        else:
            titles_list = [titles]

        serialized_titles = []
        for t in titles_list:
            tid = str(t.id) if hasattr(t, "id") else str(id(t))
            session.title_map[tid] = t
            serialized_titles.append(stamp_service_flags(serialize_title(t), service_instance))

        return web.json_response(
            {
                "session_id": session_id,
                "titles": serialized_titles,
            }
        )

    except (Exception, SystemExit) as e:
        log.exception("Error getting titles")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "session_titles", "session_id": session_id},
            debug_mode=debug_mode,
        )


async def session_tracks_handler(
    data: Dict[str, Any], session_id: str, request: Optional[web.Request] = None
) -> web.Response:
    """Get tracks and chapters for a specific title in the remote session.

    Called per-title by the client after `session/create` returns titles.
    This keeps auth separate from track fetching, allowing interactive
    auth flows (OTP, captcha) before the client requests any tracks.
    """
    session = await get_validated_session(session_id, request)
    require_authenticated(session)

    title_id = data.get("title_id")
    if not title_id:
        raise APIError(APIErrorCode.INVALID_INPUT, "Missing required parameter: title_id")

    title = session.title_map.get(str(title_id))
    if not title:
        raise APIError(
            APIErrorCode.INVALID_INPUT,
            f"Title not found in session: {title_id}",
            details={"available_titles": list(session.title_map.keys())},
        )

    try:
        service_instance = session.service_instance
        tracks = service_instance.get_tracks(title)

        title_tracks: Dict[str, Any] = {}
        for track in tracks.videos:
            title_tracks[str(track.id)] = track
            session.tracks[str(track.id)] = track
        for track in tracks.audio:
            title_tracks[str(track.id)] = track
            session.tracks[str(track.id)] = track
        for track in tracks.subtitles:
            title_tracks[str(track.id)] = track
            session.tracks[str(track.id)] = track
        session.tracks_by_title[str(title_id)] = title_tracks

        try:
            chapters = service_instance.get_chapters(title)
            session.chapters_by_title[str(title_id)] = chapters if chapters else []
        except (NotImplementedError, Exception):
            session.chapters_by_title[str(title_id)] = []

        video_tracks = sorted(tracks.videos, key=lambda t: t.bitrate or 0, reverse=True)
        audio_tracks = sorted(tracks.audio, key=lambda t: t.bitrate or 0, reverse=True)

        manifests = extract_manifests(tracks)

        svc_session = session.service_instance.session
        session_headers = dict(svc_session.headers) if hasattr(svc_session, "headers") else {}
        session_cookies = {}
        if hasattr(svc_session, "cookies"):
            for cookie in svc_session.cookies:
                if hasattr(cookie, "name") and hasattr(cookie, "value"):
                    session_cookies[cookie.name] = cookie.value

        from unshackle.core.config import config as app_config

        api_key = request.headers.get("X-Secret-Key", "anonymous") if request else "anonymous"
        user_cfg = app_config.serve.get("users", {}).get(api_key, {})
        has_wv = bool(user_cfg.get("devices"))
        has_pr = bool(user_cfg.get("playready_devices"))

        service_tag = session.service_tag
        config_cdm_type = detect_cdm_type_for_service(service_tag, app_config)

        track_has_wv = any(
            d.__class__.__name__ == "Widevine" for t in list(tracks.videos) + list(tracks.audio) if t.drm for d in t.drm
        )
        track_has_pr = any(
            d.__class__.__name__ == "PlayReady"
            for t in list(tracks.videos) + list(tracks.audio)
            if t.drm
            for d in t.drm
        )

        if config_cdm_type:
            server_cdm_type = config_cdm_type
        elif track_has_pr and has_pr:
            server_cdm_type = "playready"
        elif track_has_wv and has_wv:
            server_cdm_type = "widevine"
        elif has_wv:
            server_cdm_type = "widevine"
        else:
            server_cdm_type = "playready"

        original_ids = original_audio_ids(audio_tracks, title)

        return web.json_response(
            {
                "title": stamp_service_flags(serialize_title(title), service_instance),
                "video": [serialize_video_track(t, include_url=True) for t in video_tracks],
                "audio": [
                    serialize_audio_track(t, include_url=True, is_original=t.id in original_ids) for t in audio_tracks
                ],
                "subtitles": [serialize_subtitle_track(t, include_url=True) for t in tracks.subtitles],
                "chapters": [
                    {"timestamp": ch.timestamp, "name": ch.name}
                    for ch in session.chapters_by_title.get(str(title_id), [])
                ],
                "attachments": [a for a in map(serialize_attachment, tracks.attachments) if a],
                "manifests": manifests,
                "session_headers": session_headers,
                "session_cookies": session_cookies,
                "server_cdm": server_cdm_allowed(request, service_tag),
                "server_cdm_type": server_cdm_type,
            }
        )

    except (Exception, SystemExit) as e:
        log.exception(f"Error getting tracks for title {sanitize_log(title_id)}")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "session_tracks", "session_id": session_id, "title_id": title_id},
            debug_mode=debug_mode,
        )


async def session_segments_handler(
    data: Dict[str, Any], session_id: str, request: Optional[web.Request] = None
) -> web.Response:
    """Get segment URLs for selected tracks.

    The client calls this after selecting which tracks to download.
    Returns segment URLs, init data, DRM info, and any headers/cookies
    needed for CDN download.
    """
    session = await get_validated_session(session_id, request)

    track_ids = data.get("track_ids", [])
    if not track_ids:
        raise APIError(APIErrorCode.INVALID_INPUT, "Missing required parameter: track_ids")

    try:
        result: Dict[str, Any] = {}

        for track_id in track_ids:
            track = session.tracks.get(track_id)
            if not track:
                raise APIError(
                    APIErrorCode.TRACK_NOT_FOUND,
                    f"Track not found in session: {track_id}",
                    details={"track_id": track_id, "session_id": session_id},
                )

            descriptor_name = track.descriptor.name if hasattr(track.descriptor, "name") else str(track.descriptor)

            track_info: Dict[str, Any] = {
                "descriptor": descriptor_name,
                "url": str(track.url) if track.url else None,
                "drm": serialize_drm(track.drm) if hasattr(track, "drm") and track.drm else None,
            }

            service_session = session.service_instance.session
            if hasattr(service_session, "headers"):
                headers = dict(service_session.headers) if service_session.headers else {}
                track_info["headers"] = headers
            else:
                track_info["headers"] = {}

            if hasattr(service_session, "cookies"):
                cookie_dict = {}
                for cookie in service_session.cookies:
                    if hasattr(cookie, "name") and hasattr(cookie, "value"):
                        cookie_dict[cookie.name] = cookie.value
                    elif isinstance(cookie, str):
                        pass  # Skip non-standard cookie objects
                track_info["cookies"] = cookie_dict
            else:
                track_info["cookies"] = {}

            # Include manifest-specific data for segment resolution. Round-trip through
            # JSON so any non-serializable value becomes its str() (default=str).
            if hasattr(track, "data") and track.data:
                import json

                track_info["data"] = json.loads(json.dumps(track.data, default=str))
            else:
                track_info["data"] = {}

            result[track_id] = track_info

        return web.json_response({"tracks": result})

    except APIError:
        raise
    except (Exception, SystemExit) as e:
        log.exception("Error resolving segments")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "session_segments", "session_id": session_id},
            debug_mode=debug_mode,
        )


def cdm_type_stub(cdm_type: str) -> SimpleNamespace:
    """Lightweight CDM stand-in so ``is_playready_cdm()`` (reads ``.is_playready``)
    can detect the type without loading a full CDM."""
    return SimpleNamespace(is_playready=cdm_type == "playready")


def resolve_server_cdm(service: str, profile: Optional[str], cdm_type: Optional[str]) -> Optional[Any]:
    """Get the CDM for the server context.

    Checks the server's own CDM config (``config.cdm[service]``) to
    determine the CDM type without loading the full CDM object. This
    makes sure that when you use ``server_cdm: true``, the server's CDM
    determines device selection (e.g. PlayReady vs Widevine).

    Falls back to a lightweight stub from *cdm_type* only if the configuration
    has no server CDM for the service.
    """
    from unshackle.core.config import config as app_config

    cdm_name = ci_get(app_config.cdm, service)
    if cdm_name:
        if isinstance(cdm_name, dict):
            lower_keys = {k.lower(): v for k, v in cdm_name.items()}
            if {"widevine", "playready"} & lower_keys.keys():
                cdm_name = lower_keys.get("playready") or lower_keys.get("widevine")
            else:
                cdm_name = cdm_name.get("default") or next(iter(cdm_name.values()), None)

        if cdm_name and isinstance(cdm_name, str):
            detected_type = detect_cdm_type(cdm_name, app_config)
            if detected_type:
                return cdm_type_stub(detected_type)

    if cdm_type:
        return cdm_type_stub(cdm_type)
    return None


def detect_cdm_type_for_service(service: str, app_config: Any) -> Optional[str]:
    """Detect the CDM type configured for a service in config.cdm."""
    cdm_name = ci_get(app_config.cdm, service)
    if not cdm_name:
        return None
    if isinstance(cdm_name, dict):
        lower_keys = {k.lower(): v for k, v in cdm_name.items()}
        if {"widevine", "playready"} & lower_keys.keys():
            return "playready" if "playready" in lower_keys else "widevine"
        cdm_name = cdm_name.get("default") or next(iter(cdm_name.values()), None)
    if cdm_name and isinstance(cdm_name, str):
        return detect_cdm_type(cdm_name, app_config)
    return None


def detect_cdm_type(cdm_name: str, app_config: Any) -> Optional[str]:
    """Detect CDM type (PlayReady/widevine) from config without loading it.

    Checks remote_cdm entries and local file extensions to determine the type.
    """
    for entry in getattr(app_config, "remote_cdm", []) or []:
        if entry.get("name") == cdm_name:
            device_type = str(entry.get("device_type", entry.get("Device Type", ""))).upper()
            return "playready" if device_type == "PLAYREADY" else "widevine"

    prd_path = app_config.directories.prds / f"{cdm_name}.prd"
    if not prd_path.is_file():
        prd_path = app_config.directories.wvds / f"{cdm_name}.prd"
    if prd_path.is_file():
        return "playready"

    wvd_path = app_config.directories.wvds / f"{cdm_name}.wvd"
    if wvd_path.is_file():
        return "widevine"

    return None


def require_authenticated(session: Any) -> None:
    """Raise if the remote session has not finished authenticating."""
    if session.auth_status == AuthStatus.FAILED:
        raise APIError(
            APIErrorCode.AUTH_FAILED,
            f"Authentication failed: {session.auth_error or 'unknown error'}",
        )
    if session.auth_status in (AuthStatus.AUTHENTICATING, AuthStatus.PENDING_INPUT):
        raise APIError(
            APIErrorCode.INVALID_INPUT,
            f"Session authentication not complete (status: {session.auth_status.value})",
            details={"auth_status": session.auth_status.value},
        )


async def session_prompt_get_handler(session_id: str, request: Optional[web.Request] = None) -> web.Response:
    """Poll for pending interactive prompts during authentication.

    Returns the current auth status and any pending prompt that the
    remote client should display to the user.
    """
    session = await get_validated_session(session_id, request)

    if session.auth_status == AuthStatus.AUTHENTICATED:
        return web.json_response({"status": "authenticated"})

    if session.auth_status == AuthStatus.FAILED:
        return web.json_response({"status": "failed", "error": session.auth_error or "unknown error"})

    bridge = session.input_bridge
    if bridge:
        prompt = bridge.get_pending_prompt()
        if prompt:
            return web.json_response({"status": "pending_input", "prompt": prompt})

    return web.json_response({"status": "authenticating"})


async def session_prompt_post_handler(
    data: Dict[str, Any], session_id: str, request: Optional[web.Request] = None
) -> web.Response:
    """Submit a response to a pending interactive prompt.

    The remote client calls this after collecting user input (OTP code,
    PIN, or device-code confirmation) to unblock the server auth thread.
    """
    session = await get_validated_session(session_id, request)

    response_text = data.get("response")
    if response_text is None:
        raise APIError(APIErrorCode.INVALID_INPUT, "Missing required field: response")

    bridge = session.input_bridge
    if bridge is None or bridge.status != AuthStatus.PENDING_INPUT:
        raise APIError(APIErrorCode.INVALID_INPUT, "No prompt pending for this session")

    bridge.submit_response(str(response_text))
    return web.json_response({"status": "accepted"})


async def get_validated_session(session_id: str, request: Optional[web.Request]) -> Any:
    """Fetch a remote session and make sure that the caller owns it.

    Ownership is bound to the authenticating X-Secret-Key rather than the source IP:
    behind a reverse proxy every caller shares the proxy's address, so the IP check
    (kept as defence in depth) cannot distinguish users on its own.
    """
    import hmac

    from unshackle.core.api.session_store import get_session_store

    store = get_session_store()
    session = await store.get(session_id)
    if not session:
        raise APIError(
            APIErrorCode.SESSION_NOT_FOUND,
            f"Session not found or expired: {session_id}",
            details={"session_id": session_id},
        )
    if session.owner_key is not None and request is not None:
        caller_key = request.headers.get("X-Secret-Key", "anonymous")
        if not hmac.compare_digest(caller_key, session.owner_key):
            raise APIError(
                APIErrorCode.FORBIDDEN,
                "Session access denied",
            )
    if session.creator_ip and request and request.remote != session.creator_ip:
        raise APIError(
            APIErrorCode.FORBIDDEN,
            "Session access denied",
        )
    return session


def resolve_handler_proxy(
    data: Dict[str, Any], normalized_service: str, request: Optional[web.Request] = None
) -> tuple[Optional[str], list]:
    """Get the proxy and the proxy providers for an API request.

    ``server_proxy`` on the calling API key decides whether the server spends its own proxy
    subscriptions on this client. Without it the proxy-provider list stays empty, so the server
    rejects a country code in ``proxy``, and a client in another region must send a full proxy
    URI or ``no_proxy``. The returned list reaches ``ContextData.proxy_providers``, so the same gate
    also governs the geofence auto-proxy in ``Service.__init__``.

    The client region is the client-reported ``client_region`` field: an identifier, not a
    security boundary, because the spend gate is ``server_proxy`` on the API key. A client that
    reports no region, and an unknown region on either side, never blocks the request.
    """
    proxy_param = data.get("proxy")
    no_proxy = data.get("no_proxy", False)
    proxy_providers: list = []
    allowed = server_proxy_allowed(request)

    if allowed and not no_proxy:
        proxy_providers = initialize_proxy_providers()

    if proxy_param and not no_proxy:
        try:
            proxy_param = resolve_proxy(proxy_param, proxy_providers)
        except ValueError as e:
            if allowed:
                raise APIError(
                    APIErrorCode.INVALID_PROXY,
                    f"Proxy error: {e}",
                    details={"proxy": data.get("proxy"), "service": normalized_service},
                )
            raise APIError(
                APIErrorCode.INVALID_PROXY,
                "This server does not resolve country codes into proxies. "
                "Pass --proxy with a full proxy URI (http://, https:// or socks5://).",
                details={"service": normalized_service},
            )

    client_region = data.get("client_region")
    if client_region is not None and not isinstance(client_region, str):
        raise APIError(
            APIErrorCode.INVALID_INPUT,
            "client_region must be a string country code.",
            details={"service": normalized_service},
        )

    if not proxy_param and not no_proxy and client_region and (proxy_providers or not allowed):
        try:
            from unshackle.core.utils.ip_info import get_ip_info

            server_ip_info = get_ip_info(None, cached=True)
            server_region = server_ip_info.get("country", "").lower() if server_ip_info else None
        except Exception as e:
            log.debug(f"Server region lookup failed: {e!r}")
            server_region = None

        in_client_region = bool(server_region) and server_region == client_region.lower()
        if not allowed:
            if server_region and not in_client_region:
                raise APIError(
                    APIErrorCode.INVALID_PROXY,
                    "This server is in a different region from yours. Pass --proxy with a proxy "
                    "in the region you want, or pass --no-proxy to use the server's own connection.",
                    details={"service": normalized_service},
                )
        elif in_client_region:
            log.info(f"Server already in client region '{sanitize_log(client_region)}', no proxy needed")
        else:
            try:
                proxy_param = resolve_proxy(client_region, proxy_providers)
                log.info(f"Using server proxy for client region '{sanitize_log(client_region)}'")
            except ValueError:
                log.debug(f"No server proxy available for client region '{sanitize_log(client_region)}'")

    return proxy_param, proxy_providers


def find_title_for_track(track_id: str, session: Any) -> Any:
    """Find the title object that owns a given track."""
    for t_id, tracks_dict in session.tracks_by_title.items():
        if track_id in tracks_dict:
            return session.title_map.get(t_id)
    if session.title_map:
        return next(iter(session.title_map.values()))
    return None


def extract_pssh_from_track(track: Any, drm_type: str) -> Optional[str]:
    """Extract PSSH base64 string from a track's DRM objects."""
    if not track.drm:
        return None
    pssh_b64 = None
    for drm_obj in track.drm:
        drm_class = drm_obj.__class__.__name__
        if drm_class == "Widevine" and hasattr(drm_obj, "_pssh") and drm_obj._pssh:
            if hasattr(drm_obj._pssh, "dumps"):
                pssh_b64 = drm_obj._pssh.dumps()
                if drm_type == "widevine":
                    break
        elif drm_class == "PlayReady":
            if hasattr(drm_obj, "data") and drm_obj.data.get("pssh_b64"):
                pssh_b64 = drm_obj.data["pssh_b64"]
                if drm_type == "playready":
                    break
    return pssh_b64


def ensure_track_drm(track: Any) -> None:
    """Extract DRM from manifest data if track has none.

    Supports DASH (ContentProtection elements), HLS (EXT-X-KEY from
    playlist fetch), and ISM (ProtectionHeader elements).
    """
    if track.drm:
        return

    if track.data.get("dash"):
        from unshackle.core.manifests import DASH as DASHManifest

        rep = track.data["dash"].get("representation")
        ada = track.data["dash"].get("adaptation_set")
        if rep is not None and ada is not None:
            track.drm = DASHManifest.get_drm(rep.findall("ContentProtection") + ada.findall("ContentProtection"))
            if track.drm:
                return

    if track.data.get("hls") and track.url:
        try:
            import m3u8

            from unshackle.core.drm import PlayReady, Widevine
            from unshackle.core.manifests import HLS

            playlist = m3u8.load(track.url)
            keys = [k for k in (playlist.keys or []) + (playlist.session_keys or []) if k is not None]
            for key in keys:
                try:
                    drm = HLS.get_drm(key)
                    if isinstance(drm, (Widevine, PlayReady)):
                        track.drm = [drm]
                        return
                # EXT-X-KEY data is third-party input; skip keys we cannot turn into a DRM object
                except Exception as e:
                    log.debug(f"Skipping HLS key {key}: {e!r}")
                    continue
        except Exception as e:
            log.debug(f"HLS DRM prefetch failed for {track.id}: {e!r}")

    if track.data.get("ism"):
        try:
            from unshackle.core.manifests import ISM as ISMManifest

            manifest = track.data["ism"].get("manifest")
            if manifest is not None:
                track.drm = ISMManifest.get_drm(manifest.xpath(".//ProtectionHeader"))
        # a swallowed error here means the track silently gets no DRM, so it must be loud
        except Exception as e:
            log.warning(f"ISM DRM extraction failed for {track.id}: {e!r}")


def resolve_device_name(user_config: dict, drm_type: str, service_tag: str = "") -> str:
    """Get the CDM device name, checking service-specific config.cdm first.

    Resolution order:
    1. config.cdm[service_tag] (service-specific CDM mapping)
    2. serve.users.{key}.devices / playready_devices (user device list)
    """
    from unshackle.core.config import config as app_config

    cdm_name = ci_get(app_config.cdm, service_tag) if service_tag else None
    if isinstance(cdm_name, dict):
        drm_key = {"widevine": "widevine", "playready": "playready"}.get(drm_type)
        lower_keys = {k.lower(): v for k, v in cdm_name.items()}
        cdm_name = lower_keys.get(drm_key) or lower_keys.get("default") or ci_get(app_config.cdm, "default")
    if cdm_name and isinstance(cdm_name, str):
        return cdm_name

    if drm_type == "playready":
        device_name = (user_config.get("playready_devices") or [None])[0]
        if not device_name:
            raise APIError(APIErrorCode.INVALID_INPUT, "No PlayReady device configured for this API key")
    else:
        device_name = (user_config.get("devices") or [None])[0]
        if not device_name:
            raise APIError(APIErrorCode.INVALID_INPUT, "No Widevine device configured for this API key")
    return device_name


def load_server_vaults(service_name: str) -> Any:
    """Load server vaults from config.key_vaults."""
    from unshackle.core.config import config as app_config
    from unshackle.core.services import Services
    from unshackle.core.vaults import Vaults

    vaults = Vaults(Services.get_vault_tag(service_name))
    for vault_config in app_config.key_vaults:
        cfg = vault_config.copy()
        vault_type = cfg.pop("type", None)
        if vault_type:
            try:
                vaults.load(vault_type, **cfg)
            except (Exception, SystemExit) as e:
                log.warning(f"Could not load vault '{vault_type}': {e}")
    return vaults


def check_vaults(kids: list, service_name: str) -> Optional[Dict[str, str]]:
    """Examine the server vaults for existing content keys that match all KIDs.

    Returns a `KID:KEY` dict if ALL KIDs are found, None otherwise.
    """
    from uuid import UUID

    try:
        vaults = load_server_vaults(service_name)
        if not vaults.vaults:
            return None
        keys: Dict[str, str] = {}
        for kid in kids:
            kid_uuid = kid if isinstance(kid, UUID) else UUID(hex=str(kid))
            content_key, vault_used = vaults.get_key(kid_uuid)
            if content_key:
                keys[kid_uuid.hex] = content_key
            else:
                return None
        if keys:
            log.info(f"Vault hit: {len(keys)} key(s) from server vaults, skipping CDM")
            return keys
    # vault lookup is a best-effort shortcut before the CDM; any failure falls through to licensing
    except Exception as e:
        log.debug(f"Server vault lookup failed: {e!r}")
    return None


def cache_to_vaults(keys: Dict[str, str], service_name: str) -> None:
    """Cache newly obtained keys to server vaults."""
    from uuid import UUID

    try:
        vaults = load_server_vaults(service_name)
        if not vaults.vaults:
            return

        key_map = {UUID(hex=kid): key for kid, key in keys.items()}
        cached = vaults.add_keys(key_map)
        if cached:
            log.info(f"Cached {cached} key(s) to {cached} server vault(s)")
    except (Exception, SystemExit) as e:
        log.warning(f"Failed to cache keys to vaults: {e}")


def handle_single_server_cdm(
    service: Any,
    title: Any,
    track: Any,
    pssh_b64: Optional[str],
    drm_type: str,
    request: Optional[web.Request],
) -> Dict[str, str]:
    """Do the single-track server_cdm licensing with the DRM class get_content_keys() flow."""
    import base64

    from unshackle.core.cdm import load_cdm
    from unshackle.core.config import config as app_config

    ensure_track_drm(track)

    if not pssh_b64:
        pssh_b64 = extract_pssh_from_track(track, drm_type)
    if not pssh_b64:
        raise APIError(APIErrorCode.INVALID_INPUT, "No PSSH available for server_cdm licensing")

    api_key = request.headers.get("X-Secret-Key", "anonymous") if request else "anonymous"
    user_config = app_config.serve.get("users", {}).get(api_key, {})

    if drm_type == "playready":
        from pyplayready.system.pssh import PSSH as PlayReadyPSSH

        from unshackle.core.drm import PlayReady

        pr_pssh = PlayReadyPSSH(base64.b64decode(pssh_b64))
        pr_drm = PlayReady(pssh=pr_pssh, pssh_b64=pssh_b64)

        # Gate on the caller's CDM device first so a caller with no device cannot
        # harvest server-side keys from the vault fallback below.
        device_name = resolve_device_name(user_config, drm_type, service.__class__.__name__)

        vault_keys = check_vaults(pr_drm.kids, service.__class__.__name__)
        if vault_keys:
            return vault_keys

        cdm = load_cdm(device_name, service_name=service.__class__.__name__)
        pr_drm.get_content_keys(
            cdm=cdm,
            certificate=lambda challenge, **_: None,
            licence=lambda challenge, **_: service.get_playready_license(challenge=challenge, title=title, track=track),
        )
        keys = {kid.hex: key for kid, key in pr_drm.content_keys.items()}
    elif drm_type == "widevine":
        from pywidevine.pssh import PSSH as WvPSSH

        from unshackle.core.drm import Widevine

        wv_pssh = WvPSSH(pssh_b64)
        wv_drm = Widevine(pssh=wv_pssh)

        # Gate on the caller's CDM device first so a caller with no device cannot
        # harvest server-side keys from the vault fallback below.
        device_name = resolve_device_name(user_config, drm_type, service.__class__.__name__)

        vault_keys = check_vaults(wv_drm.kids, service.__class__.__name__)
        if vault_keys:
            return vault_keys

        cdm = load_cdm(device_name, service_name=service.__class__.__name__)
        wv_drm.get_content_keys(
            cdm=cdm,
            certificate=lambda challenge, **_: service.get_widevine_service_certificate(
                challenge=challenge, title=title, track=track
            ),
            licence=lambda challenge, **_: service.get_widevine_license(challenge=challenge, title=title, track=track),
        )
        keys = {kid.hex: key for kid, key in wv_drm.content_keys.items()}
    else:
        raise APIError(
            APIErrorCode.INVALID_PARAMETERS,
            f"Unsupported DRM type for server_cdm: {drm_type}",
        )

    if not keys:
        raise APIError(APIErrorCode.NO_CONTENT, "Server CDM returned no content keys")

    cache_to_vaults(keys, service.__class__.__name__)
    return keys


def handle_proxy_license(
    service: Any,
    title: Any,
    track: Any,
    challenge_b64: Optional[str],
    drm_type: str,
) -> web.Response:
    """Forward a client CDM challenge to the service license endpoint."""
    import base64

    if not challenge_b64:
        raise APIError(APIErrorCode.INVALID_INPUT, "Missing required parameter: challenge")
    challenge_bytes = base64.b64decode(challenge_b64)

    if drm_type == "widevine":
        license_response = service.get_widevine_license(challenge=challenge_bytes, title=title, track=track)
    elif drm_type == "playready":
        license_response = service.get_playready_license(challenge=challenge_bytes, title=title, track=track)
    else:
        raise APIError(
            APIErrorCode.INVALID_PARAMETERS,
            f"Unsupported DRM type: {drm_type}",
            details={"drm_type": drm_type, "supported": ["widevine", "playready"]},
        )

    if isinstance(license_response, str):
        license_response = license_response.encode("utf-8")

    return web.json_response({"license": base64.b64encode(license_response).decode("ascii")})


async def session_segment_filter_handler(
    data: Dict[str, Any], session_id: str, request: Optional[web.Request] = None
) -> web.Response:
    """Run the service's HLS ``OnSegmentFilter`` for one track and return the unwanted segment URIs.

    The client fetches the same media playlist again. Only the URIs leave the server; the
    filter and the data it reads stay on it.
    """
    session = await get_validated_session(session_id, request)

    track_id = data.get("track_id")
    if not track_id:
        raise APIError(APIErrorCode.INVALID_INPUT, "Missing required parameter: track_id")

    track = session.tracks.get(track_id)
    if not track:
        raise APIError(
            APIErrorCode.TRACK_NOT_FOUND,
            f"Track not found in session: {track_id}",
            details={"track_id": track_id, "session_id": session_id},
        )

    segment_filter = getattr(track, "OnSegmentFilter", None)
    if not callable(segment_filter) or not getattr(track, "url", None):
        return web.json_response({"unwanted": None})

    try:
        import m3u8
        import requests

        response = session.service_instance.session.get(track.url)
        if not getattr(response, "ok", True):
            raise ValueError(f"playlist request failed: {response.status_code}")
        if isinstance(response, requests.Response):
            response.encoding = response.encoding or "utf-8"
        playlist = m3u8.loads(response.text, uri=str(track.url))
        unwanted = [segment.absolute_uri for segment in playlist.segments if segment_filter(segment)]
    except (Exception, SystemExit):
        # `from None`: a debug-mode traceback would otherwise carry the service's exception chain
        log.exception(f"Error running the segment filter for track {sanitize_log(str(track_id))}")
        raise APIError(APIErrorCode.SERVICE_ERROR, "Could not run the segment filter for this track") from None

    return web.json_response({"unwanted": unwanted})


async def session_license_handler(
    data: Dict[str, Any], session_id: str, request: Optional[web.Request] = None
) -> web.Response:
    """Do the DRM licensing in proxy or server_cdm mode.

    Proxy mode (default): forwards client CDM challenge to the service's
    license endpoint, returns raw license bytes for client-side parsing.

    Server-CDM mode (mode="server_cdm"): server uses its own CDM to make
    the challenge, get the license, and extract `KID:KEY` pairs. Supports
    batch (track_ids list) and single-track requests.
    """
    import base64

    session = await get_validated_session(session_id, request)

    track_id = data.get("track_id")
    track_ids = data.get("track_ids")
    challenge_b64 = data.get("challenge")
    drm_type = data.get("drm_type", "widevine")
    mode = data.get("mode", "proxy")

    if mode == "server_cdm" and not server_cdm_allowed(request, session.service_tag):
        raise APIError(
            APIErrorCode.FORBIDDEN,
            "Server CDM licensing is not enabled for this key on this service. Use a local CDM (proxy mode).",
        )

    if mode == "server_cdm" and track_ids:
        from unshackle.core.config import config as app_config

        api_key = request.headers.get("X-Secret-Key", "anonymous") if request else "anonymous"
        user_config = app_config.serve.get("users", {}).get(api_key, {})
        service = session.service_instance
        has_wv_device = bool(user_config.get("devices"))
        has_pr_device = bool(user_config.get("playready_devices"))

        service_tag = session.service_tag
        config_cdm_type = detect_cdm_type_for_service(service_tag, app_config)

        all_keys: Dict[str, Dict[str, str]] = {}
        drm_types: Dict[str, str] = {}
        seen_pssh: set[str] = set()
        actual_drm_type: Optional[str] = None

        for tid in track_ids:
            track = session.tracks.get(tid)
            if not track:
                continue

            ensure_track_drm(track)
            if not track.drm:
                continue

            title = find_title_for_track(tid, session)

            if config_cdm_type == "playready":
                candidates = ["playready", "widevine"]
            elif config_cdm_type == "widevine":
                candidates = ["widevine", "playready"]
            else:
                candidates = [
                    name
                    for name, has_device in (("widevine", has_wv_device), ("playready", has_pr_device))
                    if has_device
                ]

            preferred = drm_preference_name(track)
            if preferred:
                if preferred in candidates:
                    candidates.remove(preferred)
                    candidates.insert(0, preferred)
                else:
                    log.warning(
                        f"Track {sanitize_log(tid[:12])} wants {preferred} DRM "
                        "but the server has no device for it, using the configured DRM instead"
                    )

            track_drm_type = None
            pssh_str = None
            for candidate in candidates:
                pssh_str = extract_pssh_from_track(track, candidate)
                if pssh_str:
                    track_drm_type = candidate
                    break

            if not pssh_str or not track_drm_type:
                continue

            if pssh_str in seen_pssh:
                for prev_tid, prev_keys in all_keys.items():
                    if prev_keys:
                        all_keys[tid] = prev_keys
                        if prev_tid in drm_types:
                            drm_types[tid] = drm_types[prev_tid]
                        break
                continue
            seen_pssh.add(pssh_str)

            try:
                keys = handle_single_server_cdm(service, title, track, pssh_str, track_drm_type, request)
                if keys:
                    all_keys[tid] = keys
                    if track_drm_type:
                        drm_types[tid] = track_drm_type
                        actual_drm_type = track_drm_type
            except SystemExit:
                log.warning(f"Service exited while resolving keys for track {sanitize_log(tid[:12])}, skipping")
            except (Exception, SystemExit) as e:
                log.warning(f"Failed to resolve keys for track {sanitize_log(tid[:12])}: {e}")

        response: Dict[str, Any] = {"keys": all_keys}
        if actual_drm_type:
            response["drm_type"] = actual_drm_type
        if drm_types:
            response["drm_types"] = drm_types
        return web.json_response(response)

    if not track_id:
        raise APIError(APIErrorCode.INVALID_INPUT, "Missing required parameter: track_id")

    track = session.tracks.get(track_id)
    if not track:
        raise APIError(
            APIErrorCode.TRACK_NOT_FOUND,
            f"Track not found in session: {track_id}",
            details={"track_id": track_id, "session_id": session_id},
        )

    try:
        title = find_title_for_track(track_id, session)
        service = session.service_instance

        pssh_b64 = data.get("pssh")
        if pssh_b64:
            if not track.drm:
                track.drm = []
            if drm_type == "playready":
                track.pr_pssh = pssh_b64
                from pyplayready.system.pssh import PSSH as PlayReadyPSSH

                from unshackle.core.drm import PlayReady

                pr_pssh = PlayReadyPSSH(base64.b64decode(pssh_b64))
                pr_drm = PlayReady(pssh=pr_pssh, pssh_b64=pssh_b64)
                track.drm.append(pr_drm)
            elif drm_type == "widevine":
                from pywidevine.pssh import PSSH as WidevinePSSH

                from unshackle.core.drm import Widevine

                wv_pssh = WidevinePSSH(pssh_b64)
                wv_drm = Widevine(pssh=wv_pssh)
                track.drm.append(wv_drm)

        if mode == "server_cdm":
            keys = handle_single_server_cdm(service, title, track, pssh_b64, drm_type, request)
            log.info(f"Server CDM resolved {len(keys)} key(s) for track {sanitize_log(track_id[:12])}")
            return web.json_response({"keys": keys})

        return handle_proxy_license(service, title, track, challenge_b64, drm_type)

    except APIError:
        raise
    except SystemExit:
        raise APIError(APIErrorCode.SERVICE_ERROR, "Service exited during license request")
    except (Exception, SystemExit) as e:
        log.exception(f"Error proxying license for track {sanitize_log(track_id)}")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={
                "operation": "session_license",
                "session_id": session_id,
                "track_id": track_id,
                "drm_type": drm_type,
            },
            debug_mode=debug_mode,
        )


async def session_info_handler(session_id: str, request: Optional[web.Request] = None) -> web.Response:
    """Make sure that the remote session is valid, and get the remote session info."""
    session = await get_validated_session(session_id, request)

    from unshackle.core.api.session_store import get_session_store

    return web.json_response(
        {
            "session_id": session.session_id,
            "service": session.service_tag,
            "valid": True,
            "expires_in": get_session_store().ttl,
            "track_count": len(session.tracks),
            "title_count": len(session.title_map),
        }
    )


async def session_delete_handler(session_id: str, request: Optional[web.Request] = None) -> web.Response:
    """Delete a remote session, return updated cache files, and clean up server-side data."""
    import base64
    import zlib

    from unshackle.core.api.session_store import get_session_store
    from unshackle.core.config import config as app_config

    session = await get_validated_session(session_id, request)
    store = get_session_store()

    if session.input_bridge:
        session.input_bridge.cancel()

    cache_tag = session.cache_tag
    cache_data: Dict[str, str] = {}
    if cache_tag:
        cache_dir = app_config.directories.cache / cache_tag
        if cache_dir.is_dir():
            for f in cache_dir.glob("*.json"):
                if not f.stem.startswith("titles_"):
                    try:
                        cache_data[f.stem] = base64.b64encode(zlib.compress(f.read_bytes())).decode("ascii")
                    except OSError:
                        pass

    await store.delete(session_id)

    response: Dict[str, Any] = {"status": "ok"}
    if cache_data:
        response["cache"] = cache_data
    return web.json_response(response)
