"""API handlers for remote service functionality."""

import http.cookiejar
import inspect
import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

import click
import yaml
from aiohttp import web

from unshackle.commands.dl import dl
from unshackle.core.api.api_keys import can_use_cdm, get_api_key_from_request, get_default_cdm, is_premium_user
from unshackle.core.api.handlers import (serialize_audio_track, serialize_subtitle_track, serialize_title,
                                         serialize_video_track, validate_service)
from unshackle.core.api.session_serializer import deserialize_session, serialize_session
from unshackle.core.config import config
from unshackle.core.credential import Credential
from unshackle.core.search_result import SearchResult
from unshackle.core.services import Services
from unshackle.core.titles import Episode
from unshackle.core.utils.click_types import ContextData
from unshackle.core.utils.collections import merge_dict

log = logging.getLogger("api.remote")

# Session expiry time in seconds (24 hours)
SESSION_EXPIRY_TIME = 86400


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


def validate_session_expiry(session_data: Dict[str, Any]) -> Optional[str]:
    """
    Validate if a session is expired.

    Args:
        session_data: Session data with cached_at timestamp

    Returns:
        Error code if session is expired, None if valid
    """
    if not session_data:
        return None

    cached_at = session_data.get("cached_at")
    if not cached_at:
        # No timestamp - assume valid (backward compatibility)
        return None

    age = time.time() - cached_at
    if age > SESSION_EXPIRY_TIME:
        log.warning(f"Session expired (age: {age:.0f}s, limit: {SESSION_EXPIRY_TIME}s)")
        return "SESSION_EXPIRED"

    # Warn if session is close to expiry (within 1 hour)
    if age > (SESSION_EXPIRY_TIME - 3600):
        remaining = SESSION_EXPIRY_TIME - age
        log.info(f"Session expires soon (remaining: {remaining:.0f}s)")

    return None


