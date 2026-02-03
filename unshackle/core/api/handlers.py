import logging
from typing import Any, Dict, List, Optional

from aiohttp import web

from unshackle.core.api.errors import APIError, APIErrorCode, handle_api_exception
from unshackle.core.constants import AUDIO_CODEC_MAP, DYNAMIC_RANGE_MAP, VIDEO_CODEC_MAP
from unshackle.core.proxies.basic import Basic
from unshackle.core.proxies.hola import Hola
from unshackle.core.proxies.nordvpn import NordVPN
from unshackle.core.proxies.surfsharkvpn import SurfsharkVPN
from unshackle.core.services import Services
from unshackle.core.titles import Episode, Movie, Title_T
from unshackle.core.tracks import Audio, Subtitle, Video

log = logging.getLogger("api")

DEFAULT_DOWNLOAD_PARAMS = {
    "profile": None,
    "quality": [],
    "vcodec": None,
    "acodec": None,
    "vbitrate": None,
    "abitrate": None,
    "range": ["SDR"],
    "channels": None,
    "no_atmos": False,
    "wanted": [],
    "latest_episode": False,
    "lang": ["orig"],
    "v_lang": [],
    "a_lang": [],
    "s_lang": ["all"],
    "require_subs": [],
    "forced_subs": False,
    "exact_lang": False,
    "sub_format": None,
    "video_only": False,
    "audio_only": False,
    "subs_only": False,
    "chapters_only": False,
    "no_subs": False,
    "no_audio": False,
    "no_chapters": False,
    "audio_description": False,
    "slow": False,
    "skip_dl": False,
    "export": None,
    "cdm_only": None,
    "no_proxy": False,
    "no_folder": False,
    "no_source": False,
    "no_mux": False,
    "workers": None,
    "downloads": 1,
    "best_available": False,
}


def initialize_proxy_providers() -> List[Any]:
    """Initialize and return available proxy providers."""
    proxy_providers = []
    try:
        from unshackle.core import binaries
        # Load the main unshackle config to get proxy provider settings
        from unshackle.core.config import config as main_config

        log.debug(f"Main config proxy providers: {getattr(main_config, 'proxy_providers', {})}")
        log.debug(f"Available proxy provider configs: {list(getattr(main_config, 'proxy_providers', {}).keys())}")

        # Use main_config instead of the service-specific config for proxy providers
        proxy_config = getattr(main_config, "proxy_providers", {})

        if proxy_config.get("basic"):
            log.debug("Loading Basic proxy provider")
            proxy_providers.append(Basic(**proxy_config["basic"]))
        if proxy_config.get("nordvpn"):
            log.debug("Loading NordVPN proxy provider")
            proxy_providers.append(NordVPN(**proxy_config["nordvpn"]))
        if proxy_config.get("surfsharkvpn"):
            log.debug("Loading SurfsharkVPN proxy provider")
            proxy_providers.append(SurfsharkVPN(**proxy_config["surfsharkvpn"]))
        if hasattr(binaries, "HolaProxy") and binaries.HolaProxy:
            log.debug("Loading Hola proxy provider")
            proxy_providers.append(Hola())

        for proxy_provider in proxy_providers:
            log.info(f"Loaded {proxy_provider.__class__.__name__}: {proxy_provider}")

        if not proxy_providers:
            log.warning("No proxy providers were loaded. Check your proxy provider configuration in unshackle.yaml")

    except Exception as e:
        log.warning(f"Failed to initialize some proxy providers: {e}")

    return proxy_providers


