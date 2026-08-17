import atexit
import logging
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from typing import Optional

import requests

from unshackle.core import binaries
from unshackle.core.proxies.proxy import Proxy
from unshackle.core.utilities import get_country_code, get_country_name, get_debug_logger, log_event
from unshackle.core.utils.ip_info import get_ip_info

gluetun_instances: list["Gluetun"] = []
cleanup_lock = threading.Lock()
cleanup_registered = False


def cleanup_all_gluetun_containers():
    """Cleanup all Gluetun containers on exit."""
    # Get instances without holding the lock during cleanup
    with cleanup_lock:
        instances = list(gluetun_instances)
        gluetun_instances.clear()

    # Cleanup each instance (no lock held, so no deadlock possible)
    for instance in instances:
        try:
            instance.cleanup()
        except Exception:
            pass


def register_cleanup():
    """Register cleanup handlers (only once)."""
    global cleanup_registered
    with cleanup_lock:
        if not cleanup_registered:
            # Only use atexit for cleanup - don't override signal handlers
            # This allows Ctrl+C to work normally while still cleaning up on exit
            atexit.register(cleanup_all_gluetun_containers)
            cleanup_registered = True


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

        self._port_lock = threading.Lock()

        for provider_name, config in self.providers.items():
            self.validate_provider_config(provider_name, config)

        register_cleanup()
        with cleanup_lock:
            gluetun_instances.append(self)

        log_event(
            "gluetun_init",
            level="INFO",
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
        parts = query.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid query format: '{query}'. Expected 'provider:region' (e.g., 'windscribe:us')")

        provider_name = parts[0].lower()
        region = parts[1].lower()

        if provider_name not in self.providers:
            available = ", ".join(self.providers.keys())
            raise ValueError(f"VPN provider '{provider_name}' not configured. Available providers: {available}")

        query_key = f"{provider_name}:{region}"
        container_name = f"{self.container_prefix}-{provider_name}-{region}"

        # Handle multiple concurrent Unshackle sessions: check if container exists in memory or in Docker
        if query_key in self.active_containers:
            container = self.active_containers[query_key]
            if self.is_container_running(container["container_name"]):
                log_event(
                    "gluetun_container_reuse",
                    level="DEBUG",
                    message=f"Reusing existing container (in-memory): {query_key}",
                    context={
                        "query_key": query_key,
                        "container_name": container["container_name"],
                        "port": container["port"],
                    },
                )
                if self.verify_ip:
                    self.verify_container(query_key)
                return self.build_proxy_uri(container["port"])
        else:
            # Not in memory, but might exist in Docker (from another session)
            existing_info = self.get_existing_container_info(container_name)
            if existing_info:
                # Container exists in Docker, reuse it
                self.active_containers[query_key] = existing_info
                log_event(
                    "gluetun_container_reuse_docker",
                    level="INFO",
                    message=f"Reusing existing Docker container: {query_key}",
                    context={
                        "query_key": query_key,
                        "container_name": container_name,
                        "port": existing_info["port"],
                    },
                )
                if self.verify_ip:
                    self.verify_container(query_key)
                return self.build_proxy_uri(existing_info["port"])

        provider_config = self.providers[provider_name]

        server_countries = provider_config.get("server_countries", {})
        server_cities = provider_config.get("server_cities", {})
        server_hostnames = provider_config.get("server_hostnames", {})

        country = server_countries.get(region)
        city = server_cities.get(region)
        hostname = server_hostnames.get(region)

        # Region may specify a server: country code + number (e.g., us1239, uk5678)
        specific_server_match = re.match(r"^([a-z]{2})(\d+)$", region, re.IGNORECASE)

        if specific_server_match and not country and not city and not hostname:
            country_code = specific_server_match.group(1).upper()
            server_num = specific_server_match.group(2)

            hostname = self.build_server_hostname(provider_name, country_code, server_num)
            country = country_code

        elif not country and not city and not hostname:
            if re.match(r"^[a-z]{2}$", region):
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

        self.remove_stopped_container(container_name)

        port = self.get_available_port()

        try:
            self.create_container(
                container_name=container_name,
                port=port,
                provider_name=provider_name,
                provider_config=provider_config,
                country=country,
                city=city,
                hostname=hostname,
            )

            self.active_containers[query_key] = {
                "container_name": container_name,
                "port": port,
                "provider": provider_name,
                "region": region,
                "country": country,
                "city": city,
                "hostname": hostname,
            }

            if not self.wait_for_container(container_name, timeout=60):
                logs = self.get_container_logs(container_name, tail=30)
                error_msg = f"Gluetun container '{container_name}' failed to start"
                if hasattr(self, "_last_wait_error") and self._last_wait_error:
                    error_msg += f": {self._last_wait_error}"
                if logs:
                    log_lines = [line for line in logs.strip().split("\n") if line.strip()][-5:]
                    error_msg += "\nRecent logs:\n" + "\n".join(log_lines)
                raise RuntimeError(error_msg)

            if self.verify_ip:
                self.verify_container(query_key)

            return self.build_proxy_uri(port)

        except Exception as e:
            self.remove_container(container_name)
            if query_key in self.active_containers:
                del self.active_containers[query_key]
            raise RuntimeError(f"Failed to create Gluetun container: {e}")

    def cleanup(self):
        """Stop and remove all managed Gluetun containers."""
        container_count = len(self.active_containers)

        if container_count > 0:
            log_event(
                "gluetun_cleanup_start",
                level="DEBUG",
                message=f"Cleaning up {container_count} Gluetun container(s)",
                context={
                    "container_count": container_count,
                    "containers": list(self.active_containers.keys()),
                },
            )

        for query_key, container_info in list(self.active_containers.items()):
            container_name = container_info["container_name"]
            self.remove_container(container_name)

            log_event(
                "gluetun_container_removed",
                level="DEBUG",
                message=f"Removed Gluetun container: {container_name}",
                context={
                    "query_key": query_key,
                    "container_name": container_name,
                },
            )

        self.active_containers.clear()

        if container_count > 0:
            log_event(
                "gluetun_cleanup_complete",
                level="INFO",
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

    def validate_provider_config(self, provider_name: str, config: dict):
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

    def get_available_port(self) -> int:
        """Find an available port starting from base_port (thread-safe)."""
        with self._port_lock:
            used_ports = {info["port"] for info in self.active_containers.values()}
            port = self.base_port
            while port in used_ports or self.is_port_in_use(port):
                port += 1
            return port

    def is_port_in_use(self, port: int) -> bool:
        """Check if a port is in use on the system or by any Docker container."""
        import socket

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
        except OSError:
            return True

        try:
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.Ports}}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if result.returncode == 0:
                return f":{port}->" in result.stdout or f"0.0.0.0:{port}" in result.stdout
            return False
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def build_server_hostname(self, provider_name: str, country_code: str, server_num: str) -> str:
        """
        Build a server hostname for specific server selection.

        Args:
            provider_name: VPN provider name (e.g., "nordvpn")
            country_code: 2-letter country code (e.g., "US")
            server_num: Server number (e.g., "1239")

        Returns:
            Server hostname (e.g., "us1239.nordvpn.com")
        """
        country_lower = country_code.lower()
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
            return f"{country_lower}{server_num}"

    def ensure_image_available(self, image: str = "qmcgaw/gluetun:latest") -> bool:
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

    def create_container(
        self,
        container_name: str,
        port: int,
        provider_name: str,
        provider_config: dict,
        country: Optional[str] = None,
        city: Optional[str] = None,
        hostname: Optional[str] = None,
    ):
        """
        Create and start a Gluetun Docker container.

        Credentials go into a temporary env-file with restricted permissions and are passed with --env-file,
        never as "-e KEY=VALUE" arguments, so they stay out of process listings. The file is overwritten and
        deleted once "docker run" returns.
        """
        start_time = time.time()

        log_event(
            "gluetun_container_create_start",
            level="DEBUG",
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

        gluetun_image = "qmcgaw/gluetun:latest"
        if not self.ensure_image_available(gluetun_image):
            log_event(
                "gluetun_image_pull_failed",
                level="ERROR",
                message=f"Failed to pull Docker image: {gluetun_image}",
                success=False,
            )
            raise RuntimeError(f"Failed to ensure Gluetun Docker image '{gluetun_image}' is available")

        vpn_type = provider_config.get("vpn_type", "wireguard").lower()
        credentials = provider_config.get("credentials", {})
        extra_env = provider_config.get("extra_env", {})

        gluetun_provider = self.PROVIDER_MAPPING.get(provider_name.lower(), provider_name.lower())

        env_vars = {
            "VPN_SERVICE_PROVIDER": gluetun_provider,
            "VPN_TYPE": vpn_type,
            "HTTPPROXY": "on",
            "HTTPPROXY_LISTENING_ADDRESS": ":8888",
            "HTTPPROXY_LOG": "on",
            "TZ": os.environ.get("TZ", "UTC"),
            "LOG_LEVEL": "info",
        }

        if vpn_type == "wireguard":
            env_vars["WIREGUARD_PRIVATE_KEY"] = credentials["private_key"]
            if "addresses" in credentials:
                env_vars["WIREGUARD_ADDRESSES"] = credentials["addresses"]
            if "preshared_key" in credentials:
                env_vars["WIREGUARD_PRESHARED_KEY"] = credentials["preshared_key"]
        elif vpn_type == "openvpn":
            env_vars["OPENVPN_USER"] = credentials.get("username", "")
            env_vars["OPENVPN_PASSWORD"] = credentials.get("password", "")

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

        if self.auth_user:
            env_vars["HTTPPROXY_USER"] = self.auth_user
        if self.auth_password:
            env_vars["HTTPPROXY_PASSWORD"] = self.auth_password

        env_vars.update(extra_env)

        if debug_logger := get_debug_logger():
            redact_markers = ("KEY", "PASSWORD", "PASS", "TOKEN", "SECRET", "USER")
            safe_env = {k: ("***" if any(m in k for m in redact_markers) else v) for k, v in env_vars.items()}
            debug_logger.log(
                level="DEBUG",
                operation="gluetun_env_vars",
                message=f"Environment variables for {container_name}",
                context={"env_vars": safe_env, "gluetun_provider": gluetun_provider},
            )

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

        env_file_path: str | None = None
        try:
            fd, env_file_path = tempfile.mkstemp(prefix=f"unshackle-{container_name}-", suffix=".env")
            try:
                if os.name != "nt":
                    if hasattr(os, "fchmod"):
                        os.fchmod(fd, 0o600)
                    else:
                        os.chmod(env_file_path, 0o600)
                else:
                    os.chmod(env_file_path, stat.S_IREAD | stat.S_IWRITE)

                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    for key, value in env_vars.items():
                        if "=" in key:
                            raise ValueError(f"Invalid env var name for docker env-file: {key!r}")
                        v = "" if value is None else str(value)
                        if "\n" in v or "\r" in v:
                            raise ValueError(f"Invalid env var value (contains newline) for {key!r}")
                        f.write(f"{key}={v}\n")
            except Exception:
                # If we fail before fdopen closes the descriptor, make sure it's not leaked.
                try:
                    os.close(fd)
                except Exception:
                    pass
                raise

            cmd.extend(["--env-file", env_file_path])

            cmd.append(gluetun_image)

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    encoding="utf-8",
                    errors="replace",
                )
            except subprocess.TimeoutExpired:
                log_event(
                    "gluetun_container_create_timeout",
                    level="ERROR",
                    message=f"Docker run timed out for {container_name}",
                    context={"container_name": container_name},
                    success=False,
                    duration_ms=(time.time() - start_time) * 1000,
                )
                raise RuntimeError("Docker run command timed out")

            if result.returncode != 0:
                error_msg = result.stderr or "unknown error"
                log_event(
                    "gluetun_container_create_failed",
                    level="ERROR",
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

            log_event(
                "gluetun_container_created",
                level="INFO",
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
                duration_ms=(time.time() - start_time) * 1000,
            )
        finally:
            if env_file_path:
                try:
                    with open(env_file_path, "r+b") as f:
                        try:
                            f.seek(0, os.SEEK_END)
                            length = f.tell()
                            f.seek(0)
                            if length > 0:
                                f.write(b"\x00" * length)
                                f.flush()
                                os.fsync(f.fileno())
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    os.remove(env_file_path)
                except FileNotFoundError:
                    pass
                except Exception:
                    pass

    def is_container_running(self, container_name: str) -> bool:
        """Check if a Docker container is running."""
        try:
            result = subprocess.run(
                [
                    "docker",
                    "ps",
                    "--filter",
                    f"name=^{re.escape(container_name)}$",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if result.returncode != 0:
                return False

            names = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
            return any(name == container_name for name in names)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def get_existing_container_info(self, container_name: str) -> Optional[dict]:
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
            if not self.is_container_running(container_name):
                return None

            # Get container port mapping
            # Format: "127.0.0.1:8888->8888/tcp"
            result = subprocess.run(
                ["docker", "inspect", container_name, "--format", "{{.NetworkSettings.Ports}}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )

            if result.returncode != 0:
                return None

            # Parse port from output like "map[8888/tcp:[{127.0.0.1 8888}]]"
            port_match = re.search(r"127\.0\.0\.1\s+(\d+)", result.stdout)
            if not port_match:
                return None

            port = int(port_match.group(1))

            # Format: unshackle-gluetun-provider-region
            name_pattern = f"{self.container_prefix}-(.+)-([^-]+)$"
            name_match = re.match(name_pattern, container_name)
            if not name_match:
                return None

            provider_name = name_match.group(1)
            region = name_match.group(2)

            country = None
            hostname = None

            specific_server_match = re.match(r"^([a-z]{2})(\d+)$", region, re.IGNORECASE)
            if specific_server_match:
                country_code = specific_server_match.group(1).upper()
                server_num = specific_server_match.group(2)
                hostname = self.build_server_hostname(provider_name, country_code, server_num)
                country = country_code

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

    def wait_for_container(self, container_name: str, timeout: int = 60) -> bool:
        """
        Wait for Gluetun container to be ready by checking logs for proxy readiness.

        Gluetun logs "http proxy listening" when the HTTP proxy is ready to accept connections.

        Args:
            container_name: Name of the container to wait for
            timeout: Maximum time to wait in seconds (default: 60)

        Returns:
            True if container is ready, False if it failed or timed out
        """
        start_time = time.time()
        last_error = None

        log_event(
            "gluetun_container_wait_start",
            level="DEBUG",
            message=f"Waiting for container to be ready: {container_name}",
            context={"container_name": container_name, "timeout": timeout},
        )

        while time.time() - start_time < timeout:
            try:
                if not self.is_container_running(container_name):
                    exit_info = self.get_container_exit_info(container_name)
                    if exit_info:
                        last_error = f"Container exited with code {exit_info.get('exit_code', 'unknown')}"
                    time.sleep(1)
                    continue

                result = subprocess.run(
                    ["docker", "logs", container_name, "--tail", "100"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    encoding="utf-8",
                    errors="replace",
                )

                if result.returncode == 0:
                    stdout = result.stdout or ""
                    stderr = result.stderr or ""
                    all_logs = (stdout + stderr).lower()

                    # Gluetun needs both proxy listening AND VPN connected
                    # The proxy starts before VPN is ready, so we need to wait for VPN
                    proxy_ready = "[http proxy] listening" in all_logs
                    vpn_ready = "initialization sequence completed" in all_logs or "public ip address is" in all_logs

                    if proxy_ready and vpn_ready:
                        # Give a brief moment for the proxy to fully initialize
                        time.sleep(1)
                        duration_ms = (time.time() - start_time) * 1000
                        log_event(
                            "gluetun_container_ready",
                            level="INFO",
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
                            for line in (stdout + stderr).split("\n"):
                                if error in line.lower():
                                    last_error = line.strip()
                                    break
                            if "fatal" in all_logs or "invalid credentials" in all_logs:
                                return False

            except subprocess.TimeoutExpired:
                pass

            time.sleep(2)

        if last_error:
            self._last_wait_error = last_error

        duration_ms = (time.time() - start_time) * 1000
        log_event(
            "gluetun_container_wait_timeout",
            level="ERROR",
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

    def get_container_exit_info(self, container_name: str) -> Optional[dict]:
        """Get exit information for a stopped container."""
        try:
            result = subprocess.run(
                ["docker", "inspect", container_name, "--format", "{{.State.ExitCode}}:{{.State.Error}}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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

    def get_container_logs(self, container_name: str, tail: int = 50) -> str:
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

    def verify_container(self, query_key: str, max_retries: int = 3):
        """
        Verify container's VPN IP and region using ipinfo.io lookup.

        Uses the shared get_ip_info function with a session configured to use
        the Gluetun proxy. Retries with exponential backoff if the network
        isn't ready immediately after the VPN connects.

        Args:
            query_key: The container query key (provider:region)
            max_retries: Maximum number of retry attempts (default: 3)

        Raises:
            RuntimeError: If the IP resolves to a country other than the expected one, which aborts
                at once with no further retries, or if verification still fails after every retry
        """
        start_time = time.time()

        if query_key not in self.active_containers:
            return

        container = self.active_containers[query_key]
        proxy_url = self.build_proxy_uri(container["port"])
        expected_country = container.get("country", "").upper()

        log_event(
            "gluetun_verify_start",
            level="DEBUG",
            message=f"Verifying VPN IP for: {query_key}",
            context={
                "query_key": query_key,
                "container_name": container.get("container_name"),
                "expected_country": expected_country,
                "max_retries": max_retries,
            },
        )

        last_error = None

        session = requests.Session()
        try:
            session.proxies = {"http": proxy_url, "https": proxy_url}

            for attempt in range(max_retries):
                try:
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
                                log_event(
                                    "gluetun_verify_mismatch",
                                    level="ERROR",
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

                        if query_key in self.active_containers:
                            self.active_containers[query_key]["public_ip"] = ip_info.get("ip")
                            self.active_containers[query_key]["ip_country"] = actual_country
                            self.active_containers[query_key]["ip_city"] = ip_info.get("city")
                            self.active_containers[query_key]["ip_org"] = ip_info.get("org")

                        duration_ms = (time.time() - start_time) * 1000
                        log_event(
                            "gluetun_verify_success",
                            level="INFO",
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

                    last_error = "Failed to get IP info from ipinfo.io"

                except RuntimeError:
                    raise
                except Exception as e:
                    last_error = str(e)
                    log_event(
                        "gluetun_verify_retry",
                        level="DEBUG",
                        message=f"Verification attempt {attempt + 1} failed, retrying",
                        context={
                            "query_key": query_key,
                            "attempt": attempt + 1,
                            "error": last_error,
                        },
                    )

                if attempt < max_retries - 1:
                    wait_time = 2**attempt
                    time.sleep(wait_time)
        finally:
            try:
                session.close()
            except Exception:
                pass

        duration_ms = (time.time() - start_time) * 1000
        log_event(
            "gluetun_verify_failed",
            level="ERROR",
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

    def remove_stopped_container(self, container_name: str) -> bool:
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
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )

            if result.returncode != 0 or not result.stdout.strip():
                return False

            # Parse status - format is "name:Up 2 hours" or "name:Exited (0) 2 hours ago"
            output = result.stdout.strip()
            if container_name not in output:
                return False

            if "Exited" in output or "Created" in output or "Dead" in output:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                return True

            return False

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def remove_container(self, container_name: str):
        """Stop and remove a Docker container."""
        try:
            if self.auto_cleanup:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
            else:
                subprocess.run(
                    ["docker", "stop", container_name],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                )
            except subprocess.TimeoutExpired:
                pass

    def build_proxy_uri(self, port: int) -> str:
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