def get_auth_from_request(data: Dict[str, Any], service_tag: str, profile: Optional[str] = None):
    """
    Get authentication from request data or fallback to server config.

    Server is STATELESS - it never stores sessions.
    Client sends pre-authenticated session with each request.

    Priority order:
    1. Pre-authenticated session from client (sent with request)
    2. Client-provided credentials/cookies in request
    3. Server-side credentials/cookies from config (fallback)

    Args:
        data: Request data
        service_tag: Service tag
        profile: Profile name

    Returns:
        Tuple of (cookies, credential, pre_authenticated_session, session_error)
        where session_error is an error code if session is expired
    """
    # First priority: Check for pre-authenticated session sent by client
    pre_authenticated_session = data.get("pre_authenticated_session")

    if pre_authenticated_session:
        log.info(f"Using client's pre-authenticated session for {service_tag}")

        # Validate session expiry
        session_error = validate_session_expiry(pre_authenticated_session)
        if session_error:
            log.warning(f"Session validation failed: {session_error}")
            return None, None, None, session_error

        # Return None, None to indicate we'll use the pre-authenticated session
        return None, None, pre_authenticated_session, None

    # Second priority: Try to get from client request
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

    return cookies, credential, None, None


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

    except Exception:
        log.exception("Error listing remote services")
        return web.json_response({"status": "error", "message": "Internal server error while listing services"}, status=500)


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
        # Client MUST send resolved proxy with credentials (e.g., http://user:pass@host:port)
        # Server does NOT resolve proxy providers - client must do that
        proxy_param = data.get("proxy")
        no_proxy = data.get("no_proxy", False)

        if proxy_param and not no_proxy:
            import re

            # Validate that client sent a fully resolved proxy URL
            if re.match(r"^https?://", proxy_param):
                log.info("Using client-resolved proxy with credentials")
            else:
                # Reject unresolved proxy parameters
                log.error(f"[SECURITY] Client sent unresolved proxy parameter: {proxy_param}")
                return web.json_response({
                    "status": "error",
                    "error_code": "INVALID_PROXY",
                    "message": f"Proxy must be a fully resolved URL (http://... or https://...). "
                               f"Cannot use proxy provider shortcuts like '{proxy_param}'. "
                               f"Please resolve the proxy on the client side before sending to server."
                }, status=400)

        ctx = click.Context(dummy_service)
        ctx.obj = ContextData(config=service_config, cdm=None, proxy_providers=[], profile=profile)
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
        cookies, credential, pre_authenticated_session, session_error = get_auth_from_request(data, normalized_service, profile)

        # Check for session expiry
        if session_error == "SESSION_EXPIRED":
            return web.json_response({
                "status": "error",
                "error_code": "SESSION_EXPIRED",
                "message": f"Session expired for {normalized_service}. Please re-authenticate."
            }, status=401)

        try:
            if pre_authenticated_session:
                # Use pre-authenticated session sent by client (server is stateless)
                deserialize_session(pre_authenticated_session, service_instance.session)
            else:
                # Authenticate with credentials/cookies
                if not cookies and not credential:
                    # No auth data available - tell client to authenticate
                    return web.json_response({
                        "status": "error",
                        "error_code": "AUTH_REQUIRED",
                        "message": f"Authentication required for {normalized_service}. No credentials or session available."
                    }, status=401)

                service_instance.authenticate(cookies, credential)
        except Exception as auth_error:
            # Authentication failed - tell client to re-authenticate
            log.warning(f"Authentication failed for {normalized_service}: {auth_error}")
            return web.json_response({
                "status": "error",
                "error_code": "AUTH_REQUIRED",
                "message": f"Authentication failed for {normalized_service}. Please authenticate locally."
            }, status=401)

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

    except Exception:
        log.exception("Error performing remote search")
        return web.json_response({"status": "error", "message": "Internal server error while performing search"}, status=500)


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
        # Client MUST send resolved proxy with credentials (e.g., http://user:pass@host:port)
        # Server does NOT resolve proxy providers - client must do that
        proxy_param = data.get("proxy")
        no_proxy = data.get("no_proxy", False)

        if proxy_param and not no_proxy:
            import re

            # Validate that client sent a fully resolved proxy URL
            if re.match(r"^https?://", proxy_param):
                log.info("Using client-resolved proxy with credentials")
            else:
                # Reject unresolved proxy parameters
                log.error(f"[SECURITY] Client sent unresolved proxy parameter: {proxy_param}")
                return web.json_response({
                    "status": "error",
                    "error_code": "INVALID_PROXY",
                    "message": f"Proxy must be a fully resolved URL (http://... or https://...). "
                               f"Cannot use proxy provider shortcuts like '{proxy_param}'. "
                               f"Please resolve the proxy on the client side before sending to server."
                }, status=400)

        ctx = click.Context(dummy_service)
        ctx.obj = ContextData(config=service_config, cdm=None, proxy_providers=[], profile=profile)
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
        cookies, credential, pre_authenticated_session, session_error = get_auth_from_request(data, normalized_service, profile)

        # Check for session expiry
        if session_error == "SESSION_EXPIRED":
            return web.json_response({
                "status": "error",
                "error_code": "SESSION_EXPIRED",
                "message": f"Session expired for {normalized_service}. Please re-authenticate."
            }, status=401)

        try:
            if pre_authenticated_session:
                # Use pre-authenticated session sent by client (server is stateless)
                deserialize_session(pre_authenticated_session, service_instance.session)
            else:
                # Authenticate with credentials/cookies
                if not cookies and not credential:
                    # No auth data available - tell client to authenticate
                    return web.json_response({
                        "status": "error",
                        "error_code": "AUTH_REQUIRED",
                        "message": f"Authentication required for {normalized_service}. No credentials or session available."
                    }, status=401)

                service_instance.authenticate(cookies, credential)
        except Exception as auth_error:
            # Authentication failed - tell client to re-authenticate
            log.warning(f"Authentication failed for {normalized_service}: {auth_error}")
            return web.json_response({
                "status": "error",
                "error_code": "AUTH_REQUIRED",
                "message": f"Authentication failed for {normalized_service}. Please authenticate locally."
            }, status=401)

        # Get titles
        titles = service_instance.get_titles()

        if hasattr(titles, "__iter__") and not isinstance(titles, str):
            title_list = [serialize_title(t) for t in titles]
        else:
            title_list = [serialize_title(titles)]

        # Serialize session data
        session_data = serialize_session(service_instance.session)

        # Include geofence info so client knows to activate VPN
        geofence = []
        if hasattr(service_module, "GEOFENCE"):
            geofence = list(service_module.GEOFENCE)

        return web.json_response({
            "status": "success",
            "titles": title_list,
            "session": session_data,
            "geofence": geofence
        })

    except Exception:
        log.exception("Error getting remote titles")
        return web.json_response({"status": "error", "message": "Internal server error while getting titles"}, status=500)


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
        # Client MUST send resolved proxy with credentials (e.g., http://user:pass@host:port)
        # Server does NOT resolve proxy providers - client must do that
        proxy_param = data.get("proxy")
        no_proxy = data.get("no_proxy", False)

        if proxy_param and not no_proxy:
            import re

            # Validate that client sent a fully resolved proxy URL
            if re.match(r"^https?://", proxy_param):
                log.info("Using client-resolved proxy with credentials")
            else:
                # Reject unresolved proxy parameters
                log.error(f"[SECURITY] Client sent unresolved proxy parameter: {proxy_param}")
                return web.json_response({
                    "status": "error",
                    "error_code": "INVALID_PROXY",
                    "message": f"Proxy must be a fully resolved URL (http://... or https://...). "
                               f"Cannot use proxy provider shortcuts like '{proxy_param}'. "
                               f"Please resolve the proxy on the client side before sending to server."
                }, status=400)

        ctx = click.Context(dummy_service)
        ctx.obj = ContextData(config=service_config, cdm=None, proxy_providers=[], profile=profile)
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
        cookies, credential, pre_authenticated_session, session_error = get_auth_from_request(data, normalized_service, profile)

        # Check for session expiry
        if session_error == "SESSION_EXPIRED":
            return web.json_response({
                "status": "error",
                "error_code": "SESSION_EXPIRED",
                "message": f"Session expired for {normalized_service}. Please re-authenticate."
            }, status=401)

        try:
            if pre_authenticated_session:
                # Use pre-authenticated session sent by client (server is stateless)
                deserialize_session(pre_authenticated_session, service_instance.session)
            else:
                # Authenticate with credentials/cookies
                if not cookies and not credential:
                    # No auth data available - tell client to authenticate
                    return web.json_response({
                        "status": "error",
                        "error_code": "AUTH_REQUIRED",
                        "message": f"Authentication required for {normalized_service}. No credentials or session available."
                    }, status=401)

                service_instance.authenticate(cookies, credential)
        except Exception as auth_error:
            # Authentication failed - tell client to re-authenticate
            log.warning(f"Authentication failed for {normalized_service}: {auth_error}")
            return web.json_response({
                "status": "error",
                "error_code": "AUTH_REQUIRED",
                "message": f"Authentication failed for {normalized_service}. Please authenticate locally."
            }, status=401)

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

                        # Include geofence info
                        geofence = []
                        if hasattr(service_module, "GEOFENCE"):
                            geofence = list(service_module.GEOFENCE)

                        response = {
                            "status": "success",
                            "episodes": episodes_data,
                            "session": session_data,
                            "geofence": geofence
                        }
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

        # Include geofence info
        geofence = []
        if hasattr(service_module, "GEOFENCE"):
            geofence = list(service_module.GEOFENCE)

        response_data = {
            "status": "success",
            "title": serialize_title(first_title),
            "video": [serialize_video_track(t) for t in video_tracks],
            "audio": [serialize_audio_track(t) for t in audio_tracks],
            "subtitles": [serialize_subtitle_track(t) for t in tracks.subtitles],
            "session": session_data,
            "geofence": geofence
        }

        return web.json_response(response_data)

    except Exception:
        log.exception("Error getting remote tracks")
        return web.json_response({"status": "error", "message": "Internal server error while getting tracks"}, status=500)


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
        # Client MUST send resolved proxy with credentials (e.g., http://user:pass@host:port)
        # Server does NOT resolve proxy providers - client must do that
        proxy_param = data.get("proxy")
        no_proxy = data.get("no_proxy", False)

        if proxy_param and not no_proxy:
            import re

            # Validate that client sent a fully resolved proxy URL
            if re.match(r"^https?://", proxy_param):
                log.info("Using client-resolved proxy with credentials")
            else:
                # Reject unresolved proxy parameters
                log.error(f"[SECURITY] Client sent unresolved proxy parameter: {proxy_param}")
                return web.json_response({
                    "status": "error",
                    "error_code": "INVALID_PROXY",
                    "message": f"Proxy must be a fully resolved URL (http://... or https://...). "
                               f"Cannot use proxy provider shortcuts like '{proxy_param}'. "
                               f"Please resolve the proxy on the client side before sending to server."
                }, status=400)

        ctx = click.Context(dummy_service)
        ctx.obj = ContextData(config=service_config, cdm=None, proxy_providers=[], profile=profile)
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
        cookies, credential, pre_authenticated_session, session_error = get_auth_from_request(data, normalized_service, profile)

        # Check for session expiry
        if session_error == "SESSION_EXPIRED":
            return web.json_response({
                "status": "error",
                "error_code": "SESSION_EXPIRED",
                "message": f"Session expired for {normalized_service}. Please re-authenticate."
            }, status=401)

        try:
            if pre_authenticated_session:
                # Use pre-authenticated session sent by client (server is stateless)
                deserialize_session(pre_authenticated_session, service_instance.session)
            else:
                # Authenticate with credentials/cookies
                if not cookies and not credential:
                    # No auth data available - tell client to authenticate
                    return web.json_response({
                        "status": "error",
                        "error_code": "AUTH_REQUIRED",
                        "message": f"Authentication required for {normalized_service}. No credentials or session available."
                    }, status=401)

                service_instance.authenticate(cookies, credential)
        except Exception as auth_error:
            # Authentication failed - tell client to re-authenticate
            log.warning(f"Authentication failed for {normalized_service}: {auth_error}")
            return web.json_response({
                "status": "error",
                "error_code": "AUTH_REQUIRED",
                "message": f"Authentication failed for {normalized_service}. Please authenticate locally."
            }, status=401)

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

    except Exception:
        log.exception("Error getting remote chapters")
        return web.json_response({"status": "error", "message": "Internal server error while getting chapters"}, status=500)


