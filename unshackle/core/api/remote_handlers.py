"""API handlers for remote service functionality."""

import http.cookiejar
import inspect
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import click
import yaml
from aiohttp import web

from unshackle.commands.dl import dl
from unshackle.core.api.handlers import (initialize_proxy_providers, resolve_proxy, serialize_audio_track,
                                         serialize_subtitle_track, serialize_title, serialize_video_track,
                                         validate_service)
from unshackle.core.api.session_serializer import serialize_session
from unshackle.core.config import config
from unshackle.core.credential import Credential
from unshackle.core.search_result import SearchResult
from unshackle.core.services import Services
from unshackle.core.titles import Episode
from unshackle.core.utils.click_types import ContextData
from unshackle.core.utils.collections import merge_dict

log = logging.getLogger("api.remote")


def load_cookies_from_content(cookies_content: Optional[str]) -> Optional[http.cookiejar.MozillaCookieJar]:
    """
    Load cookies from raw cookie file content.

    Args:
        cookies_content: Raw content of a Netscape/Mozilla format cookie file

    Returns:
        MozillaCookieJar object or None
    """
    if not cookies_content:
        return None

    # Write to temporary file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(cookies_content)
        temp_path = f.name

    try:
        # Load using standard cookie jar
        cookie_jar = http.cookiejar.MozillaCookieJar(temp_path)
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
        return cookie_jar
    finally:
        # Clean up temp file
        Path(temp_path).unlink(missing_ok=True)


def create_credential_from_dict(cred_data: Optional[Dict[str, str]]) -> Optional[Credential]:
    """
    Create a Credential object from dictionary.

    Args:
        cred_data: Dictionary with 'username' and 'password' keys

    Returns:
        Credential object or None
    """
    if not cred_data or "username" not in cred_data or "password" not in cred_data:
        return None

    return Credential(username=cred_data["username"], password=cred_data["password"])


def get_auth_from_request(data: Dict[str, Any], service_tag: str, profile: Optional[str] = None):
    """
    Get authentication (cookies and credentials) from request data or fallback to server config.

    Args:
        data: Request data
        service_tag: Service tag
        profile: Profile name

    Returns:
        Tuple of (cookies, credential)
    """
    # Try to get from client request first
    cookies_content = data.get("cookies")
    credential_data = data.get("credential")

    if cookies_content:
        cookies = load_cookies_from_content(cookies_content)
    else:
        # Fallback to server-side cookies if not provided by client
        cookies = dl.get_cookie_jar(service_tag, profile)

    if credential_data:
        credential = create_credential_from_dict(credential_data)
    else:
        # Fallback to server-side credentials if not provided by client
        credential = dl.get_credentials(service_tag, profile)

    return cookies, credential


