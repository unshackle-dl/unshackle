"""Client-side authentication for remote services.

This module handles authenticating services locally on the client side,
then sending the authenticated session to the remote server.

This approach allows:
- Interactive browser-based logins
- 2FA/CAPTCHA handling
- OAuth flows
- Any authentication that requires user interaction

The server NEVER sees credentials - only authenticated sessions.
"""

import logging
from typing import Any, Dict, Optional

import click
import requests
import yaml

from unshackle.core.api.session_serializer import serialize_session
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.credential import Credential
from unshackle.core.local_session_cache import get_local_session_cache
from unshackle.core.services import Services
from unshackle.core.utils.click_types import ContextData
from unshackle.core.utils.collections import merge_dict

log = logging.getLogger("RemoteAuth")


class RemoteAuthenticator:
    """
    Handles client-side authentication for remote services.

    Workflow:
    1. Load service locally
    2. Authenticate using local credentials/cookies (can show browser, handle 2FA)
    3. Extract authenticated session
    4. Upload session to remote server
    5. Server uses the pre-authenticated session
    """

    def __init__(self, remote_url: str, api_key: str):
        """
        Initialize remote authenticator.

        Args:
            remote_url: Base URL of remote server
            api_key: API key for remote server
        """
        self.remote_url = remote_url.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": self.api_key, "Content-Type": "application/json"})

    def authenticate_service_locally(
        self, service_tag: str, profile: Optional[str] = None, force_reauth: bool = False
    ) -> Dict[str, Any]:
        """
        Authenticate a service locally and extract the session.

        This runs the service authentication on the CLIENT side where browsers,
        2FA, and interactive prompts can work.

        Args:
            service_tag: Service to authenticate (e.g., "DSNP", "NF")
            profile: Optional profile to use for credentials
            force_reauth: Force re-authentication even if session exists

        Returns:
            Serialized session data

        Raises:
            ValueError: If service not found or authentication fails
        """
        console.print(f"[cyan]Authenticating {service_tag} locally...[/cyan]")

        # Validate service exists
        if service_tag not in Services.get_tags():
            raise ValueError(f"Service {service_tag} not found locally")

        # Load service
        service_module = Services.load(service_tag)

        # Load service config
        service_config_path = Services.get_path(service_tag) / config.filenames.config
        if service_config_path.exists():
            service_config = yaml.safe_load(service_config_path.read_text(encoding="utf8"))
        else:
            service_config = {}
        merge_dict(config.services.get(service_tag), service_config)

        # Create Click context
        @click.command()
        @click.pass_context
        def dummy_command(ctx: click.Context) -> None:
            pass

        ctx = click.Context(dummy_command)
        ctx.obj = ContextData(config=service_config, cdm=None, proxy_providers=[], profile=profile)

        # Create service instance
        try:
            # Get service initialization parameters
            import inspect

            service_init_params = inspect.signature(service_module.__init__).parameters
            service_kwargs = {}

            # Extract defaults from click command
            if hasattr(service_module, "cli") and hasattr(service_module.cli, "params"):
                for param in service_module.cli.params:
                    if hasattr(param, "name") and param.name not in service_kwargs:
                        if hasattr(param, "default") and param.default is not None:
                            service_kwargs[param.name] = param.default

            # Filter to only valid parameters
            filtered_kwargs = {k: v for k, v in service_kwargs.items() if k in service_init_params}

            # Create service instance
            service_instance = service_module(ctx, **filtered_kwargs)

            # Get credentials and cookies
            cookies = self._get_cookie_jar(service_tag, profile)
            credential = self._get_credentials(service_tag, profile)

            # Authenticate the service
            console.print("[yellow]Authenticating... (this may show browser or prompts)[/yellow]")
            service_instance.authenticate(cookies=cookies, credential=credential)

            # Serialize the authenticated session
            session_data = serialize_session(service_instance.session)

            # Add metadata
            session_data["service_tag"] = service_tag
            session_data["profile"] = profile
            session_data["authenticated"] = True

            console.print(f"[green]✓ {service_tag} authenticated successfully![/green]")
            log.info(f"Authenticated {service_tag} (profile: {profile or 'default'})")

            return session_data

        except Exception as e:
            console.print(f"[red]✗ Authentication failed: {e}[/red]")
            log.error(f"Failed to authenticate {service_tag}: {e}")
            raise ValueError(f"Authentication failed for {service_tag}: {e}")

    def save_session_locally(self, session_data: Dict[str, Any]) -> bool:
        """
        Save authenticated session to local cache.

        The session is stored only on the client machine, never on the server.
        The server is completely stateless.

        Args:
            session_data: Serialized session data

        Returns:
            True if save successful
        """
        service_tag = session_data.get("service_tag")
        profile = session_data.get("profile", "default")

        console.print("[cyan]Saving session to local cache...[/cyan]")

        try:
            # Get local session cache
            cache = get_local_session_cache()

            # Store session locally
            cache.store_session(
                remote_url=self.remote_url,
                service_tag=service_tag,
                profile=profile,
                session_data=session_data
            )

            console.print("[green]✓ Session saved locally![/green]")
            log.info(f"Saved session for {service_tag} (profile: {profile}) to local cache")
            return True

        except Exception as e:
            console.print(f"[red]✗ Save failed: {e}[/red]")
            log.error(f"Failed to save session locally: {e}")
            return False

    def authenticate_and_save(self, service_tag: str, profile: Optional[str] = None) -> bool:
        """
        Authenticate locally and save session to local cache in one step.

        Args:
            service_tag: Service to authenticate
            profile: Optional profile

        Returns:
            True if successful
        """
        try:
            # Authenticate locally
            session_data = self.authenticate_service_locally(service_tag, profile)

            # Save to local cache
            return self.save_session_locally(session_data)

        except Exception as e:
            console.print(f"[red]Authentication and save failed: {e}[/red]")
            return False

    def check_local_session_status(self, service_tag: str, profile: Optional[str] = None) -> Dict[str, Any]:
        """
        Check if a session exists in local cache.

        Args:
            service_tag: Service tag
            profile: Optional profile

        Returns:
            Session status info
        """
        try:
            cache = get_local_session_cache()
            session_data = cache.get_session(self.remote_url, service_tag, profile or "default")

            if session_data:
                # Get metadata
                sessions = cache.list_sessions(self.remote_url)
                for session in sessions:
                    if session["service_tag"] == service_tag and session["profile"] == (profile or "default"):
                        return {
                            "status": "success",
                            "exists": True,
                            "session_info": session
                        }

            return {
                "status": "success",
                "exists": False,
                "message": f"No session found for {service_tag} (profile: {profile or 'default'})"
            }

        except Exception as e:
            log.error(f"Failed to check session status: {e}")
            return {"status": "error", "message": "Failed to check session status"}

    def _get_cookie_jar(self, service_tag: str, profile: Optional[str]):
        """Get cookie jar for service and profile."""
        from unshackle.commands.dl import dl

        return dl.get_cookie_jar(service_tag, profile)

    def _get_credentials(self, service_tag: str, profile: Optional[str]) -> Optional[Credential]:
        """Get credentials for service and profile."""
        from unshackle.commands.dl import dl

        return dl.get_credentials(service_tag, profile)


def authenticate_remote_service(remote_url: str, api_key: str, service_tag: str, profile: Optional[str] = None) -> bool:
    """
    Helper function to authenticate a remote service.

    Args:
        remote_url: Remote server URL
        api_key: API key
        service_tag: Service to authenticate
        profile: Optional profile

    Returns:
        True if successful
    """
    authenticator = RemoteAuthenticator(remote_url, api_key)
    return authenticator.authenticate_and_save(service_tag, profile)


__all__ = ["RemoteAuthenticator", "authenticate_remote_service"]
