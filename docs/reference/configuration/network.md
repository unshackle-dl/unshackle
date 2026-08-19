# Network & proxy { #network-proxy }

## `network`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

TLS-fingerprinting and HTTP client settings for the rnet-based HTTP session.

| Sub-key | Type | Default | Description |
|---------|------|---------|-------------|
| `browser` | str | `"Chrome131"` | Impersonation preset. Must be an exact preset name (e.g. `Chrome131`, `Firefox135`, `Edge101`, `Safari18`, `OkHttp4_12`, `OkHttp5`, `Opera118`); unknown names raise an error. |
| `http1_only` | bool | *(unset)* | Force HTTP/1.1. |
| `http2_only` | bool | *(unset)* | Force HTTP/2. |
| `connect_timeout` | int | `10` | Connect timeout in seconds. |
| `read_timeout` | int | `30` | Read timeout in seconds (max gap between response chunks). |
| `timeout` | int | *(unset)* | Total request timeout in seconds. |
| `allow_redirects` | bool | *(unset)* | Follow redirects automatically (rnet client default when unset). |
| `pool_idle_timeout` | int | `55` | Seconds an idle pooled connection is kept (default stays under typical ~60s CDN idle kills). |
| `pool_max_idle_per_host` | int | `64` | Max idle connections kept per host. |
| `pool_max_size` | int | *(unset)* | Max total connections in the pool. |
| `tcp_keepalive` | int | `30` | TCP keepalive time in seconds. |
| `tcp_keepalive_interval` | int | *(unset)* | TCP keepalive probe interval in seconds. |
| `tcp_keepalive_retries` | int | *(unset)* | TCP keepalive probe retry count. |
| `tcp_nodelay` | bool | *(unset)* | Disable Nagle's algorithm. |

```yaml
network:
  browser: Firefox135
  http2_only: true
```

!!! note "Measured effect of `http1_only`"
    Both `http1_only` and `http2_only` are unset by default. In benchmarks, forcing HTTP/1.1
    gained 30 to 50% on hosts that throttle per-connection or stall behind HTTP/2 flow control,
    and **cost up to 27% on fast CDNs**.

!!! warning "Renamed from `curl_impersonate`"
    The old `curl_impersonate` section is a deprecated alias. If you still use it, unshackle
    honours it (only when `network` is absent) but emits a `DeprecationWarning`. Rename it to
    `network`.

## `headers`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Default HTTP headers merged into every HTTP session unshackle makes.

```yaml
headers:
  Accept-Language: en-US,en;q=0.9
```

!!! warning "Don't set `Accept-Encoding` (and similar) here"
    Compatibility headers such as `Accept-Encoding` are set by the rnet HTTP backend as part of
    its browser-impersonation profile. If you override them, you break the impersonation
    fingerprint. This block is for cross-service defaults only (for example `Accept-Language`,
    `User-Agent`). Per-service headers belong in that service's own config.

## `proxy_providers`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Proxy/VPN provider configuration. Each sub-key names a proxy provider, and unshackle passes its
block straight to that provider's constructor. See [Proxies & VPN](../../guide/proxies-and-vpn.md)
for the full proxy provider guide. Recognised proxy providers and their exit ports:

| Provider | Config key | Credentials | Proxy scheme/port |
|----------|-----------|-------------|-------------------|
| Basic (static) | `basic` | country → URI(s) | as specified |
| NordVPN | `nordvpn` | **service** credentials (48 chars combined) | `https://...:89` |
| Surfshark | `surfsharkvpn` | **service** credentials (48 chars combined) | `https://...:443` |
| Windscribe | `windscribevpn` | service credentials | `https://...:443` |
| ExpressVPN | `expressvpn` | device login (`enable: true`) / token cache | `https://cat:...@...:443` |
| ProtonVPN | `protonvpn` | TV login or exported cookies | `https://...:4443` (or `:443` Secure Core) |
| Gluetun | `gluetun` | per-VPN keys/creds | `http://localhost:{port}` (local Docker) |
| Hola | *(none, auto)* | none | `http://...:{peer}` |

```yaml
proxy_providers:
  basic:
    us: http://user:pass@1.2.3.4:8080
    de:
      - http://a.example:8080
      - socks5://b.example:1080
  nordvpn:
    username: <service username>
    password: <service password>
```

!!! note "Provider loading differs between CLI and REST server"
    The `dl` CLI loads all providers, including `windscribevpn` and `gluetun`. The REST API /
    remote-client path uses a separate resolver that does **not** load `windscribevpn` or
    `gluetun`. ExpressVPN and ProtonVPN also auto-load when their cached session exists, and Hola
    auto-loads whenever the `hola-proxy` binary is present.