async def remote_get_license(request: web.Request) -> web.Response:
    """
    Get DRM license from a remote service using client's CDM.

    The server does NOT need a CDM - it just facilitates the license request
    using the client's pre-authenticated session. The client decrypts using
    their own CDM.
    ---
    summary: Get DRM license from remote service
    description: Request license acquisition using client session (server does not need CDM)
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
              - track_id
              - challenge
            properties:
              title:
                type: string
                description: Title identifier
              track_id:
                type: string
                description: Track ID for license
              challenge:
                type: string
                description: Base64-encoded license challenge from client's CDM
              session:
                type: integer
                description: CDM session ID
              profile:
                type: string
                description: Profile to use
              pre_authenticated_session:
                type: object
                description: Client's pre-authenticated session
    responses:
      '200':
        description: License response
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                  example: success
                license:
                  type: string
                  description: Base64-encoded license response
                session:
                  type: object
                  description: Updated session data
      '400':
        description: Invalid request
      '401':
        description: Authentication required
      '500':
        description: Server error
    """
    service_tag = request.match_info.get("service")

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON request body"}, status=400)

    # Validate required parameters
    title = data.get("title")
    track_id = data.get("track_id")
    challenge = data.get("challenge")

    if not all([title, track_id, challenge]):
        return web.json_response(
            {
                "status": "error",
                "message": "Missing required parameters: title, track_id, challenge"
            },
            status=400
        )

    normalized_service = validate_service(service_tag)
    if not normalized_service:
        return web.json_response(
            {"status": "error", "message": f"Invalid or unavailable service: {service_tag}"},
            status=400
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
        # Client MUST send resolved proxy with credentials (e.g., http://user:pass@host:port)
        # Server does NOT resolve proxy providers - client must do that
        proxy_param = data.get("proxy")
        no_proxy = data.get("no_proxy", False)

        if proxy_param and not no_proxy:
            import re

            # Validate that client sent a fully resolved proxy URL
            if re.match(r"^https?://", proxy_param):
                log.info("Using client-resolved proxy with credentials")
            else:
                # Reject unresolved proxy parameters
                log.error(f"[SECURITY] Client sent unresolved proxy parameter: {proxy_param}")
                return web.json_response({
                    "status": "error",
                    "error_code": "INVALID_PROXY",
                    "message": f"Proxy must be a fully resolved URL (http://... or https://...). "
                               f"Cannot use proxy provider shortcuts like '{proxy_param}'. "
                               f"Please resolve the proxy on the client side before sending to server."
                }, status=400)

        ctx = click.Context(dummy_service)
        ctx.obj = ContextData(config=service_config, cdm=None, proxy_providers=[], profile=profile)
        ctx.params = {"proxy": proxy_param, "no_proxy": no_proxy}

        service_module = Services.load(normalized_service)

        dummy_service.name = normalized_service
        ctx.invoked_subcommand = normalized_service

        service_ctx = click.Context(dummy_service, parent=ctx)
        service_ctx.obj = ctx.obj

        service_kwargs = {"title": title}

        # Add additional parameters
        for key, value in data.items():
            if key not in ["title", "track_id", "challenge", "session", "profile", "proxy", "no_proxy", "pre_authenticated_session", "credential", "cookies"]:
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
        cookies, credential, pre_authenticated_session, session_error = get_auth_from_request(data, normalized_service, profile)

        # Check for session expiry
        if session_error == "SESSION_EXPIRED":
            return web.json_response({
                "status": "error",
                "error_code": "SESSION_EXPIRED",
                "message": f"Session expired for {normalized_service}. Please re-authenticate."
            }, status=401)

        try:
            if pre_authenticated_session:
                # Use pre-authenticated session sent by client (server is stateless)
                deserialize_session(pre_authenticated_session, service_instance.session)
            else:
                # Authenticate with credentials/cookies
                if not cookies and not credential:
                    # No auth data available - tell client to authenticate
                    return web.json_response({
                        "status": "error",
                        "error_code": "AUTH_REQUIRED",
                        "message": f"Authentication required for {normalized_service}. No credentials or session available."
                    }, status=401)

                service_instance.authenticate(cookies, credential)
        except Exception as auth_error:
            # Authentication failed - tell client to re-authenticate
            log.warning(f"Authentication failed for {normalized_service}: {auth_error}")
            return web.json_response({
                "status": "error",
                "error_code": "AUTH_REQUIRED",
                "message": f"Authentication failed for {normalized_service}. Please authenticate locally."
            }, status=401)

        # Get titles to find the correct one
        titles = service_instance.get_titles()
        if hasattr(titles, "__iter__") and not isinstance(titles, str):
            first_title = list(titles)[0]
        else:
            first_title = titles

        # Get tracks to find license URL
        tracks = service_instance.get_tracks(first_title)

        # Find the track with the matching ID
        target_track = None
        for track in tracks.videos + tracks.audio:
            if str(track.id) == str(track_id) or track.id == track_id:
                target_track = track
                break

        if not target_track:
            return web.json_response({
                "status": "error",
                "message": f"Track {track_id} not found"
            }, status=404)

        # Get license URL and headers from track
        if not hasattr(target_track, "drm") or not target_track.drm:
            return web.json_response({
                "status": "error",
                "message": f"Track {track_id} is not DRM-protected"
            }, status=400)

        # Extract license information
        license_url = None
        license_headers = {}

        # Try to get license URL from DRM info
        for drm_info in target_track.drm:
            if hasattr(drm_info, "license_url"):
                license_url = drm_info.license_url
                if hasattr(drm_info, "license_headers"):
                    license_headers = drm_info.license_headers or {}
                break

        if not license_url:
            return web.json_response({
                "status": "error",
                "message": "No license URL found for track"
            }, status=400)

        # Make license request using service session
        import base64
        challenge_data = base64.b64decode(challenge)

        license_response = service_instance.session.post(
            license_url,
            data=challenge_data,
            headers=license_headers
        )

        if license_response.status_code != 200:
            return web.json_response({
                "status": "error",
                "message": f"License request failed: {license_response.status_code}"
            }, status=500)

        # Return base64-encoded license
        license_b64 = base64.b64encode(license_response.content).decode("utf-8")

        # Serialize session data
        session_data = serialize_session(service_instance.session)

        return web.json_response({
            "status": "success",
            "license": license_b64,
            "session": session_data
        })

    except Exception:
        log.exception("Error getting remote license")
        return web.json_response({"status": "error", "message": "Internal server error while getting license"}, status=500)


async def remote_decrypt(request: web.Request) -> web.Response:
    """
    Decrypt DRM content using server's CDM (premium users only).

    This endpoint is for premium API key holders who can use the server's
    CDM infrastructure. Regular users must use their own CDM with the
    license endpoint.

    ---
    summary: Decrypt DRM content using server CDM
    description: Use server's CDM to decrypt content (premium tier only)
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
              - track_id
              - pssh
            properties:
              title:
                type: string
                description: Title identifier
              track_id:
                type: string
                description: Track ID for decryption
              pssh:
                type: string
                description: Base64-encoded PSSH box
              cdm:
                type: string
                description: Specific CDM to use (optional, uses default if not specified)
              license_url:
                type: string
                description: License server URL (optional, extracted from track if not provided)
              profile:
                type: string
                description: Profile to use
              pre_authenticated_session:
                type: object
                description: Client's pre-authenticated session
    responses:
      '200':
        description: Decryption keys
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                  example: success
                keys:
                  type: array
                  items:
                    type: object
                    properties:
                      kid:
                        type: string
                      key:
                        type: string
                      type:
                        type: string
                session:
                  type: object
                  description: Updated session data
      '400':
        description: Invalid request
      '401':
        description: Authentication required
      '403':
        description: Not authorized for premium features
      '500':
        description: Server error
    """
    service_tag = request.match_info.get("service")

    # Check if user is premium
    api_key = get_api_key_from_request(request)
    if not api_key:
        return web.json_response({
            "status": "error",
            "error_code": "NO_API_KEY",
            "message": "API key required"
        }, status=401)

    if not is_premium_user(request.app, api_key):
        return web.json_response({
            "status": "error",
            "error_code": "PREMIUM_REQUIRED",
            "message": "This endpoint requires a premium API key. Use /api/remote/{service}/license with your own CDM instead."
        }, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON request body"}, status=400)

    # Validate required parameters
    title = data.get("title")
    track_id = data.get("track_id")
    pssh = data.get("pssh")

    if not all([title, track_id, pssh]):
        return web.json_response(
            {
                "status": "error",
                "message": "Missing required parameters: title, track_id, pssh"
            },
            status=400
        )

    # Determine which CDM to use
    requested_cdm = data.get("cdm")
    if not requested_cdm:
        # Use default CDM for this API key
        requested_cdm = get_default_cdm(request.app, api_key)

    if not requested_cdm:
        return web.json_response({
            "status": "error",
            "message": "No CDM specified and no default CDM configured for your API key"
        }, status=400)

    # Check if user can use this CDM
    if not can_use_cdm(request.app, api_key, requested_cdm):
        return web.json_response({
            "status": "error",
            "error_code": "CDM_NOT_ALLOWED",
            "message": f"Your API key is not authorized to use CDM: {requested_cdm}"
        }, status=403)

    normalized_service = validate_service(service_tag)
    if not normalized_service:
        return web.json_response(
            {"status": "error", "message": f"Invalid or unavailable service: {service_tag}"},
            status=400
        )

    try:
        from pywidevine.cdm import Cdm as WidevineCdm
        from pywidevine.device import Device

        # Load the requested CDM
        log.info(f"Premium user using server CDM: {requested_cdm}")

        # Get CDM device path
        cdm_device_path = None
        if requested_cdm.endswith(".wvd"):
            # Direct path to WVD file
            cdm_device_path = Path(requested_cdm)
        else:
            # Look in configured CDM directory
            cdm_dir = config.directories.wvds
            potential_path = cdm_dir / f"{requested_cdm}.wvd"
            if potential_path.exists():
                cdm_device_path = potential_path

        if not cdm_device_path or not cdm_device_path.exists():
            return web.json_response({
                "status": "error",
                "message": f"CDM device not found: {requested_cdm}"
            }, status=404)

        # Initialize CDM
        device = Device.load(cdm_device_path)
        cdm = WidevineCdm.from_device(device)

        # Open CDM session
        session_id = cdm.open()

        # Parse PSSH
        import base64
        pssh_data = base64.b64decode(pssh)

        # Set service certificate if needed (some services require it)
        # This would be service-specific

        # Get challenge
        challenge = cdm.get_license_challenge(session_id, pssh_data)

        # Get license URL
        license_url = data.get("license_url")

        # If no license URL provided, get it from track
        if not license_url:
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
            # Client MUST send resolved proxy with credentials
            # Server does NOT resolve proxy providers - client must do that
            proxy_param = data.get("proxy")
            no_proxy = data.get("no_proxy", False)

            if proxy_param and not no_proxy:
                import re

                # Validate that client sent a fully resolved proxy URL
                if re.match(r"^https?://", proxy_param):
                    log.info("Using client-resolved proxy with credentials")
                else:
                    # Reject unresolved proxy parameters
                    log.error(f"[SECURITY] Client sent unresolved proxy parameter: {proxy_param}")
                    cdm.close(session_id)
                    return web.json_response({
                        "status": "error",
                        "error_code": "INVALID_PROXY",
                        "message": f"Proxy must be a fully resolved URL (http://... or https://...). "
                                   f"Cannot use proxy provider shortcuts like '{proxy_param}'. "
                                   f"Please resolve the proxy on the client side before sending to server."
                    }, status=400)

            ctx = click.Context(dummy_service)
            ctx.obj = ContextData(config=service_config, cdm=None, proxy_providers=[], profile=profile)
            ctx.params = {"proxy": proxy_param, "no_proxy": no_proxy}

            service_module = Services.load(normalized_service)
            dummy_service.name = normalized_service
            ctx.invoked_subcommand = normalized_service

            service_ctx = click.Context(dummy_service, parent=ctx)
            service_ctx.obj = ctx.obj

            service_kwargs = {"title": title}

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

            # Authenticate
            cookies, credential, pre_authenticated_session, session_error = get_auth_from_request(data, normalized_service, profile)

            if session_error == "SESSION_EXPIRED":
                cdm.close(session_id)
                return web.json_response({
                    "status": "error",
                    "error_code": "SESSION_EXPIRED",
                    "message": f"Session expired for {normalized_service}. Please re-authenticate."
                }, status=401)

            try:
                if pre_authenticated_session:
                    deserialize_session(pre_authenticated_session, service_instance.session)
                else:
                    if not cookies and not credential:
                        cdm.close(session_id)
                        return web.json_response({
                            "status": "error",
                            "error_code": "AUTH_REQUIRED",
                            "message": f"Authentication required for {normalized_service}."
                        }, status=401)
                    service_instance.authenticate(cookies, credential)
            except Exception as auth_error:
                cdm.close(session_id)
                log.warning(f"Authentication failed for {normalized_service}: {auth_error}")
                return web.json_response({
                    "status": "error",
                    "error_code": "AUTH_REQUIRED",
                    "message": f"Authentication failed for {normalized_service}.",
                    "details": str(auth_error)
                }, status=401)

            # Get titles and tracks to find license URL
            titles = service_instance.get_titles()
            if hasattr(titles, "__iter__") and not isinstance(titles, str):
                first_title = list(titles)[0]
            else:
                first_title = titles

            tracks = service_instance.get_tracks(first_title)

            # Find the track
            target_track = None
            for track in tracks.videos + tracks.audio:
                if str(track.id) == str(track_id) or track.id == track_id:
                    target_track = track
                    break

            if not target_track:
                cdm.close(session_id)
                return web.json_response({
                    "status": "error",
                    "message": f"Track {track_id} not found"
                }, status=404)

            if not hasattr(target_track, "drm") or not target_track.drm:
                cdm.close(session_id)
                return web.json_response({
                    "status": "error",
                    "message": f"Track {track_id} is not DRM-protected"
                }, status=400)

            # Extract license URL
            license_headers = {}
            for drm_info in target_track.drm:
                if hasattr(drm_info, "license_url"):
                    license_url = drm_info.license_url
                    if hasattr(drm_info, "license_headers"):
                        license_headers = drm_info.license_headers or {}
                    break

            if not license_url:
                cdm.close(session_id)
                return web.json_response({
                    "status": "error",
                    "message": "No license URL found for track"
                }, status=400)

            # Make license request
            license_response = service_instance.session.post(
                license_url,
                data=challenge,
                headers=license_headers
            )

            if license_response.status_code != 200:
                cdm.close(session_id)
                return web.json_response({
                    "status": "error",
                    "message": f"License request failed: {license_response.status_code}"
                }, status=500)

            # Parse license
            cdm.parse_license(session_id, license_response.content)

            # Get keys
            keys = []
            for key in cdm.get_keys(session_id):
                if key.type == "CONTENT":
                    keys.append({
                        "kid": key.kid.hex(),
                        "key": key.key.hex(),
                        "type": key.type
                    })

            # Close CDM session
            cdm.close(session_id)

            # Serialize session
            session_data = serialize_session(service_instance.session)

            return web.json_response({
                "status": "success",
                "keys": keys,
                "session": session_data,
                "cdm_used": requested_cdm
            })

        else:
            # License URL provided directly
            # Make license request (need to provide session for this)
            cdm.close(session_id)
            return web.json_response({
                "status": "error",
                "message": "Direct license URL not yet supported, omit license_url to auto-detect from service"
            }, status=400)

    except Exception:
        log.exception("Error in server-side decryption")
        return web.json_response({"status": "error", "message": "Internal server error during decryption"}, status=500)