def resolve_proxy(proxy: str, proxy_providers: List[Any]) -> str:
    """Resolve proxy parameter to actual proxy URI."""
    import re

    if not proxy:
        return proxy

    # Check if explicit proxy URI
    if re.match(r"^https?://", proxy):
        return proxy

    # Handle provider:country format (e.g., "nordvpn:us")
    requested_provider = None
    if re.match(r"^[a-z]+:.+$", proxy, re.IGNORECASE):
        requested_provider, proxy = proxy.split(":", maxsplit=1)

    # Handle country code format (e.g., "us", "uk")
    if re.match(r"^[a-z]{2}(?:\d+)?$", proxy, re.IGNORECASE):
        proxy = proxy.lower()

        if requested_provider:
            # Find specific provider (case-insensitive matching)
            proxy_provider = next(
                (x for x in proxy_providers if x.__class__.__name__.lower() == requested_provider.lower()),
                None,
            )
            if not proxy_provider:
                available_providers = [x.__class__.__name__ for x in proxy_providers]
                raise ValueError(
                    f"The proxy provider '{requested_provider}' was not recognized. Available providers: {available_providers}"
                )

            proxy_uri = proxy_provider.get_proxy(proxy)
            if not proxy_uri:
                raise ValueError(f"The proxy provider {requested_provider} had no proxy for {proxy}")

            log.info(f"Using {proxy_provider.__class__.__name__} Proxy: {proxy_uri}")
            return proxy_uri
        else:
            # Try all providers
            for proxy_provider in proxy_providers:
                proxy_uri = proxy_provider.get_proxy(proxy)
                if proxy_uri:
                    log.info(f"Using {proxy_provider.__class__.__name__} Proxy: {proxy_uri}")
                    return proxy_uri

            raise ValueError(f"No proxy provider had a proxy for {proxy}")

    # Return as-is if not recognized format
    log.info(f"Using explicit Proxy: {proxy}")
    return proxy


def validate_service(service_tag: str) -> Optional[str]:
    """Validate and normalize service tag."""
    try:
        normalized = Services.get_tag(service_tag)
        service_path = Services.get_path(normalized)
        if not service_path.exists():
            return None
        return normalized
    except Exception:
        return None


def serialize_title(title: Title_T) -> Dict[str, Any]:
    """Convert a title object to JSON-serializable dict."""
    if isinstance(title, Episode):
        episode_name = title.name if title.name else f"Episode {title.number:02d}"
        result = {
            "type": "episode",
            "name": episode_name,
            "series_title": str(title.title),
            "season": title.season,
            "number": title.number,
            "year": title.year,
            "id": str(title.id) if hasattr(title, "id") else None,
        }
    elif isinstance(title, Movie):
        result = {
            "type": "movie",
            "name": str(title.name) if hasattr(title, "name") else str(title),
            "year": title.year,
            "id": str(title.id) if hasattr(title, "id") else None,
        }
    else:
        result = {
            "type": "other",
            "name": str(title.name) if hasattr(title, "name") else str(title),
            "id": str(title.id) if hasattr(title, "id") else None,
        }

    return result


def serialize_drm(drm_list) -> Optional[List[Dict[str, Any]]]:
    """Serialize DRM objects to JSON-serializable list."""
    if not drm_list:
        return None

    if not isinstance(drm_list, list):
        drm_list = [drm_list]

    result = []
    for drm in drm_list:
        drm_info = {}
        drm_class = drm.__class__.__name__
        drm_info["type"] = drm_class.lower()

        # Get PSSH - handle both Widevine and PlayReady
        if hasattr(drm, "_pssh") and drm._pssh:
            try:
                pssh_obj = drm._pssh
                # Try to get base64 representation
                if hasattr(pssh_obj, "dumps"):
                    # pywidevine PSSH has dumps() method
                    drm_info["pssh"] = pssh_obj.dumps()
                elif hasattr(pssh_obj, "__bytes__"):
                    # Convert to base64
                    import base64
                    drm_info["pssh"] = base64.b64encode(bytes(pssh_obj)).decode()
                elif hasattr(pssh_obj, "to_base64"):
                    drm_info["pssh"] = pssh_obj.to_base64()
                else:
                    # Fallback - str() works for pywidevine PSSH
                    pssh_str = str(pssh_obj)
                    # Check if it's already base64-like or an object repr
                    if not pssh_str.startswith("<"):
                        drm_info["pssh"] = pssh_str
            except Exception:
                pass

        # Get KIDs
        if hasattr(drm, "kids") and drm.kids:
            drm_info["kids"] = [str(kid) for kid in drm.kids]

        # Get content keys if available
        if hasattr(drm, "content_keys") and drm.content_keys:
            drm_info["content_keys"] = {str(k): v for k, v in drm.content_keys.items()}

        # Get license URL - essential for remote licensing
        if hasattr(drm, "license_url") and drm.license_url:
            drm_info["license_url"] = str(drm.license_url)
        elif hasattr(drm, "_license_url") and drm._license_url:
            drm_info["license_url"] = str(drm._license_url)

        result.append(drm_info)

    return result if result else None


