import logging
import subprocess

import click
from aiohttp import web

from unshackle.core import binaries
from unshackle.core.api import cors_middleware, setup_routes, setup_swagger
from unshackle.core.config import config
from unshackle.core.constants import context_settings


@click.command(
    short_help="Serve your Local Widevine/PlayReady Devices and REST API for Remote Access.",
    context_settings=context_settings,
)
@click.option("-h", "--host", type=str, default="127.0.0.1", help="Host to serve from.")
@click.option("-p", "--port", type=int, default=8786, help="Port to serve from.")
@click.option("--caddy", is_flag=True, default=False, help="Also serve with Caddy.")
@click.option(
    "--api-only", is_flag=True, default=False, help="Serve only the REST API, not pywidevine/pyplayready CDM."
)
@click.option("--no-widevine", is_flag=True, default=False, help="Disable Widevine CDM endpoints.")
@click.option("--no-playready", is_flag=True, default=False, help="Disable PlayReady CDM endpoints.")
@click.option("--no-key", is_flag=True, default=False, help="Disable API key authentication (allows all requests).")
@click.option(
    "--debug-api",
    is_flag=True,
    default=False,
    help="Include technical debug information (tracebacks, stderr) in API error responses.",
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    help="Enable debug logging for API operations.",
)
def serve(
    host: str,
    port: int,
    caddy: bool,
    api_only: bool,
    no_widevine: bool,
    no_playready: bool,
    no_key: bool,
    debug_api: bool,
    debug: bool,
) -> None:
    """
    Serve your Local Widevine and PlayReady Devices and REST API for Remote Access.

    \b
    CDM ENDPOINTS:
    - Widevine: /{device}/open, /{device}/close/{session_id}, etc.
    - PlayReady: /playready/{device}/open, /playready/{device}/close/{session_id}, etc.

    \b
    You may serve with Caddy at the same time with --caddy. You can use Caddy
    as a reverse-proxy to serve with HTTPS. The config used will be the Caddyfile
    next to the unshackle config.

    \b
    DEVICE CONFIGURATION:
    WVD files are auto-loaded from the WVDs directory, PRD files from the PRDs directory.
    Configure user access in unshackle.yaml:

    \b
    serve:
      api_secret: "your-api-secret"
      users:
        your-secret-key:
          devices: ["device_name"]  # Widevine devices
          playready_devices: ["device_name"]  # PlayReady devices
          username: user
    """
    from pyplayready.remote import serve as pyplayready_serve
    from pywidevine import serve as pywidevine_serve

    log = logging.getLogger("serve")

    if debug:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s - %(levelname)s - %(message)s")
        log.info("Debug logging enabled for API operations")
    else:
        logging.getLogger("api").setLevel(logging.WARNING)
        logging.getLogger("api.remote").setLevel(logging.WARNING)

    if not no_key:
        api_secret = config.serve.get("api_secret")
        if not api_secret:
            raise click.ClickException(
                "API secret key is not configured. Please add 'api_secret' to the 'serve' section in your config."
            )
    else:
        api_secret = None
        log.warning("Running with --no-key: Authentication is DISABLED for all API endpoints!")

    if debug_api:
        log.warning("Running with --debug-api: Error responses will include technical debug information!")

    if api_only and (no_widevine or no_playready):
        raise click.ClickException("Cannot use --api-only with --no-widevine or --no-playready.")

    if caddy:
        if not binaries.Caddy:
            raise click.ClickException('Caddy executable "caddy" not found but is required for --caddy.')
        caddy_p = subprocess.Popen(
            [binaries.Caddy, "run", "--config", str(config.directories.user_configs / "Caddyfile")]
        )
    else:
        caddy_p = None

    try:
        if not config.serve.get("devices"):
            config.serve["devices"] = []
        config.serve["devices"].extend(list(config.directories.wvds.glob("*.wvd")))

        if not config.serve.get("playready_devices"):
            config.serve["playready_devices"] = []
        config.serve["playready_devices"].extend(list(config.directories.prds.glob("*.prd")))

        if api_only:
            log.info("Starting REST API server (pywidevine/pyplayready CDM disabled)")
            if no_key:
                app = web.Application(middlewares=[cors_middleware])
                app["config"] = {"users": {}}
            else:
                app = web.Application(middlewares=[cors_middleware, pywidevine_serve.authentication])
                app["config"] = {"users": {api_secret: {"devices": [], "username": "api_user"}}}
            app["debug_api"] = debug_api
            setup_routes(app)
            setup_swagger(app)
            log.info(f"REST API endpoints available at http://{host}:{port}/api/")
            log.info(f"Swagger UI available at http://{host}:{port}/api/docs/")
            log.info("(Press CTRL+C to quit)")
            web.run_app(app, host=host, port=port, print=None)
        else:
            serve_widevine = not no_widevine
            serve_playready = not no_playready

            serve_config = dict(config.serve)
            wvd_devices = serve_config.get("devices", []) if serve_widevine else []
            prd_devices = serve_config.get("playready_devices", []) if serve_playready else []

            cdm_parts = []
            if serve_widevine:
                cdm_parts.append("pywidevine CDM")
            if serve_playready:
                cdm_parts.append("pyplayready CDM")
            log.info(f"Starting integrated server ({' + '.join(cdm_parts)} + REST API)")

            wvd_device_names = [d.stem if hasattr(d, "stem") else str(d) for d in wvd_devices]
            prd_device_names = [d.stem if hasattr(d, "stem") else str(d) for d in prd_devices]

            if not serve_config.get("users") or not isinstance(serve_config["users"], dict):
                serve_config["users"] = {}

            if not no_key and api_secret not in serve_config["users"]:
                serve_config["users"][api_secret] = {
                    "devices": wvd_device_names,
                    "playready_devices": prd_device_names,
                    "username": "api_user",
                }

            for user_key, user_config in serve_config["users"].items():
                if "playready_devices" not in user_config:
                    # Require explicit PlayReady device access per user (default: no access).
                    user_config["playready_devices"] = []
                    log.warning(
                        f'User "{user_key}" has no "playready_devices" configured; PlayReady access disabled for this user. '
                        f"Available PlayReady devices: {prd_device_names}"
                    )

            def create_serve_authentication(serve_playready_flag: bool):
                @web.middleware
                async def serve_authentication(request: web.Request, handler) -> web.Response:
                    if serve_playready_flag and request.path in ("/playready", "/playready/"):
                        response = await handler(request)
                    else:
                        response = await pywidevine_serve.authentication(request, handler)

                    if serve_playready_flag and request.path.startswith("/playready"):
                        from pyplayready import __version__ as pyplayready_version
                        response.headers["Server"] = f"https://git.gay/ready-dl/pyplayready serve v{pyplayready_version}"

                    return response
                return serve_authentication

            if no_key:
                app = web.Application(middlewares=[cors_middleware])
            else:
                serve_auth = create_serve_authentication(serve_playready and bool(prd_devices))
                app = web.Application(middlewares=[cors_middleware, serve_auth])

            app["config"] = serve_config
            app["debug_api"] = debug_api

            if serve_widevine:
                app.on_startup.append(pywidevine_serve._startup)
                app.on_cleanup.append(pywidevine_serve._cleanup)
                app.add_routes(pywidevine_serve.routes)

            if serve_playready and prd_devices:
                if no_key:
                    playready_app = web.Application()
                else:
                    playready_app = web.Application(middlewares=[pyplayready_serve.authentication])

                # PlayReady subapp config maps playready_devices to "devices" for pyplayready compatibility
                playready_config = {
                    "devices": prd_devices,
                    "users": {
                        user_key: {
                            "devices": user_cfg.get("playready_devices", []),
                            "username": user_cfg.get("username", "user"),
                        }
                        for user_key, user_cfg in serve_config["users"].items()
                    }
                    if not no_key
                    else {},
                }
                playready_app["config"] = playready_config
                playready_app.on_startup.append(pyplayready_serve._startup)
                playready_app.on_cleanup.append(pyplayready_serve._cleanup)
                playready_app.add_routes(pyplayready_serve.routes)

                async def playready_ping(_: web.Request) -> web.Response:
                    from pyplayready import __version__ as pyplayready_version
                    response = web.json_response({"message": "OK"})
                    response.headers["Server"] = f"https://git.gay/ready-dl/pyplayready serve v{pyplayready_version}"
                    return response

                app.router.add_route("*", "/playready", playready_ping)

                app.add_subapp("/playready", playready_app)
                log.info(f"PlayReady CDM endpoints available at http://{host}:{port}/playready/")
            elif serve_playready:
                log.info("No PlayReady devices found, skipping PlayReady CDM endpoints")

            setup_routes(app)
            setup_swagger(app)

            if serve_widevine:
                log.info(f"Widevine CDM endpoints available at http://{host}:{port}/{{device}}/open")
            log.info(f"REST API endpoints available at http://{host}:{port}/api/")
            log.info(f"Swagger UI available at http://{host}:{port}/api/docs/")
            log.info("(Press CTRL+C to quit)")
            web.run_app(app, host=host, port=port, print=None)
    finally:
        if caddy_p:
            caddy_p.kill()
