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
      nordvpn:
        vpn_type: wireguard
        credentials:
          private_key: YOUR_PRIVATE_KEY
```

### 2. Usage

Use 2-letter country codes directly:

```bash
uv run unshackle dl SERVICE CONTENT --proxy gluetun:nordvpn:us
uv run unshackle dl SERVICE CONTENT --proxy gluetun:nordvpn:uk
```

Format: `gluetun:provider:region`

## Provider Credential Requirements

Each provider has different credential requirements. See the [Gluetun Wiki](https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers) for complete details.

| Provider | VPN Type | Required Credentials |
|----------|----------|---------------------|
| NordVPN | WireGuard | `private_key` only |
| ProtonVPN | WireGuard | `private_key` only |
| Windscribe | WireGuard | `private_key`, `addresses`, `preshared_key` (all required) |
| Surfshark | WireGuard | `private_key`, `addresses` |
| Mullvad | WireGuard | `private_key`, `addresses` |
| IVPN | WireGuard | `private_key`, `addresses` |
| ExpressVPN | OpenVPN | `username`, `password` (no WireGuard support) |
| Any | OpenVPN | `username`, `password` |

### Configuration Examples

**NordVPN/ProtonVPN** (only private_key needed):
```yaml
providers:
  nordvpn:
    vpn_type: wireguard
    credentials:
      private_key: YOUR_PRIVATE_KEY
```

**Windscribe** (all three credentials required):
```yaml
providers:
  windscribe:
    vpn_type: wireguard
    credentials:
      private_key: YOUR_PRIVATE_KEY
      addresses: 10.x.x.x/32
      preshared_key: YOUR_PRESHARED_KEY  # Required, can be empty string
```

**OpenVPN** (any provider):
```yaml
providers:
  expressvpn:
    vpn_type: openvpn
    credentials:
      username: YOUR_USERNAME
      password: YOUR_PASSWORD
```

## Server Selection

Most providers use `SERVER_COUNTRIES`, but some use `SERVER_REGIONS`:

| Variable | Providers |
|----------|-----------|
| `SERVER_COUNTRIES` | NordVPN, ProtonVPN, Surfshark, Mullvad, ExpressVPN, and most others |
| `SERVER_REGIONS` | Windscribe, VyprVPN, VPN Secure |

Unshackle handles this automatically - just use 2-letter country codes.

## Global Settings

```yaml
proxy_providers:
  gluetun:
    providers: {...}
    base_port: 8888           # Starting port (default: 8888)
    auto_cleanup: true        # Remove containers on exit (default: true)
    verify_ip: true           # Verify IP matches region (default: true)
    container_prefix: "unshackle-gluetun"
    auth_user: username       # Proxy auth (optional)
    auth_password: password
```

## Features

- **Container Reuse**: First request takes 10-30s; subsequent requests are instant
- **IP Verification**: Automatically verifies VPN exit IP matches requested region
- **Concurrent Sessions**: Multiple downloads share the same container
- **Specific Servers**: Use `--proxy gluetun:nordvpn:us1239` for specific server selection

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
- Windscribe requires `preshared_key` (can be empty string)
- VPN provider server issues

## Resources

- [Gluetun Wiki](https://github.com/qdm12/gluetun-wiki) - Official provider documentation
- [Gluetun GitHub](https://github.com/qdm12/gluetun)
