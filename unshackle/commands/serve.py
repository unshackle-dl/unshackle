import logging
import subprocess

import click
from aiohttp import web

from unshackle.core import binaries
from unshackle.core.api import setup_routes, setup_swagger
from unshackle.core.config import config
from unshackle.core.constants import context_settings


@click.command(
    short_help="Serve your Local Widevine Devices and REST API for Remote Access.", context_settings=context_settings
)
@click.option("-h", "--host", type=str, default="0.0.0.0", help="Host to serve from.")
@click.option("-p", "--port", type=int, default=8786, help="Port to serve from.")
@click.option("--caddy", is_flag=True, default=False, help="Also serve with Caddy.")
@click.option("--api-only", is_flag=True, default=False, help="Serve only the REST API, not pywidevine CDM.")
@click.option("--no-key", is_flag=True, default=False, help="Disable API key authentication (allows all requests).")
def serve(host: str, port: int, caddy: bool, api_only: bool, no_key: bool) -> None:
    """
    Serve your Local Widevine Devices and REST API for Remote Access.

    \b
    Host as 127.0.0.1 may block remote access even if port-forwarded.
    Instead, use 0.0.0.0 and ensure the TCP port you choose is forwarded.

    \b
    You may serve with Caddy at the same time with --caddy. You can use Caddy
    as a reverse-proxy to serve with HTTPS. The config used will be the Caddyfile
    next to the unshackle config.

    \b
    The REST API provides programmatic access to unshackle functionality.
    Configure authentication in your config under serve.users and serve.api_secret.
    """
    from pywidevine import serve as pywidevine_serve

    log = logging.getLogger("serve")

    # Validate API secret for REST API routes (unless --no-key is used)
    if not no_key:
        api_secret = config.serve.get("api_secret")
        if not api_secret:
            raise click.ClickException(
                "API secret key is not configured. Please add 'api_secret' to the 'serve' section in your config."
            )
    else:
        api_secret = None
        log.warning("Running with --no-key: Authentication is DISABLED for all API endpoints!")

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

        if api_only:
            # API-only mode: serve just the REST API
            log.info("Starting REST API server (pywidevine CDM disabled)")
            if no_key:
                app = web.Application()
                app["config"] = {"users": []}
            else:
                app = web.Application(middlewares=[pywidevine_serve.authentication])
                app["config"] = {"users": [api_secret]}
            setup_routes(app)
            setup_swagger(app)
            log.info(f"REST API endpoints available at http://{host}:{port}/api/")
            log.info(f"Swagger UI available at http://{host}:{port}/api/docs/")
            log.info("(Press CTRL+C to quit)")
            web.run_app(app, host=host, port=port, print=None)
        else:
            # Integrated mode: serve both pywidevine + REST API
            log.info("Starting integrated server (pywidevine CDM + REST API)")

            # Create integrated app with both pywidevine and API routes
            if no_key:
                app = web.Application()
                app["config"] = dict(config.serve)
                app["config"]["users"] = []
            else:
                app = web.Application(middlewares=[pywidevine_serve.authentication])
                # Setup config - add API secret to users for authentication
                serve_config = dict(config.serve)
                if not serve_config.get("users"):
                    serve_config["users"] = []
                if api_secret not in serve_config["users"]:
                    serve_config["users"].append(api_secret)
                app["config"] = serve_config

            app.on_startup.append(pywidevine_serve._startup)
            app.on_cleanup.append(pywidevine_serve._cleanup)
            app.add_routes(pywidevine_serve.routes)
            setup_routes(app)
            setup_swagger(app)

            log.info(f"REST API endpoints available at http://{host}:{port}/api/")
            log.info(f"Swagger UI available at http://{host}:{port}/api/docs/")
            log.info("(Press CTRL+C to quit)")
            web.run_app(app, host=host, port=port, print=None)
    finally:
        if caddy_p:
            caddy_p.kill()
