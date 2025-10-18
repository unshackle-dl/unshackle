import logging
from typing import Any, Dict, List, Optional

from aiohttp import web

from unshackle.core.constants import AUDIO_CODEC_MAP, DYNAMIC_RANGE_MAP, VIDEO_CODEC_MAP
from unshackle.core.proxies.basic import Basic
from unshackle.core.proxies.hola import Hola
from unshackle.core.proxies.nordvpn import NordVPN
from unshackle.core.proxies.surfsharkvpn import SurfsharkVPN
from unshackle.core.services import Services
from unshackle.core.titles import Episode, Movie, Title_T
from unshackle.core.tracks import Audio, Subtitle, Video

log = logging.getLogger("api")


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


def serialize_video_track(track: Video) -> Dict[str, Any]:
    """Convert video track to JSON-serializable dict."""
    codec_name = track.codec.name if hasattr(track.codec, "name") else str(track.codec)
    range_name = track.range.name if hasattr(track.range, "name") else str(track.range)

    return {
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
        "drm": str(track.drm) if hasattr(track, "drm") and track.drm else None,
    }


def serialize_audio_track(track: Audio) -> Dict[str, Any]:
    """Convert audio track to JSON-serializable dict."""
    codec_name = track.codec.name if hasattr(track.codec, "name") else str(track.codec)

    return {
        "id": str(track.id),
        "codec": codec_name,
        "codec_display": AUDIO_CODEC_MAP.get(codec_name, codec_name),
        "bitrate": int(track.bitrate / 1000) if track.bitrate else None,
        "channels": track.channels if track.channels else None,
        "language": str(track.language) if track.language else None,
        "atmos": track.atmos if hasattr(track, "atmos") else False,
        "descriptive": track.descriptive if hasattr(track, "descriptive") else False,
        "drm": str(track.drm) if hasattr(track, "drm") and track.drm else None,
    }


def serialize_subtitle_track(track: Subtitle) -> Dict[str, Any]:
    """Convert subtitle track to JSON-serializable dict."""
    return {
        "id": str(track.id),
        "codec": track.codec.name if hasattr(track.codec, "name") else str(track.codec),
        "language": str(track.language) if track.language else None,
        "forced": track.forced if hasattr(track, "forced") else False,
        "sdh": track.sdh if hasattr(track, "sdh") else False,
        "cc": track.cc if hasattr(track, "cc") else False,
    }


async def list_titles_handler(data: Dict[str, Any]) -> web.Response:
    """Handle list-titles request."""
    service_tag = data.get("service")
    title_id = data.get("title_id")
    profile = data.get("profile")

    if not service_tag:
        return web.json_response({"status": "error", "message": "Missing required parameter: service"}, status=400)

    if not title_id:
        return web.json_response({"status": "error", "message": "Missing required parameter: title_id"}, status=400)

    normalized_service = validate_service(service_tag)
    if not normalized_service:
        return web.json_response(
            {"status": "error", "message": f"Invalid or unavailable service: {service_tag}"}, status=400
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
                return web.json_response({"status": "error", "message": f"Proxy error: {e}"}, status=400)

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

    except Exception as e:
        log.exception("Error listing titles")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def list_tracks_handler(data: Dict[str, Any]) -> web.Response:
    """Handle list-tracks request."""
    service_tag = data.get("service")
    title_id = data.get("title_id")
    profile = data.get("profile")

    if not service_tag:
        return web.json_response({"status": "error", "message": "Missing required parameter: service"}, status=400)

    if not title_id:
        return web.json_response({"status": "error", "message": "Missing required parameter: title_id"}, status=400)

    normalized_service = validate_service(service_tag)
    if not normalized_service:
        return web.json_response(
            {"status": "error", "message": f"Invalid or unavailable service: {service_tag}"}, status=400
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
                return web.json_response({"status": "error", "message": f"Proxy error: {e}"}, status=400)

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
                    return web.json_response(
                        {"status": "error", "message": f"Invalid wanted parameter: {e}"}, status=400
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
                    return web.json_response(
                        {"status": "error", "message": "No episodes found matching wanted criteria"}, status=404
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
                        return web.json_response(
                            {
                                "status": "error",
                                "message": f"No available episodes found. Unavailable: {', '.join(failed_episodes)}",
                            },
                            status=404,
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

    except Exception as e:
        log.exception("Error listing tracks")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def download_handler(data: Dict[str, Any]) -> web.Response:
    """Handle download request - create and queue a download job."""
    from unshackle.core.api.download_manager import get_download_manager

    service_tag = data.get("service")
    title_id = data.get("title_id")

    if not service_tag:
        return web.json_response({"status": "error", "message": "Missing required parameter: service"}, status=400)

    if not title_id:
        return web.json_response({"status": "error", "message": "Missing required parameter: title_id"}, status=400)

    normalized_service = validate_service(service_tag)
    if not normalized_service:
        return web.json_response(
            {"status": "error", "message": f"Invalid or unavailable service: {service_tag}"}, status=400
        )

    try:
        # Get download manager and start workers if needed
        manager = get_download_manager()
        await manager.start_workers()

        # Create download job with filtered parameters (exclude service and title_id as they're already passed)
        filtered_params = {k: v for k, v in data.items() if k not in ["service", "title_id"]}
        job = manager.create_job(normalized_service, title_id, **filtered_params)

        return web.json_response(
            {"job_id": job.job_id, "status": job.status.value, "created_time": job.created_time.isoformat()}, status=202
        )

    except Exception as e:
        log.exception("Error creating download job")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def list_download_jobs_handler(data: Dict[str, Any]) -> web.Response:
    """Handle list download jobs request."""
    from unshackle.core.api.download_manager import get_download_manager

    try:
        manager = get_download_manager()
        jobs = manager.list_jobs()

        job_list = [job.to_dict(include_full_details=False) for job in jobs]

        return web.json_response({"jobs": job_list})

    except Exception as e:
        log.exception("Error listing download jobs")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def get_download_job_handler(job_id: str) -> web.Response:
    """Handle get specific download job request."""
    from unshackle.core.api.download_manager import get_download_manager

    try:
        manager = get_download_manager()
        job = manager.get_job(job_id)

        if not job:
            return web.json_response({"status": "error", "message": "Job not found"}, status=404)

        return web.json_response(job.to_dict(include_full_details=True))

    except Exception as e:
        log.exception(f"Error getting download job {job_id}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def cancel_download_job_handler(job_id: str) -> web.Response:
    """Handle cancel download job request."""
    from unshackle.core.api.download_manager import get_download_manager

    try:
        manager = get_download_manager()

        if not manager.get_job(job_id):
            return web.json_response({"status": "error", "message": "Job not found"}, status=404)

        success = manager.cancel_job(job_id)

        if success:
            return web.json_response({"status": "success", "message": "Job cancelled"})
        else:
            return web.json_response({"status": "error", "message": "Job cannot be cancelled"}, status=400)

    except Exception as e:
        log.exception(f"Error cancelling download job {job_id}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)
