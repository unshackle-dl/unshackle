# Gluetun VPN Proxy

Gluetun provides Docker-managed VPN proxies supporting 50+ VPN providers.

## Prerequisites

**Docker must be installed and running.**

```bash
# Linux
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER  # Then log out/in

# Windows/Mac
# Install Docker Desktop: https://www.docker.com/products/docker-desktop/
```

## Quick Start

### 1. Configuration

Add to `~/.config/unshackle/unshackle.yaml`:

```yaml
proxy_providers:
  gluetun:
    providers:
      windscribe:
        vpn_type: openvpn
        credentials:
          username: "YOUR_OPENVPN_USERNAME"
          password: "YOUR_OPENVPN_PASSWORD"
```

### 2. Usage

Use 2-letter country codes directly:

```bash
unshackle dl SERVICE CONTENT --proxy gluetun:windscribe:us
unshackle dl SERVICE CONTENT --proxy gluetun:windscribe:uk
```

Format: `gluetun:provider:region`

## Provider Credential Requirements

**OpenVPN (Recommended)**: Most providers support OpenVPN with just `username` and `password` - the simplest setup.

**WireGuard**: Requires private keys and varies by provider. See the [Gluetun Wiki](https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers) for provider-specific requirements. Note that `vpn_type` defaults to `wireguard` if not specified.

## Getting Your Credentials

### Windscribe (OpenVPN)

1. Go to [windscribe.com/getconfig/openvpn](https://windscribe.com/getconfig/openvpn)
2. Log in with your Windscribe account
3. Select any location and click "Get Config"
4. Copy the username and password shown

### NordVPN (OpenVPN)

1. Go to [NordVPN Service Credentials](https://my.nordaccount.com/dashboard/nordvpn/manual-configuration/service-credentials/)
2. Log in with your NordVPN account
3. Generate or view your service credentials
4. Copy the username and password

> **Note**: Use service credentials, NOT your account email/password.

### WireGuard Credentials (Advanced)

WireGuard requires private keys instead of username/password. See the [Gluetun Wiki](https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers) for provider-specific WireGuard setup.

## Configuration Examples

**OpenVPN (Recommended)**

Most providers support OpenVPN with just username and password:

```yaml
providers:
  windscribe:
    vpn_type: openvpn
    credentials:
      username: YOUR_OPENVPN_USERNAME
      password: YOUR_OPENVPN_PASSWORD

  nordvpn:
    vpn_type: openvpn
    credentials:
      username: YOUR_SERVICE_USERNAME
      password: YOUR_SERVICE_PASSWORD
```

**WireGuard (Advanced)**

WireGuard can be faster but requires more complex credential setup:

```yaml
# NordVPN/ProtonVPN (only private_key needed)
providers:
  nordvpn:
    vpn_type: wireguard
    credentials:
      private_key: YOUR_PRIVATE_KEY

# Surfshark/Mullvad/IVPN (private_key AND addresses required)
  surfshark:
    vpn_type: wireguard
    credentials:
      private_key: YOUR_PRIVATE_KEY
      addresses: 10.x.x.x/32

# Windscribe (all three credentials required)
  windscribe:
    vpn_type: wireguard
    credentials:
      private_key: YOUR_PRIVATE_KEY
      addresses: 10.x.x.x/32
      preshared_key: YOUR_PRESHARED_KEY
```

## Server Selection

Most providers use `SERVER_COUNTRIES`, but some use `SERVER_REGIONS`:

| Variable | Providers |
|----------|-----------|
| `SERVER_COUNTRIES` | NordVPN, ProtonVPN, Surfshark, Mullvad, ExpressVPN, and most others |
| `SERVER_REGIONS` | Windscribe, VyprVPN, VPN Secure |

Unshackle handles this automatically - just use 2-letter country codes.

### Per-Provider Server Mapping

You can explicitly map region codes to country names, cities, or hostnames per provider:

```yaml
providers:
  nordvpn:
    vpn_type: openvpn
    credentials:
      username: YOUR_USERNAME
      password: YOUR_PASSWORD
    server_countries:
      us: "United States"
      uk: "United Kingdom"
    server_cities:
      us: "New York"
    server_hostnames:
      us: "us1239.nordvpn.com"
```

### Specific Server Selection

Use `--proxy gluetun:nordvpn:us1239` for specific server selection. Unshackle builds the hostname
automatically based on the provider (e.g., `us1239.nordvpn.com` for NordVPN).

### Extra Environment Variables

You can pass additional Gluetun environment variables per provider using `extra_env`:

```yaml
providers:
  nordvpn:
    vpn_type: openvpn
    credentials:
      username: YOUR_USERNAME
      password: YOUR_PASSWORD
    extra_env:
      LOG_LEVEL: debug
```

## Global Settings

```yaml
proxy_providers:
  gluetun:
    providers: {...}
    base_port: 8888           # Starting port (default: 8888)
    auto_cleanup: true        # Remove containers on exit (default: true)
    verify_ip: true           # Verify IP matches region (default: true)
    container_prefix: "unshackle-gluetun"  # Docker container name prefix (default: "unshackle-gluetun")
    auth_user: username       # Proxy auth (optional)
    auth_password: password   # Proxy auth (optional)
```

## Features

- **Container Reuse**: First request takes 10-30s; subsequent requests are instant. Containers from other sessions are also detected and reused.
- **IP Verification**: Automatically verifies VPN exit IP matches requested region (configurable via `verify_ip`)
- **Concurrent Sessions**: Multiple downloads share the same container
- **Specific Servers**: Use `--proxy gluetun:nordvpn:us1239` for specific server selection
- **Automatic Image Pull**: The Gluetun Docker image (`qmcgaw/gluetun:latest`) is pulled automatically on first use
- **Secure Credentials**: Credentials are passed via temporary env files (mode 0600) rather than command-line arguments

## Container Management

```bash
# View containers
docker ps | grep unshackle-gluetun

# Check logs
docker logs unshackle-gluetun-nordvpn-us

# Remove all containers
docker ps -a | grep unshackle-gluetun | awk '{print $1}' | xargs docker rm -f
```

## Troubleshooting

### Docker Permission Denied (Linux)
```bash
sudo usermod -aG docker $USER
# Then log out and log back in
```

### VPN Connection Failed
Check container logs for specific errors:
```bash
docker logs unshackle-gluetun-nordvpn-us
```

Common issues:
- Invalid/missing credentials
- Windscribe WireGuard requires `preshared_key` (can be empty string, but must be set in credentials)
- VPN provider server issues
- Container startup timeout (default 60 seconds)

## Resources

- [Gluetun Wiki](https://github.com/qdm12/gluetun-wiki) - Official provider documentation
- [Gluetun GitHub](https://github.com/qdm12/gluetun)
