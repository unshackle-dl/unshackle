import asyncio
import hmac
import logging
import re
import signal
import subprocess
import sys
from contextlib import suppress
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Optional

import click
from aiohttp import web

import unshackle.core.config as config_module
from unshackle.core import binaries
from unshackle.core.api import cors_middleware, setup_routes, setup_swagger
from unshackle.core.api.compression import compression_middleware
from unshackle.core.api.events import publish_refresh_events, publish_service_event
from unshackle.core.api.handlers import (
    DASHBOARD_PREFIX,
    api_key_authentication,
    dashboard_authentication,
    dashboard_key,
    rate_limit_response,
    request_secret_key,
    server_account_regions,
    server_cdm_max_height_error,
    validate_server_accounts,
)
from unshackle.core.api.stats import (
    cdm_log_middleware,
    key_id,
    key_rate_limit,
    rate_limit_error,
    ring,
    stats,
    stats_middleware,
)
from unshackle.core.config import Config, config
from unshackle.core.console import console
from unshackle.core.constants import context_settings
from unshackle.core.downloaders import format_speed, parse_speed_limit, set_speed_limit


class _MaskAccessKey(logging.Filter):
    """Mask the ``secret_key`` query parameter the SSE routes accept before the access line is stored."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "secret_key=" in record.msg:
            record.msg = re.sub(r"secret_key=[^&\s]*", "secret_key=***", record.msg)
        return True


def _install_session_store(app: web.Application) -> None:
    """Start the remote session store's idle sweep with the app and stop it, bridges included, on cleanup."""
    from unshackle.core.api.session_store import get_session_store

    session_store = get_session_store()

    async def start(_app: web.Application) -> None:
        await session_store.start_cleanup_loop()

    async def stop(_app: web.Application) -> None:
        await session_store.cancel_all_bridges()
        await session_store.stop_cleanup_loop()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)


def _install_service_refresh(app: web.Application) -> None:
    """Report service load issues, then periodically pull the service repos and hot-reload changed services."""
    from unshackle.core import services

    log = logging.getLogger("serve")
    services.log_load_issues()
    services.record_loaded_commits()
    interval = int(config.serve.get("services_refresh_interval", 0) or 0)
    if interval <= 0 or not services.repo_specs():
        return

    async def loop() -> None:
        from unshackle.core.api.download_manager import busy_services, schedule_pending_reload

        while True:
            await asyncio.sleep(interval)
            try:
                applied = await asyncio.to_thread(services.apply_pending, busy_services())
                if applied:
                    log.info(f"Services reloaded: {', '.join(applied)}")
                    publish_service_event("applied", applied)
                repos = await asyncio.to_thread(services.refresh_and_reload, busy_services())
                for r in repos:
                    if r["changes"]:
                        log.info(f"Services refreshed {r['spec']}: {', '.join(r['changes'])}")
                    if r["deferred"]:
                        log.info(f"Services staged until their jobs and sessions finish: {', '.join(r['deferred'])}")
                    for err in r["load_errors"]:
                        log.error(f"Service reload failed: {err}")
                publish_refresh_events(repos)
                # The busy set was read before the pull. A tag whose last session ended during the pull
                # is staged although idle, and its session-end trigger ran before it was staged.
                schedule_pending_reload()
            except Exception:
                log.exception("Service refresh failed")

    async def start(_app: web.Application) -> None:
        _app["service_refresh_task"] = asyncio.create_task(loop())
        log.info(f"Service repos refresh every {interval}s")

    async def stop(_app: web.Application) -> None:
        task = _app.get("service_refresh_task")
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app.on_startup.append(start)
    app.on_cleanup.append(stop)


RESTART_KEYS = ("remote_only", "services_refresh_interval")
"""``serve.*`` keys a reload cannot apply: they decide routes or tasks fixed at startup."""

log = logging.getLogger("serve")


