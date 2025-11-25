"""Remote service discovery and management."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from unshackle.core.config import config
from unshackle.core.remote_service import RemoteService

log = logging.getLogger("RemoteServices")


class RemoteServiceManager:
    """
    Manages discovery and registration of remote services.

    This class connects to configured remote unshackle servers,
    discovers available services, and creates RemoteService instances
    that can be used like local services.
    """

    def __init__(self):
        """Initialize the remote service manager."""
        self.remote_services: Dict[str, type] = {}
        self.remote_configs: List[Dict[str, Any]] = []

    def discover_services(self) -> None:
        """
        Discover services from all configured remote servers.

        Reads the remote_services configuration, connects to each server,
        retrieves available services, and creates RemoteService classes
        for each discovered service.
        """
        if not config.remote_services:
            log.debug("No remote services configured")
            return

        log.info(f"Discovering services from {len(config.remote_services)} remote server(s)...")

        for remote_config in config.remote_services:
            try:
                self._discover_from_server(remote_config)
            except Exception as e:
                log.error(f"Failed to discover services from {remote_config.get('url')}: {e}")
                continue

        log.info(f"Discovered {len(self.remote_services)} remote service(s)")

    def _discover_from_server(self, remote_config: Dict[str, Any]) -> None:
        """
        Discover services from a single remote server.

        Args:
            remote_config: Configuration for the remote server
                          (must contain 'url' and 'api_key')
        """
        url = remote_config.get("url", "").rstrip("/")
        api_key = remote_config.get("api_key", "")
        server_name = remote_config.get("name", url)

        if not url:
            log.warning("Remote service configuration missing 'url', skipping")
            return

        if not api_key:
            log.warning(f"Remote service {url} missing 'api_key', skipping")
            return

        log.info(f"Connecting to remote server: {server_name}")

        try:
            # Query the remote server for available services
            response = requests.get(
                f"{url}/api/remote/services",
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                timeout=10,
            )

            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success" or "services" not in data:
                log.error(f"Invalid response from {url}: {data}")
                return

            services = data["services"]
            log.info(f"Found {len(services)} service(s) on {server_name}")

            # Create RemoteService classes for each service
            for service_info in services:
                self._register_remote_service(url, api_key, service_info, server_name)

        except requests.RequestException as e:
            log.error(f"Failed to connect to remote server {url}: {e}")
            raise

    def _register_remote_service(
        self, remote_url: str, api_key: str, service_info: Dict[str, Any], server_name: str
    ) -> None:
        """
        Register a remote service as a local service class.

        Args:
            remote_url: Base URL of the remote server
            api_key: API key for authentication
            service_info: Service metadata from the remote server
            server_name: Friendly name of the remote server
        """
        service_tag = service_info.get("tag")
        if not service_tag:
            log.warning(f"Service info missing 'tag': {service_info}")
            return

        # Create a unique tag for the remote service
        # Use "remote_" prefix to distinguish from local services
        remote_tag = f"remote_{service_tag}"

        # Check if this remote service is already registered
        if remote_tag in self.remote_services:
            log.debug(f"Remote service {remote_tag} already registered, skipping")
            return

        log.info(f"Registering remote service: {remote_tag} from {server_name}")

        # Create a dynamic class that inherits from RemoteService
        # This allows us to create instances with the cli() method for Click integration
        class DynamicRemoteService(RemoteService):
            """Dynamically created remote service class."""

            def __init__(self, ctx, **kwargs):
                super().__init__(
                    ctx=ctx,
                    remote_url=remote_url,
                    api_key=api_key,
                    service_tag=service_tag,
                    service_metadata=service_info,
                    **kwargs,
                )

            @staticmethod
            def cli():
                """CLI method for Click integration."""
                import click

                # Create a dynamic Click command for this service
                @click.command(
                    name=remote_tag,
                    short_help=f"Remote: {service_info.get('help', service_tag)}",
                    help=service_info.get("help", f"Remote service for {service_tag}"),
                )
                @click.argument("title", type=str, required=False)
                @click.option("-q", "--query", type=str, help="Search query")
                @click.pass_context
                def remote_service_cli(ctx, title=None, query=None, **kwargs):
                    # Combine title and kwargs
                    params = {**kwargs}
                    if title:
                        params["title"] = title
                    if query:
                        params["query"] = query

                    return DynamicRemoteService(ctx, **params)

                return remote_service_cli

        # Set class name for better debugging
        DynamicRemoteService.__name__ = remote_tag
        DynamicRemoteService.__module__ = "unshackle.remote_services"

        # Set GEOFENCE and ALIASES
        if "geofence" in service_info:
            DynamicRemoteService.GEOFENCE = tuple(service_info["geofence"])
        if "aliases" in service_info:
            # Add "remote_" prefix to aliases too
            DynamicRemoteService.ALIASES = tuple(f"remote_{alias}" for alias in service_info["aliases"])

        # Register the service
        self.remote_services[remote_tag] = DynamicRemoteService

    def get_service(self, tag: str) -> Optional[type]:
        """
        Get a remote service class by tag.

        Args:
            tag: Service tag (e.g., "remote_DSNP")

        Returns:
            RemoteService class or None if not found
        """
        return self.remote_services.get(tag)

    def get_all_services(self) -> Dict[str, type]:
        """
        Get all registered remote services.

        Returns:
            Dictionary mapping service tags to RemoteService classes
        """
        return self.remote_services.copy()

    def get_service_path(self, tag: str) -> Optional[Path]:
        """
        Get the path for a remote service.

        Remote services don't have local paths, so this returns None.
        This method exists for compatibility with the Services interface.

        Args:
            tag: Service tag

        Returns:
            None (remote services have no local path)
        """
        return None


# Global instance
_remote_service_manager: Optional[RemoteServiceManager] = None


def get_remote_service_manager() -> RemoteServiceManager:
    """
    Get the global RemoteServiceManager instance.

    Creates the instance on first call and discovers services.

    Returns:
        RemoteServiceManager instance
    """
    global _remote_service_manager

    if _remote_service_manager is None:
        _remote_service_manager = RemoteServiceManager()
        try:
            _remote_service_manager.discover_services()
        except Exception as e:
            log.error(f"Failed to discover remote services: {e}")

    return _remote_service_manager


__all__ = ("RemoteServiceManager", "get_remote_service_manager")
