# VPN-to-HTTP Proxy Bridge for Unshackle

## Overview

This guide explains how to use **Gluetun** - a Docker-based VPN client that creates an isolated HTTP proxy from VPN services (including WireGuard). This allows Unshackle to use VPN providers like ExpressVPN, Windscribe, NordVPN, and many others without affecting your system's normal internet connection.

> **Note**: Unshackle now has **native Gluetun integration**! You can use `--proxy gluetun:windscribe:us` directly without manual Docker setup. See [CONFIG.md](../CONFIG.md#gluetun-dict) for configuration. The guide below is for advanced users who want to manage Gluetun containers manually.

## Why This Approach?

- **Network Isolation**: VPN connection runs in Docker container, doesn't affect host system
- **HTTP Proxy Interface**: Exposes standard HTTP proxy that Unshackle can use directly
- **WireGuard Support**: Modern, fast, and secure VPN protocol
- **Kill Switch**: Built-in protection prevents IP leaks if VPN disconnects
- **Multi-Provider**: Supports 50+ VPN providers out of the box
- **Cross-Platform**: Works on Linux and Windows (via Docker Desktop or WSL2)

## Supported VPN Providers

Gluetun supports many providers including:
- ExpressVPN
- Windscribe
- NordVPN
- Surfshark
- ProtonVPN
- Private Internet Access
- Mullvad
- And 50+ more

Full list: https://github.com/qdm12/gluetun/wiki

## Prerequisites

### Linux
```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
# Log out and back in for group changes to take effect
```

### Windows
- Install [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)
- Enable WSL2 backend (recommended)

## Setup Instructions

### 1. Create Gluetun Configuration Directory

```bash
mkdir -p ~/gluetun-config
cd ~/gluetun-config
```

### 2. Create Docker Compose File

Create `docker-compose.yml` with your VPN provider configuration:

#### Example: Windscribe with WireGuard

```yaml
version: "3"
services:
  gluetun:
    image: qmcgaw/gluetun:latest
    container_name: gluetun
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    ports:
      - 8888:8888/tcp   # HTTP proxy
      - 8388:8388/tcp   # Shadowsocks (optional)
      - 8388:8388/udp   # Shadowsocks (optional)
    environment:
      # VPN Provider Settings
      - VPN_SERVICE_PROVIDER=windscribe
      - VPN_TYPE=wireguard

      # Get these from your Windscribe account
      - WIREGUARD_PRIVATE_KEY=your_private_key_here
      - WIREGUARD_ADDRESSES=your_address_here
      - WIREGUARD_PRESHARED_KEY=your_preshared_key_here  # if applicable

      # Server location (optional)
      - SERVER_COUNTRIES=US
      # or specific city
      # - SERVER_CITIES=New York

      # HTTP Proxy Settings
      - HTTPPROXY=on
      - HTTPPROXY_LOG=on
      - HTTPPROXY_LISTENING_ADDRESS=:8888

      # Timezone
      - TZ=America/New_York

      # Logging
      - LOG_LEVEL=info

    restart: unless-stopped

    # Health check
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "https://api.ipify.org"]
      interval: 30s
      timeout: 10s
      retries: 3
```

#### Example: ExpressVPN with WireGuard

```yaml
version: "3"
services:
  gluetun:
    image: qmcgaw/gluetun:latest
    container_name: gluetun
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    ports:
      - 8888:8888/tcp   # HTTP proxy
    environment:
      - VPN_SERVICE_PROVIDER=expressvpn
      - VPN_TYPE=wireguard

      # Get these from ExpressVPN's WireGuard configuration
      - WIREGUARD_PRIVATE_KEY=your_private_key_here
      - WIREGUARD_ADDRESSES=your_address_here

      - HTTPPROXY=on
      - HTTPPROXY_LISTENING_ADDRESS=:8888
      - TZ=America/New_York
      - LOG_LEVEL=info

    restart: unless-stopped
```

#### Example: NordVPN with WireGuard

```yaml
version: "3"
services:
  gluetun:
    image: qmcgaw/gluetun:latest
    container_name: gluetun
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    ports:
      - 8888:8888/tcp   # HTTP proxy
    environment:
      - VPN_SERVICE_PROVIDER=nordvpn
      - VPN_TYPE=wireguard

      # NordVPN token (get from NordVPN dashboard)
      - WIREGUARD_PRIVATE_KEY=your_private_key_here
      - WIREGUARD_ADDRESSES=your_address_here

      - SERVER_COUNTRIES=US

      - HTTPPROXY=on
      - HTTPPROXY_LISTENING_ADDRESS=:8888
      - TZ=America/New_York
      - LOG_LEVEL=info

    restart: unless-stopped
```

### 3. Getting Your WireGuard Credentials

#### Windscribe
1. Log into Windscribe account
2. Go to "My Account" → "WireGuard"
3. Generate a config file for your desired location
4. Extract the private key and addresses from the config

#### ExpressVPN
1. Log into ExpressVPN
2. Navigate to the manual configuration section
3. Select WireGuard and download the configuration
4. Extract credentials from the config file

#### NordVPN
1. Log into NordVPN dashboard
2. Go to Services → NordVPN → Manual setup
3. Generate WireGuard credentials
4. Copy the private key and addresses

### 4. Start Gluetun Container

```bash
cd ~/gluetun-config
docker-compose up -d
```

Check logs to verify connection:
```bash
docker logs gluetun -f
```

You should see messages indicating successful VPN connection and HTTP proxy starting on port 8888.

### 5. Test the Proxy

```bash
# Test that the proxy works
curl -x http://localhost:8888 https://api.ipify.org

# This should show your VPN's IP address, not your real IP
```

## Integrating with Unshackle

### Option 1: Using Basic Proxy Configuration

Add to your Unshackle config (`~/.config/unshackle/config.yaml`):

```yaml
proxies:
  Basic:
    us: "http://localhost:8888"
    uk: "http://localhost:8888"  # if you have multiple Gluetun containers
```

Then use in Unshackle:
```bash
uv run unshackle dl SERVICE_NAME CONTENT_ID --proxy us
```

### Option 2: Creating Multiple VPN Proxy Containers

You can run multiple Gluetun containers for different regions:

**gluetun-us.yml:**
```yaml
version: "3"
services:
  gluetun-us:
    image: qmcgaw/gluetun:latest
    container_name: gluetun-us
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    ports:
      - 8888:8888/tcp   # HTTP proxy
    environment:
      - VPN_SERVICE_PROVIDER=windscribe
      - VPN_TYPE=wireguard
      - SERVER_COUNTRIES=US
      - WIREGUARD_PRIVATE_KEY=your_key
      - WIREGUARD_ADDRESSES=your_address
      - HTTPPROXY=on
      - HTTPPROXY_LISTENING_ADDRESS=:8888
    restart: unless-stopped
```

**gluetun-uk.yml:**
```yaml
version: "3"
services:
  gluetun-uk:
    image: qmcgaw/gluetun:latest
    container_name: gluetun-uk
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    ports:
      - 8889:8888/tcp   # Different host port
    environment:
      - VPN_SERVICE_PROVIDER=windscribe
      - VPN_TYPE=wireguard
      - SERVER_COUNTRIES=GB
      - WIREGUARD_PRIVATE_KEY=your_key
      - WIREGUARD_ADDRESSES=your_address
      - HTTPPROXY=on
      - HTTPPROXY_LISTENING_ADDRESS=:8888
    restart: unless-stopped
```

Then in Unshackle config:
```yaml
proxies:
  Basic:
    us: "http://localhost:8888"
    uk: "http://localhost:8889"
    ca: "http://localhost:8890"
```

### Option 3: Using with Authentication (Recommended for Security)

Add authentication to your Gluetun proxy:

```yaml
environment:
  - HTTPPROXY=on
  - HTTPPROXY_LISTENING_ADDRESS=:8888
  - HTTPPROXY_USER=myusername
  - HTTPPROXY_PASSWORD=mypassword
```

Then in Unshackle config:
```yaml
proxies:
  Basic:
    us: "http://myusername:mypassword@localhost:8888"
```

## Advanced Features

### Port Forwarding (for torrenting services)

Some VPN providers support port forwarding:

```yaml
environment:
  - VPN_PORT_FORWARDING=on
  - VPN_PORT_FORWARDING_LISTENING_PORT=8000
```

### SOCKS5 Proxy (Alternative to HTTP)

Gluetun also supports SOCKS5 proxy:

```yaml
ports:
  - 1080:1080/tcp   # SOCKS5 proxy
environment:
  - SHADOWSOCKS=on
  - SHADOWSOCKS_LISTENING_ADDRESS=:1080
```

### DNS Over TLS

For enhanced privacy:

```yaml
environment:
  - DOT=on
  - DOT_PROVIDERS=cloudflare
```

### Custom Firewall Rules

Block specific ports or IPs:

```yaml
environment:
  - FIREWALL_OUTBOUND_SUBNETS=192.168.1.0/24  # Allow LAN access
```

## Troubleshooting

### Container Fails to Start

Check logs:
```bash
docker logs gluetun
```

Common issues:
- Missing `NET_ADMIN` capability
- `/dev/net/tun` not available
- Invalid WireGuard credentials

### VPN Not Connecting

1. Verify credentials are correct
2. Check VPN provider status
3. Try different server location
4. Check firewall isn't blocking VPN ports

### Proxy Not Working

Test connectivity:
```bash
# Check if port is open
docker exec gluetun netstat -tlnp | grep 8888

# Test proxy directly
curl -v -x http://localhost:8888 https://api.ipify.org
```

### IP Leak Prevention

Verify your IP is hidden:
```bash
# Without proxy (should show your real IP)
curl https://api.ipify.org

# With proxy (should show VPN IP)
curl -x http://localhost:8888 https://api.ipify.org
```

### Performance Issues

- WireGuard is generally faster than OpenVPN
- Try different VPN servers closer to your location
- Check container resource limits
- Monitor with `docker stats gluetun`

## Managing Gluetun

### Start Container
```bash
docker-compose up -d
```

### Stop Container
```bash
docker-compose down
```

### Restart Container
```bash
docker-compose restart
```

### Update Gluetun
```bash
docker-compose pull
docker-compose up -d
```

### View Logs
```bash
docker logs gluetun -f
```

### Check Status
```bash
docker ps | grep gluetun
```

## Windows-Specific Notes

### Using Docker Desktop

1. Ensure WSL2 backend is enabled in Docker Desktop settings
2. Use PowerShell or WSL2 terminal for commands
3. Access proxy from Windows: `http://localhost:8888`
4. Access from WSL2: `http://host.docker.internal:8888`

### Using WSL2 Directly

If running Unshackle in WSL2:
```yaml
proxies:
  Basic:
    us: "http://localhost:8888"  # If Gluetun is in same WSL2 distro
    # or
    us: "http://host.docker.internal:8888"  # If Gluetun is in Docker Desktop
```

## Network Isolation Benefits

The Docker-based approach provides several benefits:

1. **Namespace Isolation**: VPN connection exists only in container
2. **No System Route Changes**: Host routing table remains unchanged
3. **No Connection Drops**: Host internet connection unaffected
4. **Easy Switching**: Start/stop VPN without affecting other applications
5. **Multiple Simultaneous VPNs**: Run multiple containers with different locations
6. **Kill Switch**: Automatic with container networking

## Performance Considerations

- **WireGuard**: Modern protocol, faster than OpenVPN, less CPU usage
- **Docker Overhead**: Minimal (< 5% performance impact)
- **Memory Usage**: ~50-100MB per container
- **Network Latency**: Negligible with localhost connection

## Security Considerations

1. **Enable authentication** on HTTP proxy (HTTPPROXY_USER/PASSWORD)
2. **Bind to localhost only** (don't expose 0.0.0.0 unless needed)
3. **Use Docker networks** for container-to-container communication
4. **Keep Gluetun updated** for security patches
5. **Monitor logs** for unauthorized access attempts

## References

- [Gluetun GitHub Repository](https://github.com/qdm12/gluetun)
- [Gluetun Wiki - Setup Guides](https://github.com/qdm12/gluetun/wiki)
- [Windscribe Setup Guide](https://github.com/qdm12/gluetun/wiki/Windscribe)
- [Docker Installation](https://docs.docker.com/engine/install/)

## Alternative Solutions

If Gluetun doesn't meet your needs, consider:

### 1. **Pritunl Client + Tinyproxy**
- Run Pritunl in Docker with Tinyproxy
- More complex setup but more control

### 2. **OpenConnect + Privoxy**
- For Cisco AnyConnect VPNs
- Network namespace isolation on Linux

### 3. **WireGuard + SOCKS5 Proxy**
- Manual WireGuard setup with microsocks/dante
- Maximum control but requires networking knowledge

### 4. **Network Namespaces (Linux Only)**
```bash
# Create namespace
sudo ip netns add vpn

# Setup WireGuard in namespace
sudo ip netns exec vpn wg-quick up wg0

# Run proxy in namespace
sudo ip netns exec vpn tinyproxy -d -c /etc/tinyproxy.conf
```

However, **Gluetun is recommended** for its ease of use, maintenance, and cross-platform support.

## Conclusion

Using Gluetun provides a robust, isolated, and easy-to-manage solution for connecting Unshackle to VPN services that don't offer HTTP proxies. The Docker-based approach ensures your system's network remains stable while giving you full VPN benefits for Unshackle downloads.