def _prepare_serve(serve: dict, no_key: bool) -> Optional[str]:
    """Glob the device directories into ``serve``, validate it in place and return the api_secret.

    Raises ValueError on a bad value. The api_secret is None under ``--no-key``.
    """
    api_secret = None if no_key else serve.get("api_secret")
    if not no_key and not api_secret:
        raise ValueError(
            "API secret key is not configured. Please add 'api_secret' to the 'serve' section in your config."
        )

    for key, directory, pattern in (
        ("devices", config.directories.wvds, "*.wvd"),
        ("playready_devices", config.directories.prds, "*.prd"),
    ):
        if not serve.get(key):
            serve[key] = []
        serve[key].extend(directory.glob(pattern))
        for device in serve[key]:
            if not Path(device).is_file():
                raise ValueError(f"serve.{key}: device file does not exist: {device}")

    try:
        parse_speed_limit(serve.get("global_speed_limit"))
    except ValueError as e:
        raise ValueError(f"serve.global_speed_limit: {e}")

    users = serve.get("users", {})
    if isinstance(users, dict):
        users = {str(k): v for k, v in users.items()}
        serve["users"] = users
    tiers = serve.get("tiers") or {}
    if not isinstance(tiers, dict):
        raise ValueError("serve.tiers must be a mapping of tier name to settings")
    for tier_name, tier_cfg in tiers.items():
        if not isinstance(tier_cfg, dict):
            raise ValueError(f"serve.tiers.{tier_name} must be a mapping of setting to value")
        if problem := rate_limit_error(f"serve.tiers.{tier_name}", tier_cfg.get("rate_limit")):
            raise ValueError(problem)
    for user_key, user_cfg in users.items() if isinstance(users, dict) else []:
        user_services = user_cfg.get("services") if isinstance(user_cfg, dict) else None
        username = user_cfg.get("username", user_key[:8] + "...") if isinstance(user_cfg, dict) else user_key[:8]
        if user_services:
            log.info(f"User '{username}' restricted to services: {', '.join(user_services)}")
        tier = user_cfg.get("tier") if isinstance(user_cfg, dict) else None
        if tier and tier not in tiers:
            raise ValueError(f"serve.users.{username}.tier: no such tier '{tier}' under serve.tiers")
        if isinstance(user_cfg, dict):
            if problem := rate_limit_error(f"serve.users.{username}", user_cfg.get("rate_limit")):
                raise ValueError(problem)
            if problem := server_cdm_max_height_error(f"serve.users.{username}", user_cfg.get("server_cdm_max_height")):
                raise ValueError(problem)
        limit = key_rate_limit(user_key, serve)
        if limit:
            log.info(f"User '{username}' rate limited to {limit} requests/hour")
    return api_secret


def _build_cdm_config(
    serve: dict, api_secret: Optional[str], serve_widevine: bool, serve_playready: bool
) -> tuple[dict, dict]:
    """The ``app["config"]`` for the main app and the PlayReady sub-app, derived from a prepared ``serve``.

    The main config carries the api_secret user and a ``playready_devices`` list per user; the
    sub-app config maps each user's ``playready_devices`` to ``devices`` for pyplayready.
    """
    serve_config = dict(serve)
    wvd_devices = serve_config.get("devices", []) if serve_widevine else []
    prd_devices = serve_config.get("playready_devices", []) if serve_playready else []
    wvd_device_names = [Path(d).stem for d in wvd_devices]
    prd_device_names = [Path(d).stem for d in prd_devices]

    users_cfg = serve_config.get("users")
    serve_config["users"] = {k: dict(v) for k, v in users_cfg.items()} if isinstance(users_cfg, dict) else {}
    if api_secret and api_secret not in serve_config["users"]:
        serve_config["users"][api_secret] = {
            "devices": wvd_device_names,
            "playready_devices": prd_device_names,
            "username": "api_user",
        }
    for user_key, user_config in serve_config["users"].items():
        if "playready_devices" not in user_config:
            user_config["playready_devices"] = []
            if serve_playready:
                log.warning(
                    f'User "{key_id(user_key)}" has no "playready_devices" configured; PlayReady access disabled '
                    f"for this user. Available PlayReady devices: {prd_device_names}"
                )
    serve_config["devices"] = wvd_devices
    serve_config["playready_devices"] = prd_devices

    playready_config = {
        "devices": prd_devices,
        "users": {
            user_key: {
                "devices": user_cfg.get("playready_devices", []),
                "username": user_cfg.get("username", "user"),
            }
            for user_key, user_cfg in serve_config["users"].items()
        }
        if api_secret
        else {},
    }
    return serve_config, playready_config


