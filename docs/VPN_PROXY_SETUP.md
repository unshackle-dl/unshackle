# VPN Proxy Setup for Unshackle

## Overview

Unshackle has **native Gluetun integration** that automatically creates and manages Docker containers to bridge VPN connections to HTTP proxies. Simply configure your VPN credentials in `unshackle.yaml` and use `--proxy gluetun:<provider>:<region>`.

## Why Use VPN Proxies?

- **Network Isolation**: VPN runs in a Docker container, doesn't affect your system's internet
- **Easy Switching**: Switch between regions without reconfiguring anything
- **Multiple Regions**: Use different VPN locations for different downloads

## Requirements

- Docker must be installed and running
- Verify with: `unshackle env check`

## Configuration

Add your VPN provider credentials to `unshackle.yaml`:

```yaml
gluetun:
  base_port: 8888 # Starting port for HTTP proxies
  auto_cleanup: true # Remove containers when done
  container_prefix: "unshackle-gluetun"
  verify_ip: true # Verify VPN IP matches expected region

  providers:
    windscribe:
      vpn_type: openvpn
      credentials:
        username: "YOUR_OPENVPN_USERNAME"
        password: "YOUR_OPENVPN_PASSWORD"
      server_countries:
        us: US
        uk: GB
        ca: CA

    nordvpn:
      vpn_type: openvpn
      credentials:
        username: "YOUR_SERVICE_USERNAME"
        password: "YOUR_SERVICE_PASSWORD"
      server_countries:
        us: US
        de: DE
```

## Getting Your VPN Credentials

### Windscribe

1. Go to [windscribe.com/getconfig/openvpn](https://windscribe.com/getconfig/openvpn)
2. Generate a config file for any location
3. Copy the username and password shown

> **Note**: Windscribe uses region names like "US East" instead of country codes. Unshackle automatically converts codes like `us`, `ca`, `uk` to the correct region names.

### NordVPN

1. Log into NordVPN dashboard
2. Go to Services > NordVPN > Manual setup
3. Copy your service credentials (not your account email/password)

### Other Providers

Gluetun supports 50+ VPN providers. See the [Gluetun Wiki](https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers) for provider-specific setup instructions.

## Usage

Use the `--proxy` flag with the format `gluetun:<provider>:<region>`:

```bash
# Connect via Windscribe to US
unshackle dl SERVICE CONTENT_ID --proxy gluetun:windscribe:us

# Connect via NordVPN to Germany
unshackle dl SERVICE CONTENT_ID --proxy gluetun:nordvpn:de
```

Unshackle will automatically:

1. Start a Gluetun Docker container with your credentials
2. Wait for the VPN connection to establish
3. Route your download through the VPN proxy
4. Clean up the container when done (if `auto_cleanup: true`)

## Troubleshooting

### Docker Not Running

```
Error: Docker is not running
```

Start Docker Desktop or the Docker daemon.

### Invalid Credentials

```
Error: VPN authentication failed
```

Verify your credentials are correct. Use VPN service credentials from your provider's manual setup page, not your account login.

### Container Fails to Start

Check Docker logs:

```bash
docker logs unshackle-gluetun-windscribe-us
```

### VPN Connection Timeout

If the VPN connection hangs or times out, your network may be blocking the default UDP port 1194. Try using TCP port 443:

```yaml
windscribe:
  vpn_type: openvpn
  openvpn_port: 443 # Use TCP 443 for restricted networks
  credentials:
    username: "YOUR_USERNAME"
    password: "YOUR_PASSWORD"
```

### Verify VPN Connection

The `verify_ip` option checks that your IP matches the expected region. If verification fails, try a different server location in your provider's settings.

## References

- [Gluetun GitHub](https://github.com/qdm12/gluetun)
- [Gluetun Wiki - Provider Setup](https://github.com/qdm12/gluetun-wiki)
- [CONFIG.md - Full gluetun options](../CONFIG.md#gluetun-dict)
