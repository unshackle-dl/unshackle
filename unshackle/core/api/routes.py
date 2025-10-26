import logging

from aiohttp import web
from aiohttp_swagger3 import SwaggerDocs, SwaggerInfo, SwaggerUiSettings

from unshackle.core import __version__
from unshackle.core.api.handlers import (cancel_download_job_handler, download_handler, get_download_job_handler,
                                         list_download_jobs_handler, list_titles_handler, list_tracks_handler)
from unshackle.core.services import Services
from unshackle.core.update_checker import UpdateChecker


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Add CORS headers to all responses."""
    # Handle preflight requests
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)

    # Add CORS headers
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
    response.headers["Access-Control-Max-Age"] = "3600"

    return response


log = logging.getLogger("api")


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
    """
    try:
        service_tags = Services.get_tags()
        services_info = []

        for tag in service_tags:
            service_data = {"tag": tag, "aliases": [], "geofence": [], "title_regex": None, "url": None, "help": None}

            try:
                service_module = Services.load(tag)

                if hasattr(service_module, "ALIASES"):
                    service_data["aliases"] = list(service_module.ALIASES)

                if hasattr(service_module, "GEOFENCE"):
                    service_data["geofence"] = list(service_module.GEOFENCE)

                if hasattr(service_module, "TITLE_RE"):
                    service_data["title_regex"] = service_module.TITLE_RE

                if hasattr(service_module, "cli") and hasattr(service_module.cli, "short_help"):
                    service_data["url"] = service_module.cli.short_help

                if service_module.__doc__:
                    service_data["help"] = service_module.__doc__.strip()

            except Exception as e:
                log.warning(f"Could not load details for service {tag}: {e}")

            services_info.append(service_data)

        return web.json_response({"services": services_info})
    except Exception as e:
        log.exception("Error listing services")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


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
        description: Invalid request
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON request body"}, status=400)

    return await list_titles_handler(data)


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
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON request body"}, status=400)

    return await list_tracks_handler(data)


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
                description: Profile to use for credentials and cookies
              quality:
                type: array
                items:
                  type: integer
                description: Download resolution(s), defaults to best available
              vcodec:
                type: string
                description: Video codec to download (e.g., H264, H265, VP9, AV1)
              acodec:
                type: string
                description: Audio codec to download (e.g., AAC, AC3, EAC3)
              vbitrate:
                type: integer
                description: Video bitrate in kbps
              abitrate:
                type: integer
                description: Audio bitrate in kbps
              range:
                type: array
                items:
                  type: string
                description: Video color range (SDR, HDR10, DV)
              channels:
                type: number
                description: Audio channels (e.g., 2.0, 5.1, 7.1)
              no_atmos:
                type: boolean
                description: Exclude Dolby Atmos audio tracks
              wanted:
                type: array
                items:
                  type: string
                description: Wanted episodes (e.g., ["S01E01", "S01E02"])
              latest_episode:
                type: boolean
                description: Download only the single most recent episode
              lang:
                type: array
                items:
                  type: string
                description: Language for video and audio (use 'orig' for original)
              v_lang:
                type: array
                items:
                  type: string
                description: Language for video tracks only
              a_lang:
                type: array
                items:
                  type: string
                description: Language for audio tracks only
              s_lang:
                type: array
                items:
                  type: string
                description: Language for subtitle tracks (default is 'all')
              require_subs:
                type: array
                items:
                  type: string
                description: Required subtitle languages
              forced_subs:
                type: boolean
                description: Include forced subtitle tracks
              exact_lang:
                type: boolean
                description: Use exact language matching (no variants)
              sub_format:
                type: string
                description: Output subtitle format (SRT, VTT, etc.)
              video_only:
                type: boolean
                description: Only download video tracks
              audio_only:
                type: boolean
                description: Only download audio tracks
              subs_only:
                type: boolean
                description: Only download subtitle tracks
              chapters_only:
                type: boolean
                description: Only download chapters
              no_subs:
                type: boolean
                description: Do not download subtitle tracks
              no_audio:
                type: boolean
                description: Do not download audio tracks
              no_chapters:
                type: boolean
                description: Do not download chapters
              audio_description:
                type: boolean
                description: Download audio description tracks
              slow:
                type: boolean
                description: Add 60-120s delay between downloads
              skip_dl:
                type: boolean
                description: Skip downloading, only retrieve decryption keys
              export:
                type: string
                description: Path to export decryption keys as JSON
              cdm_only:
                type: boolean
                description: Only use CDM for key retrieval (true) or only vaults (false)
              proxy:
                type: string
                description: Proxy URI or country code
              no_proxy:
                type: boolean
                description: Force disable all proxy use
              tag:
                type: string
                description: Set the group tag to be used
              tmdb_id:
                type: integer
                description: Use this TMDB ID for tagging
              tmdb_name:
                type: boolean
                description: Rename titles using TMDB name
              tmdb_year:
                type: boolean
                description: Use release year from TMDB
              no_folder:
                type: boolean
                description: Disable folder creation for TV shows
              no_source:
                type: boolean
                description: Disable source tag from output file name
              no_mux:
                type: boolean
                description: Do not mux tracks into a container file
              workers:
                type: integer
                description: Max workers/threads per track download
              downloads:
                type: integer
                description: Amount of tracks to download concurrently
              best_available:
                type: boolean
                description: Continue with best available if requested quality unavailable
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
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON request body"}, status=400)

    return await download_handler(data)


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
    }
    return await list_download_jobs_handler(query_params)


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
    return await get_download_job_handler(job_id)


async def cancel_download_job(request: web.Request) -> web.Response:
    """
    Cancel download job.
    ---
    summary: Cancel download job
    description: Cancel a queued or running download job
    parameters:
      - name: job_id
        in: path
        required: true
        schema:
          type: string
    responses:
      '200':
        description: Job cancelled successfully
      '400':
        description: Job cannot be cancelled
      '404':
        description: Job not found
      '500':
        description: Server error
    """
    job_id = request.match_info["job_id"]
    return await cancel_download_job_handler(job_id)


def setup_routes(app: web.Application) -> None:
    """Setup all API routes."""
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/services", services)
    app.router.add_post("/api/list-titles", list_titles)
    app.router.add_post("/api/list-tracks", list_tracks)
    app.router.add_post("/api/download", download)
    app.router.add_get("/api/download/jobs", download_jobs)
    app.router.add_get("/api/download/jobs/{job_id}", download_job_detail)
    app.router.add_delete("/api/download/jobs/{job_id}", cancel_download_job)


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

    # Add routes with OpenAPI documentation
    swagger.add_routes(
        [
            web.get("/api/health", health),
            web.get("/api/services", services),
            web.post("/api/list-titles", list_titles),
            web.post("/api/list-tracks", list_tracks),
            web.post("/api/download", download),
            web.get("/api/download/jobs", download_jobs),
            web.get("/api/download/jobs/{job_id}", download_job_detail),
            web.delete("/api/download/jobs/{job_id}", cancel_download_job),
        ]
    )