def _device_map(devices: list[Any]) -> dict[str, Path]:
    """The ``{stem: path}`` form pywidevine and pyplayready rewrite ``app["config"]["devices"]`` into at startup."""
    return {Path(d).stem: Path(d) for d in devices}


def _device_stems(serve: dict) -> set[str]:
    return {Path(d).stem for d in serve.get("devices", []) + serve.get("playready_devices", [])}


def reload_config(app: web.Application, *, no_key: bool, serve_widevine: bool, serve_playready: bool) -> bool:
    """Re-read the config file and swap it in; return True when the new file is in use.

    The swap replaces every ``Config`` section, so services read new credentials and CDM mappings
    too. Runs synchronously on the event loop so no request sees a half-updated config. A file
    that fails to parse or validate, or that changes a startup-only setting, keeps the running
    config. The reload leaves open CDM sessions alone: a revoked key fails authentication on
    its next call.
    """
    try:
        if not config_module.config_path:
            raise ValueError("no config file found")
        fresh = Config.from_yaml(config_module.config_path)
        serve = fresh.serve
        api_secret = _prepare_serve(serve, no_key)
    except Exception as e:
        log.error(f"Config reload rejected, keeping the running config: {e}")
        return False

    changed = [k for k in RESTART_KEYS if serve.get(k) != config.serve.get(k)]
    if serve_playready and bool(serve.get("playready_devices")) != ("playready_app" in app):
        changed.append("playready_devices (first device added or last removed)")
    if changed:
        log.warning(f"Config reload skipped, restart to apply: {', '.join(changed)}")
        return False

    serve_config, playready_config = _build_cdm_config(serve, api_secret, serve_widevine, serve_playready)
    old_users, new_users = set(app["config"]["users"]), set(serve_config["users"])
    old_devices, new_devices = _device_stems(config.serve), _device_stems(serve)
    vars(config).update(vars(fresh))
    app["config"]["users"] = serve_config["users"]
    if serve_widevine:
        app["config"]["devices"] = _device_map(serve_config["devices"])
    if playready_app := app.get("playready_app"):
        playready_app["config"]["users"] = playready_config["users"]
        playready_app["config"]["devices"] = _device_map(playready_config["devices"])
    set_speed_limit(parse_speed_limit(serve.get("global_speed_limit")), lock=True)
    log.info(
        "Config reloaded: "
        f"keys added {sorted(key_id(k) for k in new_users - old_users)}, "
        f"removed {sorted(key_id(k) for k in old_users - new_users)}; "
        f"devices added {sorted(new_devices - old_devices)}, removed {sorted(old_devices - new_devices)}"
    )
    return True