def serialize_video_track(track: Video, include_url: bool = False) -> Dict[str, Any]:
    """Convert video track to JSON-serializable dict."""
    codec_name = track.codec.name if hasattr(track.codec, "name") else str(track.codec)
    range_name = track.range.name if hasattr(track.range, "name") else str(track.range)

    # Get descriptor for N_m3u8DL-RE compatibility (HLS, DASH, URL, etc.)
    descriptor_name = None
    if hasattr(track, "descriptor") and track.descriptor:
        descriptor_name = track.descriptor.name if hasattr(track.descriptor, "name") else str(track.descriptor)

    result = {
        "id": str(track.id),
        "codec": codec_name,
        "codec_display": VIDEO_CODEC_MAP.get(codec_name, codec_name),
        "bitrate": int(track.bitrate / 1000) if track.bitrate else None,
        "width": track.width,
        "height": track.height,
        "resolution": f"{track.width}x{track.height}" if track.width and track.height else None,
        "fps": track.fps if track.fps else None,
        "range": range_name,
        "range_display": DYNAMIC_RANGE_MAP.get(range_name, range_name),
        "language": str(track.language) if track.language else None,
        "drm": serialize_drm(track.drm) if hasattr(track, "drm") and track.drm else None,
        "descriptor": descriptor_name,
    }
    if include_url and hasattr(track, "url") and track.url:
        result["url"] = str(track.url)
    return result


def serialize_audio_track(track: Audio, include_url: bool = False) -> Dict[str, Any]:
    """Convert audio track to JSON-serializable dict."""
    codec_name = track.codec.name if hasattr(track.codec, "name") else str(track.codec)

    # Get descriptor for N_m3u8DL-RE compatibility
    descriptor_name = None
    if hasattr(track, "descriptor") and track.descriptor:
        descriptor_name = track.descriptor.name if hasattr(track.descriptor, "name") else str(track.descriptor)

    result = {
        "id": str(track.id),
        "codec": codec_name,
        "codec_display": AUDIO_CODEC_MAP.get(codec_name, codec_name),
        "bitrate": int(track.bitrate / 1000) if track.bitrate else None,
        "channels": track.channels if track.channels else None,
        "language": str(track.language) if track.language else None,
        "atmos": track.atmos if hasattr(track, "atmos") else False,
        "descriptive": track.descriptive if hasattr(track, "descriptive") else False,
        "drm": serialize_drm(track.drm) if hasattr(track, "drm") and track.drm else None,
        "descriptor": descriptor_name,
    }
    if include_url and hasattr(track, "url") and track.url:
        result["url"] = str(track.url)
    return result


def serialize_subtitle_track(track: Subtitle, include_url: bool = False) -> Dict[str, Any]:
    """Convert subtitle track to JSON-serializable dict."""
    # Get descriptor for compatibility
    descriptor_name = None
    if hasattr(track, "descriptor") and track.descriptor:
        descriptor_name = track.descriptor.name if hasattr(track.descriptor, "name") else str(track.descriptor)

    result = {
        "id": str(track.id),
        "codec": track.codec.name if hasattr(track.codec, "name") else str(track.codec),
        "language": str(track.language) if track.language else None,
        "forced": track.forced if hasattr(track, "forced") else False,
        "sdh": track.sdh if hasattr(track, "sdh") else False,
        "cc": track.cc if hasattr(track, "cc") else False,
        "descriptor": descriptor_name,
    }
    if include_url and hasattr(track, "url") and track.url:
        result["url"] = str(track.url)
    return result


