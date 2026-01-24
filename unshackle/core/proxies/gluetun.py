import atexit
import logging
import os
import re
import subprocess
import threading
import time
from typing import Optional

import requests

from unshackle.core import binaries
from unshackle.core.proxies.proxy import Proxy
from unshackle.core.utilities import get_country_code, get_country_name, get_debug_logger, get_ip_info

# Global registry for cleanup on exit
_gluetun_instances: list["Gluetun"] = []
_cleanup_lock = threading.Lock()
_cleanup_registered = False


def _cleanup_all_gluetun_containers():
    """Cleanup all Gluetun containers on exit."""
    # Get instances without holding the lock during cleanup
    with _cleanup_lock:
        instances = list(_gluetun_instances)
        _gluetun_instances.clear()

    # Cleanup each instance (no lock held, so no deadlock possible)
    for instance in instances:
        try:
            instance.cleanup()
        except Exception:
            pass


def _register_cleanup():
    """Register cleanup handlers (only once)."""
    global _cleanup_registered
    with _cleanup_lock:
        if not _cleanup_registered:
            # Only use atexit for cleanup - don't override signal handlers
            # This allows Ctrl+C to work normally while still cleaning up on exit
            atexit.register(_cleanup_all_gluetun_containers)
            _cleanup_registered = True


