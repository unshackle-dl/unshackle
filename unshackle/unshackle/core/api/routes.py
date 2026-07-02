import functools
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import click
from aiohttp import web
from aiohttp_swagger3 import SwaggerDocs, SwaggerInfo, SwaggerUiSettings

from unshackle.core import __version__
from unshackle.core.api.errors import APIError, APIErrorCode, build_error_response, handle_api_exception
from unshackle.core.api.handlers import (cancel_download_job_handler, clear_cache_handler,
                                         clear_finished_download_jobs_handler, clear_temp_handler,
                                         delete_history_handler, download_handler, download_history_handler,
                                         env_check_handler, get_allowed_services, get_download_job_handler,
                                         list_download_jobs_handler, list_titles_handler, list_tracks_handler,
                                         prioritize_download_job_handler, profiles_handler, refresh_services_handler,
                                         retry_download_job_handler, search_handler, server_config_handler,
                                         session_create_handler, session_delete_handler, session_info_handler,
                                         session_license_handler, session_prompt_get_handler,
                                         session_prompt_post_handler, session_segments_handler,
                                         session_titles_handler, session_tracks_handler)
from unshackle.core.services import Services
from unshackle.core.update_checker import UpdateChecker


@web.middleware
async def cors_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Add CORS headers to all responses."""
    # Handle preflight requests
    response: web.StreamResponse
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)

    # Add CORS headers
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Secret-Key, Authorization"
    response.headers["Access-Control-Max-Age"] = "3600"

    return response


log = logging.getLogger("api")

# Route handler signature: takes the request, returns a response.
Handler = Callable[[web.Request], Awaitable[web.Response]]


def api_handler(handler: Handler) -> Handler:
    """Wrap a route handler so any raised APIError becomes a structured error response."""

    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except APIError as e:
            return build_error_response(e, request.app.get("debug_api", False))

    return wrapper


@api_handler
async def health(request: web.Request) -> web.Response:
    """
    Health check endpoint.
    ---
    summary: Health check
    description: Get server health status, version info, and update availability
    responses:
      '200':
        description: Health status
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                  example: ok
                version:
                  type: string
                  example: "2.0.0"
                update_check:
                  type: object
                  properties:
                    update_available:
                      type: boolean
                      nullable: true
                    current_version:
                      type: string
                    latest_version:
                      type: string
                      nullable: true
    """
    try:
        latest_version = await UpdateChecker.check_for_updates(__version__)
        update_info = {
            "update_available": latest_version is not None,
            "current_version": __version__,
            "latest_version": latest_version,
        }
    except Exception as e:
        log.warning(f"Failed to check for updates: {e}")
        update_info = {"update_available": None, "current_version": __version__, "latest_version": None}

    return web.json_response({"status": "ok", "version": __version__, "update_check": update_info})


@api_handler
async def services(request: web.Request) -> web.Response:
    """
    List available services.
    ---
    summary: List services
    description: Get all available streaming services with their details
    responses:
      '200':
        description: List of services
        content:
          application/json:
            schema:
              type: object
              properties:
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
                      title_regex:
                        oneOf:
                          - type: string
                          - type: array
                            items:
                              type: string
                        nullable: true
                      url:
                        type: string
                        nullable: true
                        description: Service URL from short_help
                      help:
                        type: string
                        nullable: true
                        description: Full service documentation
      '500':
        description: Server error
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                  example: error
                error_code:
                  type: string
                  example: INTERNAL_ERROR
                message:
                  type: string
                  example: An unexpected error occurred
                details:
                  type: object
                timestamp:
                  type: string
                  format: date-time
                debug_info:
                  type: object
                  description: Only present when --debug-api flag is enabled
    """
    try:
        service_tags = Services.get_tags()
        allowed = get_allowed_services(request)
        if allowed is not None:
            service_tags = [t for t in service_tags if t in allowed]
        services_info = []

        for tag in service_tags:
            service_data: dict[str, Any] = {
                "tag": tag,
                "aliases": [],
                "geofence": [],
                "title_regex": None,
                "url": None,
                "help": None,
            }

            try:
                service_module = Services.load(tag)

                if hasattr(service_module, "ALIASES"):
                    service_data["aliases"] = list(service_module.ALIASES)

                if hasattr(service_module, "GEOFENCE"):
                    service_data["geofence"] = list(service_module.GEOFENCE)

                if hasattr(service_module, "TITLE_RE"):
                    title_re = service_module.TITLE_RE
                    # Handle different types of TITLE_RE
                    if isinstance(title_re, re.Pattern):
                        service_data["title_regex"] = title_re.pattern
                    elif isinstance(title_re, str):
                        service_data["title_regex"] = title_re
                    elif isinstance(title_re, (list, tuple)):
                        # Convert list/tuple of patterns to list of strings
                        patterns = []
                        for item in title_re:
                            if isinstance(item, re.Pattern):
                                patterns.append(item.pattern)
                            elif isinstance(item, str):
                                patterns.append(item)
                        service_data["title_regex"] = patterns if patterns else None

                if hasattr(service_module, "cli") and hasattr(service_module.cli, "short_help"):
                    service_data["url"] = service_module.cli.short_help

                if hasattr(service_module, "cli") and hasattr(service_module.cli, "params"):
                    cli_params = []
                    for param in service_module.cli.params:
                        param_info: dict = {"name": getattr(param, "name", None)}
                        if isinstance(param, click.Argument):
                            param_info["kind"] = "argument"
                            param_info["required"] = param.required
                        else:
                            param_info["kind"] = "option"
                            param_info["opts"] = list(param.opts) if hasattr(param, "opts") else []
                            param_info["is_flag"] = getattr(param, "is_flag", False)
                            default = param.default
                            if default is None:
                                pass
                            elif callable(default) or type(default).__name__ == "Sentinel":
                                default = None
                            elif hasattr(default, "name"):
                                default = default.name
                            elif not isinstance(default, (str, int, float, bool, list)):
                                default = str(default)
                            param_info["default"] = default
                            param_info["help"] = getattr(param, "help", None)
                            param_info["type"] = param.type.name if hasattr(param.type, "name") else str(param.type)
                        cli_params.append(param_info)
                    service_data["cli_params"] = cli_params

                if service_module.__doc__:
                    service_data["help"] = service_module.__doc__.strip()

                # Capability flags, derived from which Service hooks the service overrides.
                from unshackle.core.service import Service as _BaseService

                service_data["needs_auth"] = (
                    getattr(service_module, "authenticate", None) is not _BaseService.authenticate
                )
                service_data["has_search"] = getattr(service_module, "search", None) is not _BaseService.search
                service_data["has_drm"] = (
                    getattr(service_module, "get_widevine_license", None) is not _BaseService.get_widevine_license
                    or getattr(service_module, "get_playready_license", None) is not _BaseService.get_playready_license
                )

                # Prefer the service's explicit AUTH_METHODS; otherwise infer from authenticate().
                methods = []
                if service_data["needs_auth"]:
                    declared = getattr(service_module, "AUTH_METHODS", None)
                    if declared:
                        methods = list(declared)
                    else:
                        try:
                            import inspect as _inspect

                            src_lines = _inspect.getsource(service_module.authenticate).splitlines()
                            start = next((i + 1 for i, ln in enumerate(src_lines) if ln.rstrip().endswith(":")), 1)
                            body = "\n".join(src_lines[start:])
                            if "cookies" in body:
                                methods.append("cookies")
                            if "credential" in body:
                                methods.append("credentials")
                        except (OSError, TypeError):
                            pass
                        if not methods:
                            methods = ["cookies"]
                service_data["auth_methods"] = methods

            except Exception as e:
                log.warning(f"Could not load details for service {tag}: {e}")

            services_info.append(service_data)

        return web.json_response({"services": services_info})
    except Exception as e:
        log.exception("Error listing services")
        debug_mode = request.app.get("debug_api", False)
        return handle_api_exception(e, context={"operation": "list_services"}, debug_mode=debug_mode)


@api_handler
async def search(request: web.Request) -> web.Response:
    """
    Search for titles from a service.
    ---
    summary: Search for titles
    description: Search for titles by query string from a service
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            required:
              - service
              - query
            properties:
              service:
                type: string
                description: Service tag
              query:
                type: string
                description: Search query string
              profile:
                type: string
                description: Profile to use for credentials and cookies (default - None)
              proxy:
                type: string
                description: Proxy URI or country code (default - None)
              no_proxy:
                type: boolean
                description: Force disable all proxy use (default - false)
    responses:
      '200':
        description: Search results
        content:
          application/json:
            schema:
              type: object
              properties:
                results:
                  type: array
                  items:
                    type: object
                    properties:
                      id:
                        type: string
                        description: Title ID for use with other endpoints
                      title:
                        type: string
                        description: Title name
                      description:
                        type: string
                        description: Title description
                      label:
                        type: string
                        description: Informative label (e.g., availability, region)
                      url:
                        type: string
                        description: URL to the title page
                count:
                  type: integer
                  description: Number of results returned
      '400':
        description: Invalid request
    """
    try:
        data = await request.json()
    except Exception as e:
        return build_error_response(
            APIError(
                APIErrorCode.INVALID_INPUT,
                "Invalid JSON request body",
                details={"error": str(e)},
            ),
            request.app.get("debug_api", False),
        )

    try:
        return await search_handler(data, request)
    except Exception as e:
        log.exception("Error in search")
        debug_mode = request.app.get("debug_api", False)
        return handle_api_exception(e, context={"operation": "search"}, debug_mode=debug_mode)


@api_handler
async def list_titles(request: web.Request) -> web.Response:
    """
    List titles for a service and title ID.
    ---
    summary: List titles
    description: Get available titles for a service and title ID
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            required:
              - service
              - title_id
            properties:
              service:
                type: string
                description: Service tag
              title_id:
                type: string
                description: Title identifier
    responses:
      '200':
        description: List of titles
      '400':
        description: Invalid request (missing parameters, invalid service)
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                  example: error
                error_code:
                  type: string
                  example: INVALID_INPUT
                message:
                  type: string
                  example: Missing required parameter
                details:
                  type: object
                timestamp:
                  type: string
                  format: date-time
      '401':
        description: Authentication failed
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                  example: error
                error_code:
                  type: string
                  example: AUTH_FAILED
                message:
                  type: string
                details:
                  type: object
                timestamp:
                  type: string
                  format: date-time
      '404':
        description: Title not found
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                  example: error
                error_code:
                  type: string
                  example: NOT_FOUND
                message:
                  type: string
                details:
                  type: object
                timestamp:
                  type: string
                  format: date-time
      '500':
        description: Server error
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                  example: error
                error_code:
                  type: string
                  example: INTERNAL_ERROR
                message:
                  type: string
                details:
                  type: object
                timestamp:
                  type: string
                  format: date-time
    """
    try:
        data = await request.json()
    except Exception as e:
        return build_error_response(
            APIError(
                APIErrorCode.INVALID_INPUT,
                "Invalid JSON request body",
                details={"error": str(e)},
            ),
            request.app.get("debug_api", False),
        )

    return await list_titles_handler(data, request)


@api_handler
async def list_tracks(request: web.Request) -> web.Response:
    """
    List tracks for a title, separated by type.
    ---
    summary: List tracks
    description: Get available video, audio, and subtitle tracks for a title
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            required:
              - service
              - title_id
            properties:
              service:
                type: string
                description: Service tag
              title_id:
                type: string
                description: Title identifier
              wanted:
                type: string
                description: Specific episode/season (optional)
              proxy:
                type: string
                description: Proxy configuration (optional)
    responses:
      '200':
        description: Track information
      '400':
        description: Invalid request
    """
    try:
        data = await request.json()
    except Exception as e:
        return build_error_response(
            APIError(
                APIErrorCode.INVALID_INPUT,
                "Invalid JSON request body",
                details={"error": str(e)},
            ),
            request.app.get("debug_api", False),
        )

    return await list_tracks_handler(data, request)


@api_handler
async def download(request: web.Request) -> web.Response:
    """
    Download content based on provided parameters.
    ---
    summary: Download content
    description: Download video content based on specified parameters
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            required:
              - service
              - title_id
            properties:
              service:
                type: string
                description: Service tag
              title_id:
                type: string
                description: Title identifier
              profile:
                type: string
                description: Profile to use for credentials and cookies (default - None)
              quality:
                type: array
                items:
                  type: integer
                description: Download resolution(s) (default - best available)
              vcodec:
                oneOf:
                  - type: string
                  - type: array
                    items:
                      type: string
                description: Video codec(s) to download (e.g., "H265" or ["H264", "H265"]) - accepts H264, H265, AVC, HEVC, VP8, VP9, AV1, VC1 (default - None)
              acodec:
                oneOf:
                  - type: string
                  - type: array
                    items:
                      type: string
                description: Audio codec(s) to download (e.g., "AAC" or ["AAC", "EC3"]) - accepts AAC, AC3, EC3, AC4, OPUS, FLAC, ALAC, DTS, OGG (default - None)
              vbitrate:
                type: integer
                description: Video bitrate in kbps (default - None)
              abitrate:
                type: integer
                description: Audio bitrate in kbps (default - None)
              range:
                type: array
                items:
                  type: string
                description: Video color range (SDR, HDR10, HDR10+, HLG, DV, HYBRID) (default - ["SDR"])
              channels:
                type: number
                description: Audio channels (e.g., 2.0, 5.1, 7.1) (default - None)
              no_atmos:
                type: boolean
                description: Exclude Dolby Atmos audio tracks (default - false)
              wanted:
                type: array
                items:
                  type: string
                description: Wanted episodes (e.g., ["S01E01", "S01E02"]) (default - all)
              latest_episode:
                type: boolean
                description: Download only the single most recent episode (default - false)
              lang:
                type: array
                items:
                  type: string
                description: Language for video and audio (use 'orig' for original) (default - ["orig"])
              v_lang:
                type: array
                items:
                  type: string
                description: Language for video tracks only (default - [])
              a_lang:
                type: array
                items:
                  type: string
                description: Language for audio tracks only (default - [])
              s_lang:
                type: array
                items:
                  type: string
                description: Language for subtitle tracks (default - ["all"])
              require_subs:
                type: array
                items:
                  type: string
                description: Required subtitle languages (default - [])
              forced_subs:
                type: boolean
                description: Include forced subtitle tracks (default - false)
              exact_lang:
                type: boolean
                description: Use exact language matching (no variants) (default - false)
              sub_format:
                type: string
                description: Output subtitle format (SRT, VTT, etc.) (default - None)
              video_only:
                type: boolean
                description: Only download video tracks (default - false)
              audio_only:
                type: boolean
                description: Only download audio tracks (default - false)
              subs_only:
                type: boolean
                description: Only download subtitle tracks (default - false)
              chapters_only:
                type: boolean
                description: Only download chapters (default - false)
              no_subs:
                type: boolean
                description: Do not download subtitle tracks (default - false)
              no_audio:
                type: boolean
                description: Do not download audio tracks (default - false)
              no_chapters:
                type: boolean
                description: Do not download chapters (default - false)
              no_video:
                type: boolean
                description: Do not download video tracks (default - false)
              audio_description:
                type: boolean
                description: Download audio description tracks (default - false)
              slow:
                oneOf:
                  - type: boolean
                  - type: string
                description: Add randomized delay between downloads. `true` for default 60-120s, or `"MIN-MAX"` string (e.g., `"20-40"`). Min must be >= 20 (default - null)
              split_audio:
                type: boolean
                description: Create separate output files per audio codec instead of merging all audio (default - null)
              skip_dl:
                type: boolean
                description: Skip downloading, only retrieve decryption keys (default - false)
              export:
                type: boolean
                description: Export manifest, track URLs, keys, and subtitles to JSON in the exports directory (default - false)
              cdm_only:
                type: boolean
                description: Only use CDM for key retrieval (true) or only vaults (false) (default - None)
              proxy:
                type: string
                description: Proxy URI or country code (default - None)
              no_proxy:
                type: boolean
                description: Force disable all proxy use (default - false)
              no_proxy_download:
                type: boolean
                description: Bypass proxy for segment downloads only. Manifest, license, and auth still use proxy (default - false)
              tag:
                type: string
                description: Set the group tag to be used (default - None)
              tmdb_id:
                type: integer
                description: Use this TMDB ID for tagging (default - None)
              animeapi_id:
                type: string
                description: Anime database ID via AnimeAPI, e.g. mal:12345 (default - None)
              enrich:
                type: boolean
                description: Override show title and year from external source (default - false)
              no_folder:
                type: boolean
                description: Disable folder creation for TV shows (default - false)
              no_source:
                type: boolean
                description: Disable source tag from output file name (default - false)
              no_mux:
                type: boolean
                description: Do not mux tracks into a container file (default - false)
              workers:
                type: integer
                description: Max workers/threads per track download (default - None)
              downloads:
                type: integer
                description: Amount of tracks to download concurrently (default - 1)
              best_available:
                type: boolean
                description: Continue with best available if requested quality unavailable (default - false)
              worst:
                type: boolean
                description: Select the lowest bitrate track within the specified quality. Requires `quality` (default - false)
              repack:
                type: boolean
                description: Add REPACK tag to the output filename (default - false)
              imdb_id:
                type: string
                description: Use this IMDB ID (e.g. tt1375666) for tagging (default - None)
              output_dir:
                type: string
                description: Override the output directory for this download (default - None)
              no_cache:
                type: boolean
                description: Bypass title cache for this download (default - false)
              reset_cache:
                type: boolean
                description: Clear title cache before fetching (default - false)
    responses:
      '202':
        description: Download job created
        content:
          application/json:
            schema:
              type: object
              properties:
                job_id:
                  type: string
                status:
                  type: string
                created_time:
                  type: string
      '400':
        description: Invalid request
    """
    try:
        data = await request.json()
    except Exception as e:
        return build_error_response(
            APIError(
                APIErrorCode.INVALID_INPUT,
                "Invalid JSON request body",
                details={"error": str(e)},
            ),
            request.app.get("debug_api", False),
        )

    return await download_handler(data, request)


@api_handler
async def download_jobs(request: web.Request) -> web.Response:
    """
    List all download jobs with optional filtering and sorting.
    ---
    summary: List download jobs
    description: Get list of all download jobs with their status, with optional filtering by status/service and sorting
    parameters:
      - name: status
        in: query
        required: false
        schema:
          type: string
          enum: [queued, downloading, completed, failed, cancelled]
        description: Filter jobs by status
      - name: service
        in: query
        required: false
        schema:
          type: string
        description: Filter jobs by service tag
      - name: sort_by
        in: query
        required: false
        schema:
          type: string
          enum: [created_time, started_time, completed_time, progress, status, service]
          default: created_time
        description: Field to sort by
      - name: sort_order
        in: query
        required: false
        schema:
          type: string
          enum: [asc, desc]
          default: desc
        description: Sort order (ascending or descending)
      - name: full
        in: query
        required: false
        schema:
          type: string
          enum: ["true", "false"]
          default: "false"
        description: When "true", include full job details (parameters, timestamps, output files, errors) per job
    responses:
      '200':
        description: List of download jobs
        content:
          application/json:
            schema:
              type: object
              properties:
                jobs:
                  type: array
                  items:
                    type: object
                    properties:
                      job_id:
                        type: string
                      status:
                        type: string
                      created_time:
                        type: string
                      service:
                        type: string
                      title_id:
                        type: string
                      progress:
                        type: number
      '400':
        description: Invalid query parameters
      '500':
        description: Server error
    """
    # Extract query parameters
    query_params = {
        "status": request.query.get("status"),
        "service": request.query.get("service"),
        "sort_by": request.query.get("sort_by", "created_time"),
        "sort_order": request.query.get("sort_order", "desc"),
        "full": request.query.get("full"),
    }
    return await list_download_jobs_handler(query_params, request)


@api_handler
async def download_job_detail(request: web.Request) -> web.Response:
    """
    Get download job details.
    ---
    summary: Get download job
    description: Get detailed information about a specific download job
    parameters:
      - name: job_id
        in: path
        required: true
        schema:
          type: string
    responses:
      '200':
        description: Download job details
      '404':
        description: Job not found
      '500':
        description: Server error
    """
    job_id = request.match_info["job_id"]
    return await get_download_job_handler(job_id, request)


@api_handler
async def cancel_download_job(request: web.Request) -> web.Response:
    """
    Cancel or remove download job.
    ---
    summary: Cancel or remove download job
    description: Cancel a queued or running download job, or remove a completed/failed/cancelled job entirely
    parameters:
      - name: job_id
        in: path
        required: true
        schema:
          type: string
    responses:
      '200':
        description: Job cancelled successfully
      '204':
        description: Terminal job removed from the manager
      '400':
        description: Job cannot be cancelled
      '404':
        description: Job not found
      '500':
        description: Server error
    """
    job_id = request.match_info["job_id"]
    return await cancel_download_job_handler(job_id, request)


@api_handler
async def clear_finished_download_jobs(request: web.Request) -> web.Response:
    """
    Clear finished download jobs.
    ---
    summary: Clear finished download jobs
    description: Remove all completed, failed, and cancelled jobs from the manager
    responses:
      '200':
        description: Finished jobs removed
        content:
          application/json:
            schema:
              type: object
              properties:
                removed:
                  type: integer
                  description: Number of jobs removed
      '500':
        description: Server error
    """
    return await clear_finished_download_jobs_handler(request)


@api_handler
async def retry_download_job(request: web.Request) -> web.Response:
    """
    Retry download job.
    ---
    summary: Retry download job
    description: Enqueue a new job reusing a completed, failed, or cancelled job's service, title, and parameters
    parameters:
      - name: job_id
        in: path
        required: true
        schema:
          type: string
    responses:
      '202':
        description: New job queued
        content:
          application/json:
            schema:
              type: object
              properties:
                job_id:
                  type: string
                status:
                  type: string
                created_time:
                  type: string
      '404':
        description: Job not found
      '409':
        description: Job is not in a terminal state
      '500':
        description: Server error
    """
    job_id = request.match_info["job_id"]
    return await retry_download_job_handler(job_id, request)


@api_handler
async def prioritize_download_job(request: web.Request) -> web.Response:
    """
    Prioritize download job.
    ---
    summary: Prioritize download job
    description: Move a queued job to the front of the download queue
    parameters:
      - name: job_id
        in: path
        required: true
        schema:
          type: string
    responses:
      '200':
        description: Job moved to front of queue
        content:
          application/json:
            schema:
              type: object
              properties:
                job_id:
                  type: string
                position:
                  type: string
                  example: front
      '404':
        description: Job not found
      '409':
        description: Job is not queued
      '500':
        description: Server error
    """
    job_id = request.match_info["job_id"]
    return await prioritize_download_job_handler(job_id, request)


@api_handler
async def profiles(request: web.Request) -> web.Response:
    """
    List configured credential profiles per service.
    ---
    summary: List credential profiles
    description: >
      Enumerate named credential profiles configured per service (usable as the `profile`
      parameter). Only services whose credentials are a mapping of profile-name to credential
      are listed (including a `default` key if present); a service configured with a single
      plain (unnamed) credential is omitted entirely. Filtered by the caller's service allowlist.
    responses:
      '200':
        description: Profiles per service
        content:
          application/json:
            schema:
              type: object
              properties:
                profiles:
                  type: object
                  additionalProperties:
                    type: array
                    items:
                      type: string
      '500':
        description: Server error
    """
    return await profiles_handler(request)


@api_handler
async def server_config(request: web.Request) -> web.Response:
    """
    Get the redacted effective server configuration.
    ---
    summary: Get server config
    description: >
      Read-only, redacted view of the effective server configuration for display in a UI
      settings page. Secrets (api_secret, users, credentials, tokens) are never included;
      secret-looking keys inside `dl` are masked.
    responses:
      '200':
        description: Redacted server configuration
        content:
          application/json:
            schema:
              type: object
              properties:
                config:
                  type: object
                  properties:
                    dl:
                      type: object
                      description: Default dl parameters from config (secret-looking keys masked)
                    serve:
                      type: object
                      properties:
                        max_concurrent_downloads:
                          type: integer
                        job_retention_hours:
                          type: integer
                        services:
                          type: array
                          items:
                            type: string
                          nullable: true
                        remote_only:
                          type: boolean
                        cdm_overrides:
                          nullable: true
                          description: List of permitted CDM device names, true, or null
                        allow_job_credentials:
                          type: boolean
                    directories:
                      type: object
                      properties:
                        downloads:
                          type: string
                        temp:
                          type: string
                        cache:
                          type: string
                    services:
                      type: array
                      items:
                        type: string
                      description: Available service tags (allowlist-filtered)
      '500':
        description: Server error
    """
    return await server_config_handler(request)


@api_handler
async def download_history(request: web.Request) -> web.Response:
    """
    Get persistent download history.
    ---
    summary: Get download history
    description: >
      Read the persisted job history (jobs that reached a terminal state), newest first.
      Corrupt lines in the history file are skipped; a missing file yields an empty list.
    parameters:
      - name: limit
        in: query
        required: false
        schema:
          type: integer
          minimum: 1
          default: 100
        description: Maximum number of entries to return
      - name: service
        in: query
        required: false
        schema:
          type: string
        description: Filter entries by service tag (case-insensitive)
    responses:
      '200':
        description: History entries, newest first
        content:
          application/json:
            schema:
              type: object
              properties:
                history:
                  type: array
                  items:
                    type: object
                    properties:
                      job_id:
                        type: string
                      service:
                        type: string
                      title_id:
                        type: string
                      title:
                        type: string
                        nullable: true
                      status:
                        type: string
                        enum: [completed, failed, cancelled]
                      created_time:
                        type: string
                      completed_time:
                        type: string
                        nullable: true
                      output_files:
                        type: array
                        items:
                          type: string
                      error_message:
                        type: string
                        nullable: true
                count:
                  type: integer
      '400':
        description: Invalid query parameters
      '500':
        description: Server error
    """
    query_params = {"limit": request.query.get("limit"), "service": request.query.get("service")}
    return await download_history_handler(query_params, request)


@api_handler
async def delete_history(request: web.Request) -> web.Response:
    """
    Delete a download history entry.
    ---
    summary: Delete download history entry
    description: Remove a single persisted history entry by job_id.
    parameters:
      - name: job_id
        in: path
        required: true
        schema:
          type: string
    responses:
      '204':
        description: History entry removed
      '404':
        description: History entry not found
      '500':
        description: Server error
    """
    return await delete_history_handler(request.match_info["job_id"], request)


@api_handler
async def maintenance_clear_cache(request: web.Request) -> web.Response:
    """
    Clear the cache directory.
    ---
    summary: Clear cache
    description: Delete the contents of the cache directory (recreated empty). Freed size is best effort.
    responses:
      '200':
        description: Cache cleared
        content:
          application/json:
            schema:
              type: object
              properties:
                cleared:
                  type: boolean
                freed_bytes:
                  type: integer
      '500':
        description: Server error
    """
    return await clear_cache_handler(request)


@api_handler
async def maintenance_clear_temp(request: web.Request) -> web.Response:
    """
    Clear the temp directory.
    ---
    summary: Clear temp
    description: Delete the contents of the temp directory (recreated empty). Freed size is best effort.
    responses:
      '200':
        description: Temp cleared
        content:
          application/json:
            schema:
              type: object
              properties:
                cleared:
                  type: boolean
                freed_bytes:
                  type: integer
      '500':
        description: Server error
    """
    return await clear_temp_handler(request)


@api_handler
async def maintenance_refresh_services(request: web.Request) -> web.Response:
    """
    Refresh configured service repos.
    ---
    summary: Refresh service repos
    description: >
      Force-sync (git pull) every service repo configured in directories.services.
      `refreshed` is true when all repos synced (or none are configured); per-repo
      results are listed under `repos`.
    responses:
      '200':
        description: Refresh results
        content:
          application/json:
            schema:
              type: object
              properties:
                refreshed:
                  type: boolean
                repos:
                  type: array
                  items:
                    type: object
                    properties:
                      spec:
                        type: string
                      updated:
                        type: boolean
                      changes:
                        type: array
                        items:
                          type: string
      '500':
        description: Server error
    """
    return await refresh_services_handler(request)


@api_handler
async def env_check(request: web.Request) -> web.Response:
    """
    Check environment dependencies.
    ---
    summary: Environment check
    description: Report install status of the binaries `env check` inspects, with best-effort versions.
    responses:
      '200':
        description: Dependency check results
        content:
          application/json:
            schema:
              type: object
              properties:
                checks:
                  type: array
                  items:
                    type: object
                    properties:
                      name:
                        type: string
                      installed:
                        type: boolean
                      version:
                        type: string
                        nullable: true
                      required:
                        type: boolean
      '500':
        description: Server error
    """
    return await env_check_handler(request)


@api_handler
async def session_create(request: web.Request) -> web.Response:
    """
    Create a remote-dl session.
    ---
    summary: Create session
    description: Authenticate with a service, get titles, tracks, and chapters in one call
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            additionalProperties: true
            required:
              - service
              - title_id
            properties:
              service:
                type: string
              title_id:
                type: string
              credentials:
                type: object
                additionalProperties: true
              cookies:
                type: string
              proxy:
                type: string
              no_proxy:
                type: boolean
              profile:
                type: string
              cache:
                type: object
                additionalProperties: true
    responses:
      '200':
        description: Session created with titles, tracks, and chapters
      '400':
        description: Invalid request
      '401':
        description: Authentication failed
    """
    try:
        data = await request.json()
    except Exception as e:
        return build_error_response(
            APIError(APIErrorCode.INVALID_INPUT, "Invalid JSON request body", details={"error": str(e)}),
            request.app.get("debug_api", False),
        )
    try:
        return await session_create_handler(data, request)
    except Exception as e:
        log.exception("Error in session create")
        return handle_api_exception(
            e, context={"operation": "session_create"}, debug_mode=request.app.get("debug_api", False)
        )


@api_handler
async def session_titles(request: web.Request) -> web.Response:
    """
    Get titles for an authenticated session.
    ---
    summary: Get titles
    description: Fetch titles from the authenticated service session
    parameters:
      - name: session_id
        in: path
        required: true
        schema:
          type: string
    responses:
      '200':
        description: List of titles
      '404':
        description: Session not found
    """
    session_id = request.match_info["session_id"]
    try:
        return await session_titles_handler(session_id, request)
    except Exception as e:
        log.exception("Error in session titles")
        return handle_api_exception(
            e, context={"operation": "session_titles"}, debug_mode=request.app.get("debug_api", False)
        )


@api_handler
async def session_tracks(request: web.Request) -> web.Response:
    """
    Get tracks and chapters for a specific title.
    ---
    summary: Get tracks
    description: Fetch tracks and chapters for a title in the session
    parameters:
      - name: session_id
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
              - title_id
            properties:
              title_id:
                type: string
                description: ID of the title to get tracks for
    responses:
      '200':
        description: Tracks and chapters for the title
      '404':
        description: Session or title not found
    """
    session_id = request.match_info["session_id"]
    try:
        data = await request.json()
    except Exception as e:
        return build_error_response(
            APIError(APIErrorCode.INVALID_INPUT, "Invalid JSON request body", details={"error": str(e)}),
            request.app.get("debug_api", False),
        )
    try:
        return await session_tracks_handler(data, session_id, request)
    except Exception as e:
        log.exception("Error in session tracks")
        return handle_api_exception(
            e, context={"operation": "session_tracks"}, debug_mode=request.app.get("debug_api", False)
        )


@api_handler
async def session_segments(request: web.Request) -> web.Response:
    """
    Resolve segment URLs for selected tracks.
    ---
    summary: Resolve segments
    description: Get download URLs, DRM info, and headers for selected tracks
    parameters:
      - name: session_id
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
              - track_ids
            properties:
              track_ids:
                type: array
                items:
                  type: string
                description: List of track IDs to resolve
    responses:
      '200':
        description: Segment URLs and DRM info for each track
      '404':
        description: Session or track not found
    """
    session_id = request.match_info["session_id"]
    try:
        data = await request.json()
    except Exception as e:
        return build_error_response(
            APIError(APIErrorCode.INVALID_INPUT, "Invalid JSON request body", details={"error": str(e)}),
            request.app.get("debug_api", False),
        )
    try:
        return await session_segments_handler(data, session_id, request)
    except Exception as e:
        log.exception("Error in session segments")
        return handle_api_exception(
            e, context={"operation": "session_segments"}, debug_mode=request.app.get("debug_api", False)
        )


@api_handler
async def session_license(request: web.Request) -> web.Response:
    """
    Proxy DRM license through authenticated service.
    ---
    summary: Proxy license
    description: Forward a CDM challenge to the service's license endpoint
    parameters:
      - name: session_id
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
              - track_id
              - challenge
            properties:
              track_id:
                type: string
                description: Track ID this license is for
              challenge:
                type: string
                description: Base64-encoded CDM challenge
              drm_type:
                type: string
                enum: [widevine, playready]
                description: DRM type (default widevine)
    responses:
      '200':
        description: License response
      '404':
        description: Session or track not found
    """
    session_id = request.match_info["session_id"]
    try:
        data = await request.json()
    except Exception as e:
        return build_error_response(
            APIError(APIErrorCode.INVALID_INPUT, "Invalid JSON request body", details={"error": str(e)}),
            request.app.get("debug_api", False),
        )
    try:
        return await session_license_handler(data, session_id, request)
    except Exception as e:
        log.exception("Error in session license")
        return handle_api_exception(
            e, context={"operation": "session_license"}, debug_mode=request.app.get("debug_api", False)
        )


@api_handler
async def session_info(request: web.Request) -> web.Response:
    """
    Get session info.
    ---
    summary: Session info
    description: Check session validity and get metadata
    parameters:
      - name: session_id
        in: path
        required: true
        schema:
          type: string
    responses:
      '200':
        description: Session info
      '404':
        description: Session not found
    """
    session_id = request.match_info["session_id"]
    return await session_info_handler(session_id, request)


@api_handler
async def session_delete(request: web.Request) -> web.Response:
    """
    Delete a session.
    ---
    summary: Delete session
    description: Clean up a remote-dl session
    parameters:
      - name: session_id
        in: path
        required: true
        schema:
          type: string
    responses:
      '200':
        description: Session deleted
      '404':
        description: Session not found
    """
    session_id = request.match_info["session_id"]
    return await session_delete_handler(session_id, request)


@api_handler
async def session_prompt_get(request: web.Request) -> web.Response:
    """
    Poll for pending interactive prompts during authentication.
    ---
    summary: Get auth prompt
    description: Poll for pending interactive prompts (OTP, device code, PIN) during session authentication
    parameters:
      - name: session_id
        in: path
        required: true
        schema:
          type: string
    responses:
      '200':
        description: Auth status and optional prompt
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                  enum: [authenticating, pending_input, authenticated, failed]
                prompt:
                  type: string
                  description: Prompt to display to the user (only when status is pending_input)
                error:
                  type: string
                  description: Error message (only when status is failed)
      '404':
        description: Session not found
    """
    session_id = request.match_info["session_id"]
    try:
        return await session_prompt_get_handler(session_id, request)
    except Exception as e:
        log.exception("Error in session prompt get")
        return handle_api_exception(
            e, context={"operation": "session_prompt_get"}, debug_mode=request.app.get("debug_api", False)
        )


@api_handler
async def session_prompt_submit(request: web.Request) -> web.Response:
    """
    Submit a response to a pending interactive prompt.
    ---
    summary: Submit prompt response
    description: Submit user input (OTP code, PIN, device code confirmation) to unblock server authentication
    parameters:
      - name: session_id
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
              - response
            properties:
              response:
                type: string
                description: User's response to the prompt
    responses:
      '200':
        description: Response accepted
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                  example: accepted
      '400':
        description: No prompt pending or invalid request
      '404':
        description: Session not found
    """
    session_id = request.match_info["session_id"]
    try:
        data = await request.json()
    except Exception as e:
        return build_error_response(
            APIError(APIErrorCode.INVALID_INPUT, "Invalid JSON request body", details={"error": str(e)}),
            request.app.get("debug_api", False),
        )
    try:
        return await session_prompt_post_handler(data, session_id, request)
    except Exception as e:
        log.exception("Error in session prompt submit")
        return handle_api_exception(
            e, context={"operation": "session_prompt_submit"}, debug_mode=request.app.get("debug_api", False)
        )


# Single source of truth for all API routes. `remote` marks endpoints exposed in
# --remote-only server mode; full mode registers every route. Both setup_routes and
# setup_swagger derive from this table so the live routes and the docs cannot drift.
ROUTES: list[tuple[str, str, Handler, bool]] = [
    ("GET", "/api/health", health, True),
    ("GET", "/api/services", services, True),
    ("POST", "/api/search", search, True),
    ("POST", "/api/list-titles", list_titles, False),
    ("POST", "/api/list-tracks", list_tracks, False),
    ("POST", "/api/download", download, False),
    ("GET", "/api/download/jobs", download_jobs, False),
    ("POST", "/api/download/jobs/clear-finished", clear_finished_download_jobs, False),
    ("GET", "/api/download/jobs/{job_id}", download_job_detail, False),
    ("DELETE", "/api/download/jobs/{job_id}", cancel_download_job, False),
    ("POST", "/api/download/jobs/{job_id}/retry", retry_download_job, False),
    ("POST", "/api/download/jobs/{job_id}/priority", prioritize_download_job, False),
    ("GET", "/api/profiles", profiles, False),
    ("GET", "/api/config", server_config, False),
    ("GET", "/api/history", download_history, False),
    ("DELETE", "/api/history/{job_id}", delete_history, False),
    ("POST", "/api/maintenance/clear-cache", maintenance_clear_cache, False),
    ("POST", "/api/maintenance/clear-temp", maintenance_clear_temp, False),
    ("POST", "/api/maintenance/refresh-services", maintenance_refresh_services, False),
    ("GET", "/api/env/check", env_check, False),
    ("POST", "/api/session/create", session_create, True),
    ("GET", "/api/session/{session_id}/titles", session_titles, True),
    ("POST", "/api/session/{session_id}/tracks", session_tracks, True),
    ("POST", "/api/session/{session_id}/segments", session_segments, True),
    ("POST", "/api/session/{session_id}/license", session_license, True),
    ("GET", "/api/session/{session_id}/prompt", session_prompt_get, True),
    ("POST", "/api/session/{session_id}/prompt", session_prompt_submit, True),
    ("GET", "/api/session/{session_id}", session_info, True),
    ("DELETE", "/api/session/{session_id}", session_delete, True),
]


def setup_routes(app: web.Application, remote_only: bool = False) -> None:
    """Setup API routes. When remote_only=True, only expose remote session endpoints."""
    add: dict[str, Callable[..., Any]] = {
        "GET": app.router.add_get,
        "POST": app.router.add_post,
        "DELETE": app.router.add_delete,
    }
    for method, path, handler, remote in ROUTES:
        if remote_only and not remote:
            continue
        add[method](path, handler)


def setup_swagger(app: web.Application) -> None:
    """Setup Swagger UI documentation."""
    swagger = SwaggerDocs(
        app,
        swagger_ui_settings=SwaggerUiSettings(path="/api/docs/"),
        info=SwaggerInfo(
            title="Unshackle REST API",
            version=__version__,
            description="REST API for Unshackle - Modular Movie, TV, and Music Archival Software",
        ),
    )

    route: dict[str, Callable[..., Any]] = {"GET": web.get, "POST": web.post, "DELETE": web.delete}
    swagger.add_routes([route[method](path, handler) for method, path, handler, _ in ROUTES])