async def list_titles_handler(data: Dict[str, Any], request: Optional[web.Request] = None) -> web.Response:
    """Handle list-titles request."""
    service_tag = data.get("service")
    title_id = data.get("title_id")
    profile = data.get("profile")

    if not service_tag:
        raise APIError(
            APIErrorCode.INVALID_INPUT,
            "Missing required parameter: service",
            details={"missing_parameter": "service"},
        )

    if not title_id:
        raise APIError(
            APIErrorCode.INVALID_INPUT,
            "Missing required parameter: title_id",
            details={"missing_parameter": "title_id"},
        )

    normalized_service = validate_service(service_tag)
    if not normalized_service:
        raise APIError(
            APIErrorCode.INVALID_SERVICE,
            f"Invalid or unavailable service: {service_tag}",
            details={"service": service_tag},
        )

    try:
        import inspect

        import click
        import yaml

        from unshackle.commands.dl import dl
        from unshackle.core.config import config
        from unshackle.core.utils.click_types import ContextData
        from unshackle.core.utils.collections import merge_dict

        service_config_path = Services.get_path(normalized_service) / config.filenames.config
        if service_config_path.exists():
            service_config = yaml.safe_load(service_config_path.read_text(encoding="utf8"))
        else:
            service_config = {}
        merge_dict(config.services.get(normalized_service), service_config)

        @click.command()
        @click.pass_context
        def dummy_service(ctx: click.Context) -> None:
            pass

        # Handle proxy configuration
        proxy_param = data.get("proxy")
        no_proxy = data.get("no_proxy", False)
        proxy_providers = []

        if not no_proxy:
            proxy_providers = initialize_proxy_providers()

        if proxy_param and not no_proxy:
            try:
                resolved_proxy = resolve_proxy(proxy_param, proxy_providers)
                proxy_param = resolved_proxy
            except ValueError as e:
                raise APIError(
                    APIErrorCode.INVALID_PROXY,
                    f"Proxy error: {e}",
                    details={"proxy": proxy_param, "service": normalized_service},
                )

        ctx = click.Context(dummy_service)
        ctx.obj = ContextData(config=service_config, cdm=None, proxy_providers=proxy_providers, profile=profile)
        ctx.params = {"proxy": proxy_param, "no_proxy": no_proxy}

        service_module = Services.load(normalized_service)

        dummy_service.name = normalized_service
        dummy_service.params = [click.Argument([title_id], type=str)]
        ctx.invoked_subcommand = normalized_service

        service_ctx = click.Context(dummy_service, parent=ctx)
        service_ctx.obj = ctx.obj

        service_kwargs = {"title": title_id}

        # Add additional parameters from request data
        for key, value in data.items():
            if key not in ["service", "title_id", "profile", "season", "episode", "wanted", "proxy", "no_proxy"]:
                service_kwargs[key] = value

        # Get service parameter info and click command defaults
        service_init_params = inspect.signature(service_module.__init__).parameters

        # Extract default values from the click command
        if hasattr(service_module, "cli") and hasattr(service_module.cli, "params"):
            for param in service_module.cli.params:
                if hasattr(param, "name") and param.name not in service_kwargs:
                    # Add default value if parameter is not already provided
                    if hasattr(param, "default") and param.default is not None:
                        service_kwargs[param.name] = param.default

        # Handle required parameters that don't have click defaults
        for param_name, param_info in service_init_params.items():
            if param_name not in service_kwargs and param_name not in ["self", "ctx"]:
                # Check if parameter is required (no default value in signature)
                if param_info.default is inspect.Parameter.empty:
                    # Provide sensible defaults for common required parameters
                    if param_name == "meta_lang":
                        service_kwargs[param_name] = None
                    elif param_name == "movie":
                        service_kwargs[param_name] = False
                    else:
                        # Log warning for unknown required parameters
                        log.warning(f"Unknown required parameter '{param_name}' for service {normalized_service}")

        # Filter out any parameters that the service doesn't accept
        filtered_kwargs = {}
        for key, value in service_kwargs.items():
            if key in service_init_params:
                filtered_kwargs[key] = value

        service_instance = service_module(service_ctx, **filtered_kwargs)

        cookies = dl.get_cookie_jar(normalized_service, profile)
        credential = dl.get_credentials(normalized_service, profile)
        service_instance.authenticate(cookies, credential)

        titles = service_instance.get_titles()

        if hasattr(titles, "__iter__") and not isinstance(titles, str):
            title_list = [serialize_title(t) for t in titles]
        else:
            title_list = [serialize_title(titles)]

        return web.json_response({"titles": title_list})

    except APIError:
        raise
    except Exception as e:
        log.exception("Error listing titles")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "list_titles", "service": normalized_service, "title_id": title_id},
            debug_mode=debug_mode,
        )