def _install_config_reload(app: web.Application, *, no_key: bool, serve_widevine: bool, serve_playready: bool) -> None:
    """Reload the config on SIGHUP, so ``systemctl reload`` applies a config edit without a restart."""

    async def start(_app: web.Application) -> None:
        reload = partial(
            reload_config, _app, no_key=no_key, serve_widevine=serve_widevine, serve_playready=serve_playready
        )
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, reload)
        except (AttributeError, NotImplementedError):
            return
        log.info("Config hot-reload: send SIGHUP (systemctl reload) after editing unshackle.yaml")

    app.on_startup.append(start)


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
@click.option(
    "--cdm-only",
    is_flag=True,
    default=False,
    help="Serve only the pywidevine/pyplayready CDM endpoints (plus health and the dashboard), not the REST API.",
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
@click.option(
    "--remote-only",
    is_flag=True,
    default=False,
    help="Only expose remote service session endpoints (health, services, search, session).",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    default=False,
    help="Headless mode: no banner and no Rich output, plain log lines on stderr.",
)
def serve(
    host: str,
    port: int,
    caddy: bool,
    api_only: bool,
    cdm_only: bool,
    no_widevine: bool,
    no_playready: bool,
    no_key: bool,
    debug_api: bool,
    debug: bool,
    remote_only: bool,
    quiet: bool,
) -> None:
    """
    Serve your Local Widevine and PlayReady Devices and REST API for Remote Access.

    \b
    CDM ENDPOINTS:
    - Widevine: /{device}/open, /{device}/close/{session_id}, and others
    - PlayReady: /playready/{device}/open, /playready/{device}/close/{session_id}, and others

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

    if quiet:
        logging.basicConfig(
            force=True,
            level=logging.DEBUG if debug else logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            stream=sys.stderr,
        )
        console.quiet = True
        if not debug:
            for handler in logging.getLogger().handlers:
                handler.addFilter(lambda record: record.name != "aiohttp.access")
        if debug:
            log_path = config.directories.logs / config.filenames.log.format(
                name="serve", time=datetime.now().strftime("%Y%m%d-%H%M%S")
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(file_handler)
            log.info(f"Writing log to {log_path}")
    elif debug:
        logging.getLogger().setLevel(logging.DEBUG)
    if debug:
        log.info("Debug logging enabled for API operations")
    elif not quiet:
        logging.getLogger("api").setLevel(logging.WARNING)
        logging.getLogger("api.remote").setLevel(logging.WARNING)
    ring.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(ring)
    logging.getLogger("aiohttp.access").addFilter(_MaskAccessKey())

    if no_key:
        log.warning("Running with --no-key: Authentication is DISABLED for all API endpoints!")

    if debug_api:
        log.warning("Running with --debug-api: Error responses will include technical debug information!")

    if api_only and (no_widevine or no_playready):
        raise click.ClickException("Cannot use --api-only with --no-widevine or --no-playready.")
    if cdm_only and (api_only or remote_only):
        raise click.ClickException("Cannot use --cdm-only with --api-only or --remote-only.")

    if caddy:
        if not binaries.Caddy:
            raise click.ClickException('Caddy executable "caddy" not found but is required for --caddy.')
        caddy_p = subprocess.Popen(
            [binaries.Caddy, "run", "--config", str(config.directories.user_configs / "Caddyfile")]
        )
    else:
        caddy_p = None

    try:
        try:
            api_secret = _prepare_serve(config.serve, no_key)
        except ValueError as e:
            raise click.ClickException(str(e))

        remote_only = remote_only or config.serve.get("remote_only", False)
        if remote_only:
            api_only = True
        stats.host, stats.port = host, port
        stats.mode = "remote_only" if remote_only else "api_only" if api_only else "cdm_only" if cdm_only else "full"
        dashboard = bool(dashboard_key())
        if dashboard:
            log.info(f"Developer dashboard endpoints available at http://{host}:{port}/api/dashboard/")
        elif quiet:
            log.info("Developer dashboard disabled: set serve.dashboard.key in unshackle.yaml to enable it")

        if not cdm_only:
            global_speed_limit = parse_speed_limit(config.serve.get("global_speed_limit"))
            if global_speed_limit:
                set_speed_limit(global_speed_limit, lock=True)
                log.info(f"Global speed limit: {format_speed(global_speed_limit)} (shared by all jobs)")

            global_services = config.serve.get("services")
            if global_services:
                log.info(f"Global service allowlist: {', '.join(global_services)}")
            try:
                server_accounts = validate_server_accounts()
            except ValueError as e:
                raise click.ClickException(str(e))
            for tag in server_accounts:
                regions = server_account_regions(tag) or {}
                covered = list(regions.get("regions") or []) + (["global"] if regions.get("global") else [])
                log.info(f"Server accounts for {tag}: {', '.join(covered)}")
            if config.key_vaults and not any(v.get("type") == "SQLite" for v in config.key_vaults):
                log.warning(
                    "No SQLite key vault configured: a content key a remote client proves wrong cannot be flagged"
                )

        serve_widevine = not api_only and not no_widevine
        serve_playready = not api_only and not no_playready
        serve_config, playready_config = _build_cdm_config(config.serve, api_secret, serve_widevine, serve_playready)
        prd_devices = playready_config["devices"]

        if api_only:
            log.info("Starting REST API server (pywidevine/pyplayready CDM disabled)")
            if no_key:
                app = web.Application(
                    middlewares=[cors_middleware, stats_middleware, dashboard_authentication, compression_middleware]
                )
            else:
                app = web.Application(
                    middlewares=[
                        cors_middleware,
                        stats_middleware,
                        dashboard_authentication,
                        api_key_authentication,
                        compression_middleware,
                    ]
                )
            app["config"] = serve_config
            app["debug_api"] = debug_api

            _install_session_store(app)
            _install_service_refresh(app)
            _install_config_reload(app, no_key=no_key, serve_widevine=serve_widevine, serve_playready=serve_playready)

            setup_routes(app, remote_only=remote_only, dashboard=dashboard)
            if not remote_only:
                setup_swagger(app, dashboard=dashboard)
                log.info(f"REST API endpoints available at http://{host}:{port}/api/")
                log.info(f"Swagger UI available at http://{host}:{port}/api/docs/")
            else:
                log.info(f"Remote service endpoints available at http://{host}:{port}/api/session/")
            log.info("(Press CTRL+C to quit)")
            web.run_app(app, host=host, port=port, print=None)
        else:
            cdm_parts = []
            if serve_widevine:
                cdm_parts.append("pywidevine CDM")
            if serve_playready:
                cdm_parts.append("pyplayready CDM")
            log.info(f"Starting integrated server ({' + '.join(cdm_parts)} + REST API)")

            def create_serve_authentication(serve_playready_flag: bool):
                @web.middleware
                async def serve_authentication(request: web.Request, handler) -> web.StreamResponse:
                    secret_key = request_secret_key(request)
                    if request.path.startswith(DASHBOARD_PREFIX):
                        return await handler(request)
                    if (
                        request.path != "/api/health"
                        and secret_key
                        and any(hmac.compare_digest(secret_key, k) for k in request.app["config"]["users"])
                    ):
                        limited = rate_limit_response(secret_key)
                        if limited is not None:
                            return limited
                    if serve_playready_flag and request.path in ("/playready", "/playready/"):
                        response = await handler(request)
                    elif secret_key and not request.headers.get("X-Secret-Key"):
                        if not any(hmac.compare_digest(secret_key, k) for k in request.app["config"]["users"]):
                            return web.json_response({"status": 401, "message": "Secret Key is Invalid."}, status=401)
                        response = await handler(request)
                    else:
                        response = await pywidevine_serve.authentication(request, handler)

                    if serve_playready_flag and request.path.startswith("/playready"):
                        from pyplayready import __version__ as pyplayready_version

                        response.headers["Server"] = (
                            f"https://git.gay/ready-dl/pyplayready serve v{pyplayready_version}"
                        )

                    return response

                return serve_authentication

            if no_key:
                app = web.Application(
                    middlewares=[
                        cors_middleware,
                        stats_middleware,
                        cdm_log_middleware,
                        dashboard_authentication,
                        compression_middleware,
                    ]
                )
            else:
                serve_auth = create_serve_authentication(serve_playready and bool(prd_devices))
                app = web.Application(
                    middlewares=[
                        cors_middleware,
                        stats_middleware,
                        cdm_log_middleware,
                        dashboard_authentication,
                        serve_auth,
                        compression_middleware,
                    ]
                )

            app["config"] = serve_config
            app["debug_api"] = debug_api

            if not cdm_only:
                _install_session_store(app)
                _install_service_refresh(app)
            _install_config_reload(app, no_key=no_key, serve_widevine=serve_widevine, serve_playready=serve_playready)

            if serve_widevine:
                app.on_startup.append(pywidevine_serve._startup)
                app.on_cleanup.append(pywidevine_serve._cleanup)
                app.add_routes(pywidevine_serve.routes)

            if serve_playready and prd_devices:
                if no_key:
                    playready_app = web.Application()
                else:
                    playready_app = web.Application(middlewares=[pyplayready_serve.authentication])
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
                app["playready_app"] = playready_app
                log.info(f"PlayReady CDM endpoints available at http://{host}:{port}/playready/")
            elif serve_playready:
                log.info("No PlayReady devices found, skipping PlayReady CDM endpoints")

            setup_routes(app, remote_only=remote_only, dashboard=dashboard, cdm_only=cdm_only)

            if serve_widevine:
                log.info(f"Widevine CDM endpoints available at http://{host}:{port}/{{device}}/open")
            if cdm_only:
                log.info("REST API disabled (--cdm-only): only /api/health and the dashboard answer under /api/")
            elif remote_only:
                log.info(f"Remote service endpoints available at http://{host}:{port}/api/session/")
            else:
                setup_swagger(app, dashboard=dashboard)
                log.info(f"REST API endpoints available at http://{host}:{port}/api/")
                log.info(f"Swagger UI available at http://{host}:{port}/api/docs/")
            log.info("(Press CTRL+C to quit)")
            web.run_app(app, host=host, port=port, print=None)
    finally:
        if caddy_p:
            caddy_p.kill()