async def remote_list_services(request: web.Request) -> web.Response:
    """
    List all available services on this remote server.
    ---
    summary: List remote services
    description: Get all available services that can be accessed remotely
    responses:
      '200':
        description: List of available services
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                  example: success
                services:
                  type: array
                  items:
                    type: object
                    properties:
                      tag:
                        type: string
                      aliases:
                        type: array
                        items:
                          type: string
                      geofence:
                        type: array
                        items:
                          type: string
                      help:
                        type: string
      '500':
        description: Server error
    """
    try:
        service_tags = Services.get_tags()
        services_info = []

        for tag in service_tags:
            service_data = {
                "tag": tag,
                "aliases": [],
                "geofence": [],
                "help": None,
            }

            try:
                service_module = Services.load(tag)

                if hasattr(service_module, "ALIASES"):
                    service_data["aliases"] = list(service_module.ALIASES)

                if hasattr(service_module, "GEOFENCE"):
                    service_data["geofence"] = list(service_module.GEOFENCE)

                if service_module.__doc__:
                    service_data["help"] = service_module.__doc__.strip()

            except Exception as e:
                log.warning(f"Could not load details for service {tag}: {e}")

            services_info.append(service_data)

        return web.json_response({"status": "success", "services": services_info})

    except Exception as e:
        log.exception("Error listing remote services")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def remote_search(request: web.Request) -> web.Response:
    """
    Search for content on a remote service.
    ---
    summary: Search remote service
    description: Search for content using a remote service
    parameters:
      - name: service
        in: path
        required: true
        schema:
          type: string
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            required:
              - query
            properties:
              query:
                type: string
                description: Search query
              profile:
                type: string
                description: Profile to use for credentials
    responses:
      '200':
        description: Search results
      '400':
        description: Invalid request
      '500':
        description: Server error
    """
    service_tag = request.match_info.get("service")

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON request body"}, status=400)

    query = data.get("query")
    if not query:
        return web.json_response({"status": "error", "message": "Missing required parameter: query"}, status=400)

    normalized_service = validate_service(service_tag)
    if not normalized_service:
        return web.json_response(
            {"status": "error", "message": f"Invalid or unavailable service: {service_tag}"}, status=400
        )

    try:
        profile = data.get("profile")

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
        ctx.invoked_subcommand = normalized_service

        service_ctx = click.Context(dummy_service, parent=ctx)
        service_ctx.obj = ctx.obj

        # Get service initialization parameters
        service_init_params = inspect.signature(service_module.__init__).parameters
        service_kwargs = {}

        # Extract defaults from click command
        if hasattr(service_module, "cli") and hasattr(service_module.cli, "params"):
            for param in service_module.cli.params:
                if hasattr(param, "name") and param.name not in service_kwargs:
                    if hasattr(param, "default") and param.default is not None:
                        service_kwargs[param.name] = param.default

        # Add query parameter
        if "query" in service_init_params:
            service_kwargs["query"] = query

        # Filter to only valid parameters
        filtered_kwargs = {k: v for k, v in service_kwargs.items() if k in service_init_params}

        service_instance = service_module(service_ctx, **filtered_kwargs)

        # Authenticate with client-provided or server-side auth
        cookies, credential = get_auth_from_request(data, normalized_service, profile)
        service_instance.authenticate(cookies, credential)

        # Perform search
        search_results = []
        if hasattr(service_instance, "search"):
            for result in service_instance.search():
                if isinstance(result, SearchResult):
                    search_results.append(
                        {
                            "id": str(result.id_),
                            "title": result.title,
                            "description": result.description,
                            "label": result.label,
                            "url": result.url,
                        }
                    )

        # Serialize session data
        session_data = serialize_session(service_instance.session)

        return web.json_response({"status": "success", "results": search_results, "session": session_data})

    except Exception as e:
        log.exception("Error performing remote search")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def remote_get_titles(request: web.Request) -> web.Response:
    """
    Get titles from a remote service.
    ---
    summary: Get titles from remote service
    description: Get available titles for content from a remote service
    parameters:
      - name: service
        in: path
        required: true
        schema:
          type: string
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            required:
              - title
            properties:
              title:
                type: string
                description: Title identifier, URL, or any format accepted by the service
              profile:
                type: string
                description: Profile to use for credentials
              proxy:
                type: string
                description: Proxy region code (e.g., "ca", "us") or full proxy URL - uses server's proxy configuration
              no_proxy:
                type: boolean
                description: Disable proxy usage
              cookies:
                type: string
                description: Raw Netscape/Mozilla format cookie file content (optional - uses server cookies if not provided)
              credential:
                type: object
                description: Credentials object with username and password (optional - uses server credentials if not provided)
                properties:
                  username:
                    type: string
                  password:
                    type: string
    responses:
      '200':
        description: Titles and session data
      '400':
        description: Invalid request
      '500':
        description: Server error
    """
    service_tag = request.match_info.get("service")

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON request body"}, status=400)

    # Accept 'title', 'title_id', or 'url' for flexibility
    title = data.get("title") or data.get("title_id") or data.get("url")
    if not title:
        return web.json_response(
            {
                "status": "error",
                "message": "Missing required parameter: title (can be URL, ID, or any format accepted by the service)",
            },
            status=400,
        )

    normalized_service = validate_service(service_tag)
    if not normalized_service:
        return web.json_response(
            {"status": "error", "message": f"Invalid or unavailable service: {service_tag}"}, status=400
        )

    try:
        profile = data.get("profile")

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
        dummy_service.params = [click.Argument([title], type=str)]
        ctx.invoked_subcommand = normalized_service

        service_ctx = click.Context(dummy_service, parent=ctx)
        service_ctx.obj = ctx.obj

        service_kwargs = {"title": title}

        # Add additional parameters from request data
        for key, value in data.items():
            if key not in ["title", "title_id", "url", "profile", "proxy", "no_proxy"]:
                service_kwargs[key] = value

        # Get service parameter info and click command defaults
        service_init_params = inspect.signature(service_module.__init__).parameters

        # Extract default values from the click command
        if hasattr(service_module, "cli") and hasattr(service_module.cli, "params"):
            for param in service_module.cli.params:
                if hasattr(param, "name") and param.name not in service_kwargs:
                    if hasattr(param, "default") and param.default is not None:
                        service_kwargs[param.name] = param.default

        # Handle required parameters
        for param_name, param_info in service_init_params.items():
            if param_name not in service_kwargs and param_name not in ["self", "ctx"]:
                if param_info.default is inspect.Parameter.empty:
                    if param_name == "meta_lang":
                        service_kwargs[param_name] = None
                    elif param_name == "movie":
                        service_kwargs[param_name] = False

        # Filter to only valid parameters
        filtered_kwargs = {k: v for k, v in service_kwargs.items() if k in service_init_params}

        service_instance = service_module(service_ctx, **filtered_kwargs)

        # Authenticate with client-provided or server-side auth
        cookies, credential = get_auth_from_request(data, normalized_service, profile)
        service_instance.authenticate(cookies, credential)

        # Get titles
        titles = service_instance.get_titles()

        if hasattr(titles, "__iter__") and not isinstance(titles, str):
            title_list = [serialize_title(t) for t in titles]
        else:
            title_list = [serialize_title(titles)]

        # Serialize session data
        session_data = serialize_session(service_instance.session)

        return web.json_response({"status": "success", "titles": title_list, "session": session_data})

    except Exception as e:
        log.exception("Error getting remote titles")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def remote_get_tracks(request: web.Request) -> web.Response:
    """
    Get tracks from a remote service.
    ---
    summary: Get tracks from remote service
    description: Get available tracks for a title from a remote service
    parameters:
      - name: service
        in: path
        required: true
        schema:
          type: string
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            required:
              - title
            properties:
              title:
                type: string
                description: Title identifier, URL, or any format accepted by the service
              wanted:
                type: string
                description: Specific episodes/seasons
              profile:
                type: string
                description: Profile to use for credentials
              proxy:
                type: string
                description: Proxy region code (e.g., "ca", "us") or full proxy URL - uses server's proxy configuration
              no_proxy:
                type: boolean
                description: Disable proxy usage
              cookies:
                type: string
                description: Raw Netscape/Mozilla format cookie file content (optional - uses server cookies if not provided)
              credential:
                type: object
                description: Credentials object with username and password (optional - uses server credentials if not provided)
                properties:
                  username:
                    type: string
                  password:
                    type: string
    responses:
      '200':
        description: Tracks and session data
      '400':
        description: Invalid request
      '500':
        description: Server error
    """
    service_tag = request.match_info.get("service")

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON request body"}, status=400)

    # Accept 'title', 'title_id', or 'url' for flexibility
    title = data.get("title") or data.get("title_id") or data.get("url")
    if not title:
        return web.json_response(
            {
                "status": "error",
                "message": "Missing required parameter: title (can be URL, ID, or any format accepted by the service)",
            },
            status=400,
        )

    normalized_service = validate_service(service_tag)
    if not normalized_service:
        return web.json_response(
            {"status": "error", "message": f"Invalid or unavailable service: {service_tag}"}, status=400
        )

    try:
        profile = data.get("profile")

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
        dummy_service.params = [click.Argument([title], type=str)]
        ctx.invoked_subcommand = normalized_service

        service_ctx = click.Context(dummy_service, parent=ctx)
        service_ctx.obj = ctx.obj

        service_kwargs = {"title": title}

        # Add additional parameters
        for key, value in data.items():
            if key not in ["title", "title_id", "url", "profile", "wanted", "season", "episode", "proxy", "no_proxy"]:
                service_kwargs[key] = value

        # Get service parameters
        service_init_params = inspect.signature(service_module.__init__).parameters

        # Extract defaults from click command
        if hasattr(service_module, "cli") and hasattr(service_module.cli, "params"):
            for param in service_module.cli.params:
                if hasattr(param, "name") and param.name not in service_kwargs:
                    if hasattr(param, "default") and param.default is not None:
                        service_kwargs[param.name] = param.default

        # Handle required parameters
        for param_name, param_info in service_init_params.items():
            if param_name not in service_kwargs and param_name not in ["self", "ctx"]:
                if param_info.default is inspect.Parameter.empty:
                    if param_name == "meta_lang":
                        service_kwargs[param_name] = None
                    elif param_name == "movie":
                        service_kwargs[param_name] = False

        # Filter to valid parameters
        filtered_kwargs = {k: v for k, v in service_kwargs.items() if k in service_init_params}

        service_instance = service_module(service_ctx, **filtered_kwargs)

        # Authenticate with client-provided or server-side auth
        cookies, credential = get_auth_from_request(data, normalized_service, profile)
        service_instance.authenticate(cookies, credential)

        # Get titles
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
                except Exception as e:
                    return web.json_response(
                        {"status": "error", "message": f"Invalid wanted parameter: {e}"}, status=400
                    )
            elif season is not None and episode is not None:
                wanted = [f"{season}x{episode}"]

            if wanted:
                matching_titles = []
                for title in titles_list:
                    if isinstance(title, Episode):
                        episode_key = f"{title.season}x{title.number}"
                        if episode_key in wanted:
                            matching_titles.append(title)
                    else:
                        matching_titles.append(title)

                if not matching_titles:
                    return web.json_response(
                        {"status": "error", "message": "No episodes found matching wanted criteria"}, status=404
                    )

                # Handle multiple episodes
                if len(matching_titles) > 1 and all(isinstance(t, Episode) for t in matching_titles):
                    episodes_data = []
                    failed_episodes = []

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
                        except (SystemExit, Exception):
                            failed_episodes.append(f"S{title.season}E{title.number:02d}")
                            continue

                    if episodes_data:
                        session_data = serialize_session(service_instance.session)
                        response = {"status": "success", "episodes": episodes_data, "session": session_data}
                        if failed_episodes:
                            response["unavailable_episodes"] = failed_episodes
                        return web.json_response(response)
                    else:
                        return web.json_response(
                            {
                                "status": "error",
                                "message": f"No available episodes. Unavailable: {', '.join(failed_episodes)}",
                            },
                            status=404,
                        )
                else:
                    first_title = matching_titles[0]
            else:
                first_title = titles_list[0]
        else:
            first_title = titles

        # Get tracks for single title
        tracks = service_instance.get_tracks(first_title)

        video_tracks = sorted(tracks.videos, key=lambda t: t.bitrate or 0, reverse=True)
        audio_tracks = sorted(tracks.audio, key=lambda t: t.bitrate or 0, reverse=True)

        # Serialize session data
        session_data = serialize_session(service_instance.session)

        response_data = {
            "status": "success",
            "title": serialize_title(first_title),
            "video": [serialize_video_track(t) for t in video_tracks],
            "audio": [serialize_audio_track(t) for t in audio_tracks],
            "subtitles": [serialize_subtitle_track(t) for t in tracks.subtitles],
            "session": session_data,
        }

        return web.json_response(response_data)

    except Exception as e:
        log.exception("Error getting remote tracks")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def remote_get_chapters(request: web.Request) -> web.Response:
    """
    Get chapters from a remote service.
    ---
    summary: Get chapters from remote service
    description: Get available chapters for a title from a remote service
    parameters:
      - name: service
        in: path
        required: true
        schema:
          type: string
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            required:
              - title
            properties:
              title:
                type: string
                description: Title identifier, URL, or any format accepted by the service
              profile:
                type: string
                description: Profile to use for credentials
              proxy:
                type: string
                description: Proxy region code (e.g., "ca", "us") or full proxy URL - uses server's proxy configuration
              no_proxy:
                type: boolean
                description: Disable proxy usage
              cookies:
                type: string
                description: Raw Netscape/Mozilla format cookie file content (optional - uses server cookies if not provided)
              credential:
                type: object
                description: Credentials object with username and password (optional - uses server credentials if not provided)
                properties:
                  username:
                    type: string
                  password:
                    type: string
    responses:
      '200':
        description: Chapters and session data
      '400':
        description: Invalid request
      '500':
        description: Server error
    """
    service_tag = request.match_info.get("service")

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON request body"}, status=400)

    # Accept 'title', 'title_id', or 'url' for flexibility
    title = data.get("title") or data.get("title_id") or data.get("url")
    if not title:
        return web.json_response(
            {
                "status": "error",
                "message": "Missing required parameter: title (can be URL, ID, or any format accepted by the service)",
            },
            status=400,
        )

    normalized_service = validate_service(service_tag)
    if not normalized_service:
        return web.json_response(
            {"status": "error", "message": f"Invalid or unavailable service: {service_tag}"}, status=400
        )

    try:
        profile = data.get("profile")

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
        dummy_service.params = [click.Argument([title], type=str)]
        ctx.invoked_subcommand = normalized_service

        service_ctx = click.Context(dummy_service, parent=ctx)
        service_ctx.obj = ctx.obj

        service_kwargs = {"title": title}

        # Add additional parameters
        for key, value in data.items():
            if key not in ["title", "title_id", "url", "profile", "proxy", "no_proxy"]:
                service_kwargs[key] = value

        # Get service parameters
        service_init_params = inspect.signature(service_module.__init__).parameters

        # Extract defaults
        if hasattr(service_module, "cli") and hasattr(service_module.cli, "params"):
            for param in service_module.cli.params:
                if hasattr(param, "name") and param.name not in service_kwargs:
                    if hasattr(param, "default") and param.default is not None:
                        service_kwargs[param.name] = param.default

        # Handle required parameters
        for param_name, param_info in service_init_params.items():
            if param_name not in service_kwargs and param_name not in ["self", "ctx"]:
                if param_info.default is inspect.Parameter.empty:
                    if param_name == "meta_lang":
                        service_kwargs[param_name] = None
                    elif param_name == "movie":
                        service_kwargs[param_name] = False

        # Filter to valid parameters
        filtered_kwargs = {k: v for k, v in service_kwargs.items() if k in service_init_params}

        service_instance = service_module(service_ctx, **filtered_kwargs)

        # Authenticate with client-provided or server-side auth
        cookies, credential = get_auth_from_request(data, normalized_service, profile)
        service_instance.authenticate(cookies, credential)

        # Get titles
        titles = service_instance.get_titles()

        if hasattr(titles, "__iter__") and not isinstance(titles, str):
            first_title = list(titles)[0]
        else:
            first_title = titles

        # Get chapters if service supports it
        chapters_data = []
        if hasattr(service_instance, "get_chapters"):
            chapters = service_instance.get_chapters(first_title)
            if chapters:
                for chapter in chapters:
                    chapters_data.append(
                        {
                            "timestamp": chapter.timestamp,
                            "name": chapter.name if hasattr(chapter, "name") else None,
                        }
                    )

        # Serialize session data
        session_data = serialize_session(service_instance.session)

        return web.json_response({"status": "success", "chapters": chapters_data, "session": session_data})

    except Exception as e:
        log.exception("Error getting remote chapters")
        return web.json_response({"status": "error", "message": str(e)}, status=500)