async def list_tracks_handler(data: Dict[str, Any], request: Optional[web.Request] = None) -> web.Response:
    """Handle list-tracks request."""
    service_tag = data.get("service")
    title_id = data.get("title_id")
    profile = data.get("profile")

    if not service_tag:
        raise APIError(
            APIErrorCode.INVALID_INPUT,
            "Missing required parameter: service",
            details={"missing_parameter": "service"},
        )

    if not title_id:
        raise APIError(
            APIErrorCode.INVALID_INPUT,
            "Missing required parameter: title_id",
            details={"missing_parameter": "title_id"},
        )

    normalized_service = validate_service(service_tag)
    if not normalized_service:
        raise APIError(
            APIErrorCode.INVALID_SERVICE,
            f"Invalid or unavailable service: {service_tag}",
            details={"service": service_tag},
        )

    try:
        import inspect

        import click
        import yaml

        from unshackle.commands.dl import dl
        from unshackle.core.config import config
        from unshackle.core.utils.click_types import ContextData
        from unshackle.core.utils.collections import merge_dict

        service_config_path = Services.get_path(normalized_service) / config.filenames.config
        if service_config_path.exists():
            service_config = yaml.safe_load(service_config_path.read_text(encoding="utf8"))
        else:
            service_config = {}
        merge_dict(config.services.get(normalized_service), service_config)

        @click.command()
        @click.pass_context
        def dummy_service(ctx: click.Context) -> None:
            pass

        # Handle proxy configuration
        proxy_param = data.get("proxy")
        no_proxy = data.get("no_proxy", False)
        proxy_providers = []

        if not no_proxy:
            proxy_providers = initialize_proxy_providers()

        if proxy_param and not no_proxy:
            try:
                resolved_proxy = resolve_proxy(proxy_param, proxy_providers)
                proxy_param = resolved_proxy
            except ValueError as e:
                raise APIError(
                    APIErrorCode.INVALID_PROXY,
                    f"Proxy error: {e}",
                    details={"proxy": proxy_param, "service": normalized_service},
                )

        ctx = click.Context(dummy_service)
        ctx.obj = ContextData(config=service_config, cdm=None, proxy_providers=proxy_providers, profile=profile)
        ctx.params = {"proxy": proxy_param, "no_proxy": no_proxy}

        service_module = Services.load(normalized_service)

        dummy_service.name = normalized_service
        dummy_service.params = [click.Argument([title_id], type=str)]
        ctx.invoked_subcommand = normalized_service

        service_ctx = click.Context(dummy_service, parent=ctx)
        service_ctx.obj = ctx.obj

        service_kwargs = {"title": title_id}

        # Add additional parameters from request data
        for key, value in data.items():
            if key not in ["service", "title_id", "profile", "season", "episode", "wanted", "proxy", "no_proxy"]:
                service_kwargs[key] = value

        # Get service parameter info and click command defaults
        service_init_params = inspect.signature(service_module.__init__).parameters

        # Extract default values from the click command
        if hasattr(service_module, "cli") and hasattr(service_module.cli, "params"):
            for param in service_module.cli.params:
                if hasattr(param, "name") and param.name not in service_kwargs:
                    # Add default value if parameter is not already provided
                    if hasattr(param, "default") and param.default is not None:
                        service_kwargs[param.name] = param.default

        # Handle required parameters that don't have click defaults
        for param_name, param_info in service_init_params.items():
            if param_name not in service_kwargs and param_name not in ["self", "ctx"]:
                # Check if parameter is required (no default value in signature)
                if param_info.default is inspect.Parameter.empty:
                    # Provide sensible defaults for common required parameters
                    if param_name == "meta_lang":
                        service_kwargs[param_name] = None
                    elif param_name == "movie":
                        service_kwargs[param_name] = False
                    else:
                        # Log warning for unknown required parameters
                        log.warning(f"Unknown required parameter '{param_name}' for service {normalized_service}")

        # Filter out any parameters that the service doesn't accept
        filtered_kwargs = {}
        for key, value in service_kwargs.items():
            if key in service_init_params:
                filtered_kwargs[key] = value

        service_instance = service_module(service_ctx, **filtered_kwargs)

        cookies = dl.get_cookie_jar(normalized_service, profile)
        credential = dl.get_credentials(normalized_service, profile)
        service_instance.authenticate(cookies, credential)

        titles = service_instance.get_titles()

        wanted_param = data.get("wanted")
        season = data.get("season")
        episode = data.get("episode")

        if hasattr(titles, "__iter__") and not isinstance(titles, str):
            titles_list = list(titles)

            wanted = None
            if wanted_param:
                from unshackle.core.utils.click_types import SeasonRange

                try:
                    season_range = SeasonRange()
                    wanted = season_range.parse_tokens(wanted_param)
                    log.debug(f"Parsed wanted '{wanted_param}' into {len(wanted)} episodes: {wanted[:10]}...")
                except Exception as e:
                    raise APIError(
                        APIErrorCode.INVALID_PARAMETERS,
                        f"Invalid wanted parameter: {e}",
                        details={"wanted": wanted_param, "service": normalized_service},
                    )
            elif season is not None and episode is not None:
                wanted = [f"{season}x{episode}"]

            if wanted:
                # Filter titles based on wanted episodes, similar to how dl.py does it
                matching_titles = []
                log.debug(f"Filtering {len(titles_list)} titles with {len(wanted)} wanted episodes")
                for title in titles_list:
                    if isinstance(title, Episode):
                        episode_key = f"{title.season}x{title.number}"
                        if episode_key in wanted:
                            log.debug(f"Episode {episode_key} matches wanted list")
                            matching_titles.append(title)
                        else:
                            log.debug(f"Episode {episode_key} not in wanted list")
                    else:
                        matching_titles.append(title)

                log.debug(f"Found {len(matching_titles)} matching titles")

                if not matching_titles:
                    raise APIError(
                        APIErrorCode.NO_CONTENT,
                        "No episodes found matching wanted criteria",
                        details={
                            "service": normalized_service,
                            "title_id": title_id,
                            "wanted": wanted_param or f"{season}x{episode}",
                        },
                    )

                # If multiple episodes match, return tracks for all episodes
                if len(matching_titles) > 1 and all(isinstance(t, Episode) for t in matching_titles):
                    episodes_data = []
                    failed_episodes = []

                    # Sort matching titles by season and episode number for consistent ordering
                    sorted_titles = sorted(matching_titles, key=lambda t: (t.season, t.number))

                    for title in sorted_titles:
                        try:
                            tracks = service_instance.get_tracks(title)
                            video_tracks = sorted(tracks.videos, key=lambda t: t.bitrate or 0, reverse=True)
                            audio_tracks = sorted(tracks.audio, key=lambda t: t.bitrate or 0, reverse=True)

                            episode_data = {
                                "title": serialize_title(title),
                                "video": [serialize_video_track(t) for t in video_tracks],
                                "audio": [serialize_audio_track(t) for t in audio_tracks],
                                "subtitles": [serialize_subtitle_track(t) for t in tracks.subtitles],
                            }
                            episodes_data.append(episode_data)
                            log.debug(f"Successfully got tracks for {title.season}x{title.number}")
                        except SystemExit:
                            # Service calls sys.exit() for unavailable episodes - catch and skip
                            failed_episodes.append(f"S{title.season}E{title.number:02d}")
                            log.debug(f"Episode {title.season}x{title.number} not available, skipping")
                            continue
                        except Exception as e:
                            # Handle other errors gracefully
                            failed_episodes.append(f"S{title.season}E{title.number:02d}")
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
                    # Single episode or movie
                    first_title = matching_titles[0]
            else:
                first_title = titles_list[0]
        else:
            first_title = titles

        tracks = service_instance.get_tracks(first_title)

        video_tracks = sorted(tracks.videos, key=lambda t: t.bitrate or 0, reverse=True)
        audio_tracks = sorted(tracks.audio, key=lambda t: t.bitrate or 0, reverse=True)

        response = {
            "title": serialize_title(first_title),
            "video": [serialize_video_track(t) for t in video_tracks],
            "audio": [serialize_audio_track(t) for t in audio_tracks],
            "subtitles": [serialize_subtitle_track(t) for t in tracks.subtitles],
        }

        return web.json_response(response)

    except APIError:
        raise
    except Exception as e:
        log.exception("Error listing tracks")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "list_tracks", "service": normalized_service, "title_id": title_id},
            debug_mode=debug_mode,
        )