class Gluetun(Proxy):
    """
    Dynamic Gluetun VPN-to-HTTP Proxy Provider with multi-provider support.

    Automatically manages Docker containers running Gluetun for WireGuard/OpenVPN VPN connections.
    Supports multiple VPN providers in a single configuration using query format: provider:region

    Supported VPN providers: windscribe, expressvpn, nordvpn, surfshark, protonvpn, mullvad,
    privateinternetaccess, cyberghost, vyprvpn, torguard, and 50+ more.

    Configuration example in unshackle.yaml:
        proxy_providers:
          gluetun:
            providers:
              windscribe:
                vpn_type: wireguard
                credentials:
                  private_key: YOUR_KEY
                  addresses: YOUR_ADDRESS
                server_countries:
                  us: US
                  uk: GB
              nordvpn:
                vpn_type: wireguard
                credentials:
                  private_key: YOUR_KEY
                  addresses: YOUR_ADDRESS
                server_countries:
                  us: US
                  de: DE
            # Global settings (optional)
            base_port: 8888
            auto_cleanup: true
            container_prefix: "unshackle-gluetun"

    Usage:
        --proxy gluetun:windscribe:us
        --proxy gluetun:nordvpn:de
    """

    # Mapping of common VPN provider names to Gluetun identifiers
    PROVIDER_MAPPING = {
        "windscribe": "windscribe",
        "expressvpn": "expressvpn",
        "nordvpn": "nordvpn",
        "surfshark": "surfshark",
        "protonvpn": "protonvpn",
        "mullvad": "mullvad",
        "pia": "private internet access",
        "privateinternetaccess": "private internet access",
        "cyberghost": "cyberghost",
        "vyprvpn": "vyprvpn",
        "torguard": "torguard",
        "ipvanish": "ipvanish",
        "purevpn": "purevpn",
    }

    # Windscribe uses specific region names instead of country codes
    # See: https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/windscribe.md
    WINDSCRIBE_REGION_MAP = {
        # Country codes to Windscribe region names
        "us": "US East",
        "us-east": "US East",
        "us-west": "US West",
        "us-central": "US Central",
        "ca": "Canada East",
        "ca-east": "Canada East",
        "ca-west": "Canada West",
        "uk": "United Kingdom",
        "gb": "United Kingdom",
        "de": "Germany",
        "fr": "France",
        "nl": "Netherlands",
        "au": "Australia",
        "jp": "Japan",
        "sg": "Singapore",
        "hk": "Hong Kong",
        "kr": "South Korea",
        "in": "India",
        "it": "Italy",
        "es": "Spain",
        "ch": "Switzerland",
        "se": "Sweden",
        "no": "Norway",
        "dk": "Denmark",
        "fi": "Finland",
        "at": "Austria",
        "be": "Belgium",
        "ie": "Ireland",
        "pl": "Poland",
        "pt": "Portugal",
        "cz": "Czech Republic",
        "ro": "Romania",
        "hu": "Hungary",
        "gr": "Greece",
        "tr": "Turkey",
        "ru": "Russia",
        "ua": "Ukraine",
        "br": "Brazil",
        "mx": "Mexico",
        "ar": "Argentina",
        "za": "South Africa",
        "nz": "New Zealand",
        "th": "Thailand",
        "ph": "Philippines",
        "id": "Indonesia",
        "my": "Malaysia",
        "vn": "Vietnam",
        "tw": "Taiwan",
        "ae": "United Arab Emirates",
        "il": "Israel",
    }

    def __init__(
        self,
        providers: Optional[dict] = None,
        base_port: int = 8888,
        auto_cleanup: bool = True,
        container_prefix: str = "unshackle-gluetun",
        auth_user: Optional[str] = None,
        auth_password: Optional[str] = None,
        verify_ip: bool = True,
        **kwargs,
    ):
        """
        Initialize Gluetun proxy provider with multi-provider support.

        Args:
            providers: Dict of VPN provider configurations
                Format: {
                    "windscribe": {
                        "vpn_type": "wireguard",
                        "credentials": {"private_key": "...", "addresses": "..."},
                        "server_countries": {"us": "US", "uk": "GB"}
                    },
                    "nordvpn": {...}
                }
            base_port: Starting port for HTTP proxies (default: 8888)
            auto_cleanup: Automatically remove stopped containers (default: True)
            container_prefix: Docker container name prefix (default: "unshackle-gluetun")
            auth_user: Optional HTTP proxy authentication username
            auth_password: Optional HTTP proxy authentication password
            verify_ip: Automatically verify IP and region after connection (default: True)
        """
        # Check Docker availability using binaries module
        if not binaries.Docker:
            raise RuntimeError(
                "Docker is not available. Please install Docker to use Gluetun proxy.\n"
                "Visit: https://docs.docker.com/engine/install/"
            )

        self.providers = providers or {}
        self.base_port = base_port
        self.auto_cleanup = auto_cleanup
        self.container_prefix = container_prefix
        self.auth_user = auth_user
        self.auth_password = auth_password
        self.verify_ip = verify_ip

        # Track active containers: {query_key: {"container_name": ..., "port": ..., ...}}
        self.active_containers = {}

        # Lock for thread-safe port allocation
        self._port_lock = threading.Lock()

        # Validate provider configurations
        for provider_name, config in self.providers.items():
            self._validate_provider_config(provider_name, config)

        # Register this instance for cleanup on exit
        _register_cleanup()
        with _cleanup_lock:
            _gluetun_instances.append(self)

        # Log initialization
        debug_logger = get_debug_logger()
        if debug_logger:
            debug_logger.log(
                level="INFO",
                operation="gluetun_init",
                message=f"Gluetun proxy provider initialized with {len(self.providers)} provider(s)",
                context={
                    "providers": list(self.providers.keys()),
                    "base_port": base_port,
                    "auto_cleanup": auto_cleanup,
                    "verify_ip": verify_ip,
                    "container_prefix": container_prefix,
                },
            )

    def __repr__(self) -> str:
        provider_count = len(self.providers)
        return f"Gluetun ({provider_count} provider{['s', ''][provider_count == 1]})"

    def get_proxy(self, query: str) -> Optional[str]:
        """
        Get an HTTP proxy URI for a Gluetun VPN connection.

        Args:
            query: Query format: "provider:region" (e.g., "windscribe:us", "nordvpn:uk")

        Returns:
            HTTP proxy URI or None if unavailable
        """
        # Parse query
        parts = query.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid query format: '{query}'. Expected 'provider:region' (e.g., 'windscribe:us')")

        provider_name = parts[0].lower()
        region = parts[1].lower()

        # Check if provider is configured
        if provider_name not in self.providers:
            available = ", ".join(self.providers.keys())
            raise ValueError(f"VPN provider '{provider_name}' not configured. Available providers: {available}")

        # Create query key for tracking
        query_key = f"{provider_name}:{region}"
        container_name = f"{self.container_prefix}-{provider_name}-{region}"

        debug_logger = get_debug_logger()

        # Check if container already exists (in memory OR in Docker)
        # This handles multiple concurrent Unshackle sessions
        if query_key in self.active_containers:
            container = self.active_containers[query_key]
            if self._is_container_running(container["container_name"]):
                if debug_logger:
                    debug_logger.log(
                        level="DEBUG",
                        operation="gluetun_container_reuse",
                        message=f"Reusing existing container (in-memory): {query_key}",
                        context={
                            "query_key": query_key,
                            "container_name": container["container_name"],
                            "port": container["port"],
                        },
                    )
                # Re-verify if needed
                if self.verify_ip:
                    self._verify_container(query_key)
                return self._build_proxy_uri(container["port"])
        else:
            # Not in memory, but might exist in Docker (from another session)
            existing_info = self._get_existing_container_info(container_name)
            if existing_info:
                # Container exists in Docker, reuse it
                self.active_containers[query_key] = existing_info
                if debug_logger:
                    debug_logger.log(
                        level="INFO",
                        operation="gluetun_container_reuse_docker",
                        message=f"Reusing existing Docker container: {query_key}",
                        context={
                            "query_key": query_key,
                            "container_name": container_name,
                            "port": existing_info["port"],
                        },
                    )
                # Re-verify if needed
                if self.verify_ip:
                    self._verify_container(query_key)
                return self._build_proxy_uri(existing_info["port"])

        # Get provider configuration
        provider_config = self.providers[provider_name]

        # Determine server location
        server_countries = provider_config.get("server_countries", {})
        server_cities = provider_config.get("server_cities", {})
        server_hostnames = provider_config.get("server_hostnames", {})

        country = server_countries.get(region)
        city = server_cities.get(region)
        hostname = server_hostnames.get(region)

        # Check if region is a specific server pattern (e.g., us1239, uk5678)
        # Format: 2-letter country code + number
        specific_server_match = re.match(r"^([a-z]{2})(\d+)$", region, re.IGNORECASE)

        if specific_server_match and not country and not city and not hostname:
            # Specific server requested (e.g., us1239)
            country_code = specific_server_match.group(1).upper()
            server_num = specific_server_match.group(2)

            # Build hostname based on provider
            hostname = self._build_server_hostname(provider_name, country_code, server_num)
            country = country_code  # Set country for verification

        # If not explicitly mapped and not a specific server, try to use query as country code
        elif not country and not city and not hostname:
            if re.match(r"^[a-z]{2}$", region):
                # Convert country code to full name for Gluetun
                country = get_country_name(region)
                if not country:
                    raise ValueError(
                        f"Country code '{region}' not recognized. "
                        f"Configure it in server_countries or use a valid ISO 3166-1 alpha-2 code."
                    )
            else:
                raise ValueError(
                    f"Region '{region}' not recognized for provider '{provider_name}'. "
                    f"Configure it in server_countries or server_cities, or use a 2-letter country code."
                )

        # Remove any stopped container with the same name
        self._remove_stopped_container(container_name)

        # Find available port
        port = self._get_available_port()

        # Create container (name already set above)
        try:
            self._create_container(
                container_name=container_name,
                port=port,
                provider_name=provider_name,
                provider_config=provider_config,
                country=country,
                city=city,
                hostname=hostname,
            )

            # Store container info
            self.active_containers[query_key] = {
                "container_name": container_name,
                "port": port,
                "provider": provider_name,
                "region": region,
                "country": country,
                "city": city,
                "hostname": hostname,
            }

            # Wait for container to be ready (60s timeout for VPN connection)
            if not self._wait_for_container(container_name, timeout=60):
                # Get container logs for better error message
                logs = self._get_container_logs(container_name, tail=30)
                error_msg = f"Gluetun container '{container_name}' failed to start"
                if hasattr(self, "_last_wait_error") and self._last_wait_error:
                    error_msg += f": {self._last_wait_error}"
                if logs:
                    # Extract last few relevant lines
                    log_lines = [line for line in logs.strip().split("\n") if line.strip()][-5:]
                    error_msg += "\nRecent logs:\n" + "\n".join(log_lines)
                raise RuntimeError(error_msg)

            # Verify IP and region if enabled
            if self.verify_ip:
                self._verify_container(query_key)

            return self._build_proxy_uri(port)

        except Exception as e:
            # Cleanup on failure
            self._remove_container(container_name)
            if query_key in self.active_containers:
                del self.active_containers[query_key]
            raise RuntimeError(f"Failed to create Gluetun container: {e}")

    def cleanup(self):
        """Stop and remove all managed Gluetun containers."""
        debug_logger = get_debug_logger()
        container_count = len(self.active_containers)

        if container_count > 0 and debug_logger:
            debug_logger.log(
                level="DEBUG",
                operation="gluetun_cleanup_start",
                message=f"Cleaning up {container_count} Gluetun container(s)",
                context={
                    "container_count": container_count,
                    "containers": list(self.active_containers.keys()),
                },
            )

        for query_key, container_info in list(self.active_containers.items()):
            container_name = container_info["container_name"]
            self._remove_container(container_name)

            if debug_logger:
                debug_logger.log(
                    level="DEBUG",
                    operation="gluetun_container_removed",
                    message=f"Removed Gluetun container: {container_name}",
                    context={
                        "query_key": query_key,
                        "container_name": container_name,
                    },
                )

        self.active_containers.clear()

        if container_count > 0 and debug_logger:
            debug_logger.log(
                level="INFO",
                operation="gluetun_cleanup_complete",
                message=f"Cleanup complete: removed {container_count} container(s)",
                context={"container_count": container_count},
                success=True,
            )

    def get_connection_info(self, query: str) -> Optional[dict]:
        """
        Get connection info for a proxy query.

        Args:
            query: Query format "provider:region" (e.g., "windscribe:us")

        Returns:
            Dict with connection info including public_ip, country, city, or None if not found.
        """
        parts = query.split(":")
        if len(parts) != 2:
            return None

        provider_name = parts[0].lower()
        region = parts[1].lower()
        query_key = f"{provider_name}:{region}"

        container = self.active_containers.get(query_key)
        if not container:
            return None

        return {
            "provider": container.get("provider"),
            "region": container.get("region"),
            "public_ip": container.get("public_ip"),
            "country": container.get("ip_country"),
            "city": container.get("ip_city"),
            "org": container.get("ip_org"),
        }

    def _validate_provider_config(self, provider_name: str, config: dict):
        """Validate a provider's configuration."""
        vpn_type = config.get("vpn_type", "wireguard").lower()
        credentials = config.get("credentials", {})

        if vpn_type not in ["wireguard", "openvpn"]:
            raise ValueError(f"Provider '{provider_name}': Invalid vpn_type '{vpn_type}'. Use 'wireguard' or 'openvpn'")

        if vpn_type == "wireguard":
            # private_key is always required for WireGuard
            if "private_key" not in credentials:
                raise ValueError(f"Provider '{provider_name}': WireGuard requires 'private_key' in credentials")

            # Provider-specific WireGuard requirements based on Gluetun wiki:
            # - NordVPN, ProtonVPN: only private_key required
            # - Windscribe: private_key, addresses, AND preshared_key required (preshared_key MUST be set)
            # - Surfshark, Mullvad, IVPN: private_key AND addresses required
            provider_lower = provider_name.lower()

            # Windscribe requires preshared_key (can be empty string, but must be set)
            if provider_lower == "windscribe":
                if "preshared_key" not in credentials:
                    raise ValueError(
                        f"Provider '{provider_name}': Windscribe WireGuard requires 'preshared_key' in credentials "
                        "(can be empty string, but must be set). Get it from windscribe.com/getconfig/wireguard"
                    )
                if "addresses" not in credentials:
                    raise ValueError(
                        f"Provider '{provider_name}': Windscribe WireGuard requires 'addresses' in credentials. "
                        "Get it from windscribe.com/getconfig/wireguard"
                    )

            # Providers that require addresses (but not preshared_key)
            elif provider_lower in ["surfshark", "mullvad", "ivpn"]:
                if "addresses" not in credentials:
                    raise ValueError(f"Provider '{provider_name}': WireGuard requires 'addresses' in credentials")

        elif vpn_type == "openvpn":
            if "username" not in credentials or "password" not in credentials:
                raise ValueError(
                    f"Provider '{provider_name}': OpenVPN requires 'username' and 'password' in credentials"
                )

    def _get_available_port(self) -> int:
        """Find an available port starting from base_port (thread-safe)."""
        with self._port_lock:
            used_ports = {info["port"] for info in self.active_containers.values()}
            port = self.base_port
            while port in used_ports or self._is_port_in_use(port):
                port += 1
            return port

    def _is_port_in_use(self, port: int) -> bool:
        """Check if a port is in use on the system or by any Docker container."""
        import socket

        # First check if the port is available on the system
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
        except OSError:
            # Port is in use by something on the system
            return True

        # Also check Docker containers (in case of port forwarding)
        try:
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.Ports}}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return f":{port}->" in result.stdout or f"0.0.0.0:{port}" in result.stdout
            return False
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _build_server_hostname(self, provider_name: str, country_code: str, server_num: str) -> str:
        """
        Build a server hostname for specific server selection.

        Args:
            provider_name: VPN provider name (e.g., "nordvpn")
            country_code: 2-letter country code (e.g., "US")
            server_num: Server number (e.g., "1239")

        Returns:
            Server hostname (e.g., "us1239.nordvpn.com")
        """
        # Convert to lowercase for hostname
        country_lower = country_code.lower()

        # Provider-specific hostname formats
        hostname_formats = {
            "nordvpn": f"{country_lower}{server_num}.nordvpn.com",
            "surfshark": f"{country_lower}-{server_num}.prod.surfshark.com",
            "expressvpn": f"{country_lower}-{server_num}.expressvpn.com",
            "cyberghost": f"{country_lower}-s{server_num}.cg-dialup.net",
            # Generic fallback for other providers
        }

        # Get provider-specific format or use generic
        if provider_name in hostname_formats:
            return hostname_formats[provider_name]
        else:
            # Generic format: country_code + server_num
            return f"{country_lower}{server_num}"

    def _ensure_image_available(self, image: str = "qmcgaw/gluetun:latest") -> bool:
        """
        Ensure the Gluetun Docker image is available locally.

        If the image is not present, it will be pulled. This prevents
        the container creation from timing out during the first run.

        Args:
            image: Docker image name with tag

        Returns:
            True if image is available, False otherwise
        """
        log = logging.getLogger("Gluetun")

        # Check if image exists locally
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                return True
            log.debug(f"Image inspect failed: {result.stderr}")
        except subprocess.TimeoutExpired:
            log.warning("Docker image inspect timed out")
        except FileNotFoundError:
            log.error("Docker command not found - is Docker installed and in PATH?")
            return False

        # Image not found, pull it
        log.info(f"Pulling Docker image {image}...")
        try:
            result = subprocess.run(
                ["docker", "pull", image],
                capture_output=True,
                text=True,
                timeout=300,  # 5 minutes for pull
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                return True
            log.error(f"Docker pull failed: {result.stderr}")
            return False
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Timed out pulling Docker image '{image}'")

    def _create_container(
        self,
        container_name: str,
        port: int,
        provider_name: str,
        provider_config: dict,
        country: Optional[str] = None,
        city: Optional[str] = None,
        hostname: Optional[str] = None,
    ):
        """Create and start a Gluetun Docker container."""
        debug_logger = get_debug_logger()
        start_time = time.time()

        if debug_logger:
            debug_logger.log(
                level="DEBUG",
                operation="gluetun_container_create_start",
                message=f"Creating Gluetun container: {container_name}",
                context={
                    "container_name": container_name,
                    "port": port,
                    "provider": provider_name,
                    "country": country,
                    "city": city,
                    "hostname": hostname,
                },
            )

        # Ensure the Gluetun image is available (pulls if needed)
        gluetun_image = "qmcgaw/gluetun:latest"
        if not self._ensure_image_available(gluetun_image):
            if debug_logger:
                debug_logger.log(
                    level="ERROR",
                    operation="gluetun_image_pull_failed",
                    message=f"Failed to pull Docker image: {gluetun_image}",
                    success=False,
                )
            raise RuntimeError(f"Failed to ensure Gluetun Docker image '{gluetun_image}' is available")

        vpn_type = provider_config.get("vpn_type", "wireguard").lower()
        credentials = provider_config.get("credentials", {})
        extra_env = provider_config.get("extra_env", {})

        # Normalize provider name
        gluetun_provider = self.PROVIDER_MAPPING.get(provider_name.lower(), provider_name.lower())

        # Build environment variables
        env_vars = {
            "VPN_SERVICE_PROVIDER": gluetun_provider,
            "VPN_TYPE": vpn_type,
            "HTTPPROXY": "on",
            "HTTPPROXY_LISTENING_ADDRESS": ":8888",
            "HTTPPROXY_LOG": "on",
            "TZ": os.environ.get("TZ", "UTC"),
            "LOG_LEVEL": "info",
        }

        # Add credentials
        if vpn_type == "wireguard":
            env_vars["WIREGUARD_PRIVATE_KEY"] = credentials["private_key"]
            # addresses is optional - not needed for some providers like NordVPN
            if "addresses" in credentials:
                env_vars["WIREGUARD_ADDRESSES"] = credentials["addresses"]
            # preshared_key is required for Windscribe, optional for others
            if "preshared_key" in credentials:
                env_vars["WIREGUARD_PRESHARED_KEY"] = credentials["preshared_key"]
        elif vpn_type == "openvpn":
            env_vars["OPENVPN_USER"] = credentials.get("username", "")
            env_vars["OPENVPN_PASSWORD"] = credentials.get("password", "")

        # Add server location
        # Priority: hostname > country + city > country only
        # Note: Different providers support different server selection variables
        # - Most providers: SERVER_COUNTRIES, SERVER_CITIES
        # - Windscribe, VyprVPN, VPN Secure: SERVER_REGIONS, SERVER_CITIES (no SERVER_COUNTRIES)
        if hostname:
            # Specific server hostname requested (e.g., us1239.nordvpn.com)
            env_vars["SERVER_HOSTNAMES"] = hostname
        else:
            # Providers that use SERVER_REGIONS instead of SERVER_COUNTRIES
            region_only_providers = {"windscribe", "vyprvpn", "vpn secure"}
            uses_regions = gluetun_provider in region_only_providers

            # Use country/city selection
            if country:
                if uses_regions:
                    # Convert country code to provider-specific region name
                    if gluetun_provider == "windscribe":
                        region_name = self.WINDSCRIBE_REGION_MAP.get(country.lower(), country)
                        env_vars["SERVER_REGIONS"] = region_name
                    else:
                        env_vars["SERVER_REGIONS"] = country
                else:
                    env_vars["SERVER_COUNTRIES"] = country
            if city:
                env_vars["SERVER_CITIES"] = city

        # Add authentication if configured
        if self.auth_user:
            env_vars["HTTPPROXY_USER"] = self.auth_user
        if self.auth_password:
            env_vars["HTTPPROXY_PASSWORD"] = self.auth_password

        # Merge extra environment variables
        env_vars.update(extra_env)

        # Debug log environment variables (redact sensitive values)
        if debug_logger:
            safe_env = {k: ("***" if "KEY" in k or "PASSWORD" in k else v) for k, v in env_vars.items()}
            debug_logger.log(
                level="DEBUG",
                operation="gluetun_env_vars",
                message=f"Environment variables for {container_name}",
                context={"env_vars": safe_env, "gluetun_provider": gluetun_provider},
            )

        # Build docker run command
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--cap-add=NET_ADMIN",
            "--device=/dev/net/tun",
            "-p",
            f"127.0.0.1:{port}:8888/tcp",
        ]

        # Add environment variables
        for key, value in env_vars.items():
            cmd.extend(["-e", f"{key}={value}"])

        # Add Gluetun image
        cmd.append("qmcgaw/gluetun:latest")

        # Execute docker run
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )

            if result.returncode != 0:
                error_msg = result.stderr or "unknown error"
                if debug_logger:
                    debug_logger.log(
                        level="ERROR",
                        operation="gluetun_container_create_failed",
                        message=f"Docker run failed for {container_name}",
                        context={
                            "container_name": container_name,
                            "return_code": result.returncode,
                            "stderr": error_msg,
                        },
                        success=False,
                        duration_ms=(time.time() - start_time) * 1000,
                    )
                raise RuntimeError(f"Docker run failed: {error_msg}")

            # Log successful container creation
            if debug_logger:
                duration_ms = (time.time() - start_time) * 1000
                debug_logger.log(
                    level="INFO",
                    operation="gluetun_container_created",
                    message=f"Gluetun container created: {container_name}",
                    context={
                        "container_name": container_name,
                        "port": port,
                        "provider": provider_name,
                        "vpn_type": vpn_type,
                        "country": country,
                        "city": city,
                        "hostname": hostname,
                        "container_id": result.stdout.strip()[:12] if result.stdout else None,
                    },
                    success=True,
                    duration_ms=duration_ms,
                )

        except subprocess.TimeoutExpired:
            if debug_logger:
                debug_logger.log(
                    level="ERROR",
                    operation="gluetun_container_create_timeout",
                    message=f"Docker run timed out for {container_name}",
                    context={"container_name": container_name},
                    success=False,
                    duration_ms=(time.time() - start_time) * 1000,
                )
            raise RuntimeError("Docker run command timed out")

    def _is_container_running(self, container_name: str) -> bool:
        """Check if a Docker container is running."""
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0 and container_name in result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _get_existing_container_info(self, container_name: str) -> Optional[dict]:
        """
        Check if a container exists in Docker and get its info.

        This handles multiple Unshackle sessions - if another session already
        created the container, we'll reuse it instead of trying to create a duplicate.

        Args:
            container_name: Name of the container to check

        Returns:
            Dict with container info if exists and running, None otherwise
        """
        try:
            # Check if container is running
            if not self._is_container_running(container_name):
                return None

            # Get container port mapping
            # Format: "127.0.0.1:8888->8888/tcp"
            result = subprocess.run(
                ["docker", "inspect", container_name, "--format", "{{.NetworkSettings.Ports}}"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode != 0:
                return None

            # Parse port from output like "map[8888/tcp:[{127.0.0.1 8888}]]"
            port_match = re.search(r"127\.0\.0\.1\s+(\d+)", result.stdout)
            if not port_match:
                return None

            port = int(port_match.group(1))

            # Extract provider and region from container name
            # Format: unshackle-gluetun-provider-region
            name_pattern = f"{self.container_prefix}-(.+)-([^-]+)$"
            name_match = re.match(name_pattern, container_name)
            if not name_match:
                return None

            provider_name = name_match.group(1)
            region = name_match.group(2)

            # Get expected country and hostname from config (if available)
            country = None
            hostname = None

            # Check if region is a specific server (e.g., us1239)
            specific_server_match = re.match(r"^([a-z]{2})(\d+)$", region, re.IGNORECASE)
            if specific_server_match:
                country_code = specific_server_match.group(1).upper()
                server_num = specific_server_match.group(2)
                hostname = self._build_server_hostname(provider_name, country_code, server_num)
                country = country_code

            # Otherwise check config
            elif provider_name in self.providers:
                provider_config = self.providers[provider_name]
                server_countries = provider_config.get("server_countries", {})
                country = server_countries.get(region)

                if not country and re.match(r"^[a-z]{2}$", region):
                    country = region.upper()

            return {
                "container_name": container_name,
                "port": port,
                "provider": provider_name,
                "region": region,
                "country": country,
                "city": None,
                "hostname": hostname,
            }

        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            return None

    def _wait_for_container(self, container_name: str, timeout: int = 60) -> bool:
        """
        Wait for Gluetun container to be ready by checking logs for proxy readiness.

        Gluetun logs "http proxy listening" when the HTTP proxy is ready to accept connections.

        Args:
            container_name: Name of the container to wait for
            timeout: Maximum time to wait in seconds (default: 60)

        Returns:
            True if container is ready, False if it failed or timed out
        """
        debug_logger = get_debug_logger()
        start_time = time.time()
        last_error = None

        if debug_logger:
            debug_logger.log(
                level="DEBUG",
                operation="gluetun_container_wait_start",
                message=f"Waiting for container to be ready: {container_name}",
                context={"container_name": container_name, "timeout": timeout},
            )

        while time.time() - start_time < timeout:
            try:
                # First check if container is still running
                if not self._is_container_running(container_name):
                    # Container may have exited - check if it crashed
                    exit_info = self._get_container_exit_info(container_name)
                    if exit_info:
                        last_error = f"Container exited with code {exit_info.get('exit_code', 'unknown')}"
                    time.sleep(1)
                    continue

                # Check logs for readiness indicators
                result = subprocess.run(
                    ["docker", "logs", container_name, "--tail", "100"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    encoding="utf-8",
                    errors="replace",
                )

                if result.returncode == 0:
                    # Combine stdout and stderr for checking (handle None values)
                    stdout = result.stdout or ""
                    stderr = result.stderr or ""
                    all_logs = (stdout + stderr).lower()

                    # Gluetun needs both proxy listening AND VPN connected
                    # The proxy starts before VPN is ready, so we need to wait for VPN
                    proxy_ready = "[http proxy] listening" in all_logs
                    vpn_ready = "initialization sequence completed" in all_logs

                    if proxy_ready and vpn_ready:
                        # Give a brief moment for the proxy to fully initialize
                        time.sleep(1)
                        duration_ms = (time.time() - start_time) * 1000
                        if debug_logger:
                            debug_logger.log(
                                level="INFO",
                                operation="gluetun_container_ready",
                                message=f"Gluetun container is ready: {container_name}",
                                context={
                                    "container_name": container_name,
                                    "proxy_ready": proxy_ready,
                                    "vpn_ready": vpn_ready,
                                },
                                success=True,
                                duration_ms=duration_ms,
                            )
                        return True

                    # Check for fatal errors that indicate VPN connection failure
                    error_indicators = [
                        "fatal",
                        "cannot connect",
                        "authentication failed",
                        "invalid credentials",
                        "connection refused",
                        "no valid servers",
                    ]

                    for error in error_indicators:
                        if error in all_logs:
                            # Extract the error line for better messaging
                            for line in (stdout + stderr).split("\n"):
                                if error in line.lower():
                                    last_error = line.strip()
                                    break
                            # Fatal errors mean we should stop waiting
                            if "fatal" in all_logs or "invalid credentials" in all_logs:
                                return False

            except subprocess.TimeoutExpired:
                pass

            time.sleep(2)

        # Store the last error for potential logging
        if last_error:
            self._last_wait_error = last_error

        # Log timeout/failure
        duration_ms = (time.time() - start_time) * 1000
        if debug_logger:
            debug_logger.log(
                level="ERROR",
                operation="gluetun_container_wait_timeout",
                message=f"Gluetun container failed to become ready: {container_name}",
                context={
                    "container_name": container_name,
                    "timeout": timeout,
                    "last_error": last_error,
                },
                success=False,
                duration_ms=duration_ms,
            )
        return False

    def _get_container_exit_info(self, container_name: str) -> Optional[dict]:
        """Get exit information for a stopped container."""
        try:
            result = subprocess.run(
                ["docker", "inspect", container_name, "--format", "{{.State.ExitCode}}:{{.State.Error}}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split(":", 1)
                return {
                    "exit_code": int(parts[0]) if parts[0].isdigit() else -1,
                    "error": parts[1] if len(parts) > 1 else "",
                }
            return None
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            return None

    def _get_container_logs(self, container_name: str, tail: int = 50) -> str:
        """Get recent logs from a container for error reporting."""
        try:
            result = subprocess.run(
                ["docker", "logs", container_name, "--tail", str(tail)],
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
            )
            return (result.stdout or "") + (result.stderr or "")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""

    def _verify_container(self, query_key: str, max_retries: int = 3):
        """
        Verify container's VPN IP and region using ipinfo.io lookup.

        Uses the shared get_ip_info function with a session configured to use
        the Gluetun proxy. Retries with exponential backoff if the network
        isn't ready immediately after the VPN connects.

        Args:
            query_key: The container query key (provider:region)
            max_retries: Maximum number of retry attempts (default: 3)

        Raises:
            RuntimeError: If verification fails after all retries
        """
        debug_logger = get_debug_logger()
        start_time = time.time()

        if query_key not in self.active_containers:
            return

        container = self.active_containers[query_key]
        proxy_url = self._build_proxy_uri(container["port"])
        expected_country = container.get("country", "").upper()

        if debug_logger:
            debug_logger.log(
                level="DEBUG",
                operation="gluetun_verify_start",
                message=f"Verifying VPN IP for: {query_key}",
                context={
                    "query_key": query_key,
                    "container_name": container.get("container_name"),
                    "expected_country": expected_country,
                    "max_retries": max_retries,
                },
            )

        last_error = None

        # Create a session with the proxy configured
        session = requests.Session()
        session.proxies = {"http": proxy_url, "https": proxy_url}

        # Retry with exponential backoff
        for attempt in range(max_retries):
            try:
                # Get external IP through the proxy using shared utility
                ip_info = get_ip_info(session)

                if ip_info:
                    actual_country = ip_info.get("country", "").upper()

                    # Check if country matches (if we have an expected country)
                    # ipinfo.io returns country codes (CA), but we may have full names (Canada)
                    # Normalize both to country codes for comparison using shared utility
                    if expected_country:
                        # Convert expected country name to code if it's a full name
                        expected_code = get_country_code(expected_country) or expected_country
                        expected_code = expected_code.upper()

                        if actual_country != expected_code:
                            duration_ms = (time.time() - start_time) * 1000
                            if debug_logger:
                                debug_logger.log(
                                    level="ERROR",
                                    operation="gluetun_verify_mismatch",
                                    message=f"Region mismatch for {query_key}",
                                    context={
                                        "query_key": query_key,
                                        "expected_country": expected_code,
                                        "actual_country": actual_country,
                                        "ip": ip_info.get("ip"),
                                        "city": ip_info.get("city"),
                                        "org": ip_info.get("org"),
                                    },
                                    success=False,
                                    duration_ms=duration_ms,
                                )
                            raise RuntimeError(
                                f"Region mismatch for {container['provider']}:{container['region']}: "
                                f"Expected '{expected_code}' but got '{actual_country}' "
                                f"(IP: {ip_info.get('ip')}, City: {ip_info.get('city')})"
                            )

                    # Verification successful - store IP info in container record
                    if query_key in self.active_containers:
                        self.active_containers[query_key]["public_ip"] = ip_info.get("ip")
                        self.active_containers[query_key]["ip_country"] = actual_country
                        self.active_containers[query_key]["ip_city"] = ip_info.get("city")
                        self.active_containers[query_key]["ip_org"] = ip_info.get("org")

                    duration_ms = (time.time() - start_time) * 1000
                    if debug_logger:
                        debug_logger.log(
                            level="INFO",
                            operation="gluetun_verify_success",
                            message=f"VPN IP verified for: {query_key}",
                            context={
                                "query_key": query_key,
                                "ip": ip_info.get("ip"),
                                "country": actual_country,
                                "city": ip_info.get("city"),
                                "org": ip_info.get("org"),
                                "attempts": attempt + 1,
                            },
                            success=True,
                            duration_ms=duration_ms,
                        )
                    return

                # ip_info was None, retry
                last_error = "Failed to get IP info from ipinfo.io"

            except RuntimeError:
                raise  # Re-raise region mismatch errors immediately
            except Exception as e:
                last_error = str(e)
                if debug_logger:
                    debug_logger.log(
                        level="DEBUG",
                        operation="gluetun_verify_retry",
                        message=f"Verification attempt {attempt + 1} failed, retrying",
                        context={
                            "query_key": query_key,
                            "attempt": attempt + 1,
                            "error": last_error,
                        },
                    )

            # Wait before retry (exponential backoff)
            if attempt < max_retries - 1:
                wait_time = 2**attempt  # 1, 2, 4 seconds
                time.sleep(wait_time)

        # All retries exhausted
        duration_ms = (time.time() - start_time) * 1000
        if debug_logger:
            debug_logger.log(
                level="ERROR",
                operation="gluetun_verify_failed",
                message=f"VPN verification failed after {max_retries} attempts",
                context={
                    "query_key": query_key,
                    "max_retries": max_retries,
                    "last_error": last_error,
                },
                success=False,
                duration_ms=duration_ms,
            )
        raise RuntimeError(
            f"Failed to verify VPN IP for {container['provider']}:{container['region']} "
            f"after {max_retries} attempts. Last error: {last_error}"
        )

    def _remove_stopped_container(self, container_name: str) -> bool:
        """
        Remove a stopped container with the given name if it exists.

        This prevents "container name already in use" errors when a previous
        container wasn't properly cleaned up.

        Args:
            container_name: Name of the container to check and remove

        Returns:
            True if a container was removed, False otherwise
        """
        try:
            # Check if container exists (running or stopped)
            result = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}:{{.Status}}"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode != 0 or not result.stdout.strip():
                return False

            # Parse status - format is "name:Up 2 hours" or "name:Exited (0) 2 hours ago"
            output = result.stdout.strip()
            if container_name not in output:
                return False

            # Check if container is stopped (not running)
            if "Exited" in output or "Created" in output or "Dead" in output:
                # Container exists but is stopped - remove it
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return True

            return False

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _remove_container(self, container_name: str):
        """Stop and remove a Docker container."""
        try:
            if self.auto_cleanup:
                # Use docker rm -f to force remove (stops and removes in one command)
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            else:
                # Just stop the container
                subprocess.run(
                    ["docker", "stop", container_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
        except subprocess.TimeoutExpired:
            # Force kill if timeout
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except subprocess.TimeoutExpired:
                pass

    def _build_proxy_uri(self, port: int) -> str:
        """Build HTTP proxy URI."""
        if self.auth_user and self.auth_password:
            return f"http://{self.auth_user}:{self.auth_password}@localhost:{port}"
        return f"http://localhost:{port}"

    def __del__(self):
        """Cleanup containers on object destruction."""
        if hasattr(self, "auto_cleanup") and self.auto_cleanup:
            try:
                if hasattr(self, "active_containers") and self.active_containers:
                    self.cleanup()
            except Exception:
                pass
