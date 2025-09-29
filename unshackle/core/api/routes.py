import logging

from aiohttp import web
from aiohttp_swagger3 import SwaggerDocs, SwaggerInfo, SwaggerUiSettings

from unshackle.core import __version__
from unshackle.core.api.handlers import (
    download_handler,
    list_titles_handler,
    list_tracks_handler,
    list_download_jobs_handler,
    get_download_job_handler,
    cancel_download_job_handler,
)
from unshackle.core.services import Services
from unshackle.core.update_checker import UpdateChecker

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
                      help:
                        type: string
                        nullable: true
      '500':
        description: Server error
    """
    try:
        service_tags = Services.get_tags()
        services_info = []

        for tag in service_tags:
            service_data = {"tag": tag, "aliases": [], "geofence": [], "title_regex": None, "help": None}

            try:
                service_module = Services.load(tag)

                if hasattr(service_module, "ALIASES"):
                    service_data["aliases"] = list(service_module.ALIASES)

                if hasattr(service_module, "GEOFENCE"):
                    service_data["geofence"] = list(service_module.GEOFENCE)

                if hasattr(service_module, "TITLE_RE"):
                    service_data["title_regex"] = service_module.TITLE_RE

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
    responses:
      '200':
        description: Download started
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
    List all download jobs.
    ---
    summary: List download jobs
    description: Get list of all download jobs with their status
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
      '500':
        description: Server error
    """
    return await list_download_jobs_handler({})


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