def validate_download_parameters(data: Dict[str, Any]) -> Optional[str]:
    """
    Validate download parameters and return error message if invalid.

    Returns:
        None if valid, error message string if invalid
    """
    if "vcodec" in data and data["vcodec"]:
        valid_vcodecs = ["H264", "H265", "VP9", "AV1"]
        if data["vcodec"].upper() not in valid_vcodecs:
            return f"Invalid vcodec: {data['vcodec']}. Must be one of: {', '.join(valid_vcodecs)}"

    if "acodec" in data and data["acodec"]:
        valid_acodecs = ["AAC", "AC3", "EC3", "EAC3", "DD", "DD+", "AC4", "OPUS", "FLAC", "ALAC", "VORBIS", "OGG", "DTS"]
        if isinstance(data["acodec"], str):
            acodec_values = [v.strip() for v in data["acodec"].split(",") if v.strip()]
        elif isinstance(data["acodec"], list):
            acodec_values = [str(v).strip() for v in data["acodec"] if str(v).strip()]
        else:
            return "acodec must be a string or list"

        invalid = [value for value in acodec_values if value.upper() not in valid_acodecs]
        if invalid:
            return f"Invalid acodec: {', '.join(invalid)}. Must be one of: {', '.join(valid_acodecs)}"

    if "sub_format" in data and data["sub_format"]:
        valid_sub_formats = ["SRT", "VTT", "ASS", "SSA"]
        if data["sub_format"].upper() not in valid_sub_formats:
            return f"Invalid sub_format: {data['sub_format']}. Must be one of: {', '.join(valid_sub_formats)}"

    if "vbitrate" in data and data["vbitrate"] is not None:
        if not isinstance(data["vbitrate"], int) or data["vbitrate"] <= 0:
            return "vbitrate must be a positive integer"

    if "abitrate" in data and data["abitrate"] is not None:
        if not isinstance(data["abitrate"], int) or data["abitrate"] <= 0:
            return "abitrate must be a positive integer"

    if "channels" in data and data["channels"] is not None:
        if not isinstance(data["channels"], (int, float)) or data["channels"] <= 0:
            return "channels must be a positive number"

    if "workers" in data and data["workers"] is not None:
        if not isinstance(data["workers"], int) or data["workers"] <= 0:
            return "workers must be a positive integer"

    if "downloads" in data and data["downloads"] is not None:
        if not isinstance(data["downloads"], int) or data["downloads"] <= 0:
            return "downloads must be a positive integer"

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

    if data.get("s_lang") and data.get("require_subs"):
        return "Cannot use both s_lang and require_subs"

    if "range" in data and data["range"]:
        valid_ranges = ["SDR", "HDR10", "HDR10+", "DV", "HLG"]
        if isinstance(data["range"], list):
            for r in data["range"]:
                if r.upper() not in valid_ranges:
                    return f"Invalid range value: {r}. Must be one of: {', '.join(valid_ranges)}"
        elif data["range"].upper() not in valid_ranges:
            return f"Invalid range value: {data['range']}. Must be one of: {', '.join(valid_ranges)}"

    return None


async def download_handler(data: Dict[str, Any], request: Optional[web.Request] = None) -> web.Response:
    """Handle download request - create and queue a download job."""
    from unshackle.core.api.download_manager import get_download_manager

    service_tag = data.get("service")
    title_id = data.get("title_id")

    if not service_tag:
        raise APIError(
            APIErrorCode.INVALID_INPUT,
            "Missing required parameter: service",
            details={"missing_parameter": "service"},
        )

    if not title_id:
        raise APIError(
            APIErrorCode.INVALID_INPUT,
            "Missing required parameter: title_id",
            details={"missing_parameter": "title_id"},
        )

    normalized_service = validate_service(service_tag)
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

    try:
        # Load service module to extract service-specific parameter defaults
        service_module = Services.load(normalized_service)
        service_specific_defaults = {}

        # Extract default values from the service's click command
        if hasattr(service_module, "cli") and hasattr(service_module.cli, "params"):
            for param in service_module.cli.params:
                if hasattr(param, "name") and hasattr(param, "default") and param.default is not None:
                    # Store service-specific defaults (e.g., drm_system, hydrate_track, profile for NF)
                    service_specific_defaults[param.name] = param.default

        # Get download manager and start workers if needed
        manager = get_download_manager()
        await manager.start_workers()

        # Create download job with filtered parameters (exclude service and title_id as they're already passed)
        filtered_params = {k: v for k, v in data.items() if k not in ["service", "title_id"]}
        # Merge defaults with provided parameters (user params override service defaults, which override global defaults)
        params_with_defaults = {**DEFAULT_DOWNLOAD_PARAMS, **service_specific_defaults, **filtered_params}
        job = manager.create_job(normalized_service, title_id, **params_with_defaults)

        return web.json_response(
            {"job_id": job.job_id, "status": job.status.value, "created_time": job.created_time.isoformat()}, status=202
        )

    except APIError:
        raise
    except Exception as e:
        log.exception("Error creating download job")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "create_download_job", "service": normalized_service, "title_id": title_id},
            debug_mode=debug_mode,
        )


async def list_download_jobs_handler(data: Dict[str, Any], request: Optional[web.Request] = None) -> web.Response:
    """Handle list download jobs request with optional filtering and sorting."""
    from unshackle.core.api.download_manager import get_download_manager

    try:
        manager = get_download_manager()
        jobs = manager.list_jobs()

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
            """Get the sorting key value, handling None values."""
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

        job_list = [job.to_dict(include_full_details=False) for job in jobs]

        return web.json_response({"jobs": job_list})

    except APIError:
        raise
    except Exception as e:
        log.exception("Error listing download jobs")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "list_download_jobs"},
            debug_mode=debug_mode,
        )


async def get_download_job_handler(job_id: str, request: Optional[web.Request] = None) -> web.Response:
    """Handle get specific download job request."""
    from unshackle.core.api.download_manager import get_download_manager

    try:
        manager = get_download_manager()
        job = manager.get_job(job_id)

        if not job:
            raise APIError(
                APIErrorCode.JOB_NOT_FOUND,
                "Job not found",
                details={"job_id": job_id},
            )

        return web.json_response(job.to_dict(include_full_details=True))

    except APIError:
        raise
    except Exception as e:
        log.exception(f"Error getting download job {job_id}")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "get_download_job", "job_id": job_id},
            debug_mode=debug_mode,
        )


async def cancel_download_job_handler(job_id: str, request: Optional[web.Request] = None) -> web.Response:
    """Handle cancel download job request."""
    from unshackle.core.api.download_manager import get_download_manager

    try:
        manager = get_download_manager()

        if not manager.get_job(job_id):
            raise APIError(
                APIErrorCode.JOB_NOT_FOUND,
                "Job not found",
                details={"job_id": job_id},
            )

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
    except Exception as e:
        log.exception(f"Error cancelling download job {job_id}")
        debug_mode = request.app.get("debug_api", False) if request else False
        return handle_api_exception(
            e,
            context={"operation": "cancel_download_job", "job_id": job_id},
            debug_mode=debug_mode,
        )
