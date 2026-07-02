# V2Ray / Xray Proxy

V2Ray / Xray is a generic proxy platform that supports VMess, VLESS, Trojan, and Shadowsocks
protocols with extensive transport options (TCP, WebSocket, gRPC, HTTP/2, QUIC, HTTPUpgrade)
and TLS / XTLS-Reality security layers. The `v2ray` provider spins up a local V2Ray or Xray
subprocess with an ephemeral SOCKS5 inbound on `127.0.0.1` and routes traffic through a
user-selected outbound — letting unshackle use any V2Ray-compatible server as a proxy,
just like the existing NordVPN / ExpressVPN / Gluetun integrations.

## Prerequisites

Install **either** [Xray-core](https://github.com/XTLS/Xray-core) **or**
[V2Ray-core](https://github.com/v2fly/v2ray-core) and make sure the binary is on your
`PATH`. Xray is the modern, fully-compatible fork and is preferred when both are installed;
the original `v2ray` binary is used as a fallback.

```bash
# Xray (recommended) — install via the project's release assets or your distro's package manager
# https://github.com/XTLS/Xray-core/releases
sudo install -m 0755 xray /usr/local/bin/xray
xray version  # sanity-check

# V2Ray-core (fallback) — https://github.com/v2fly/v2ray-core/releases
sudo install -m 0755 v2ray /usr/local/bin/v2ray
v2ray version  # sanity-check
```

You can also place the binary in `unshackle/binaries/` (next to the existing
`placehere.txt`) — unshackle's binary discovery looks there before falling back to `PATH`.

To verify unshackle can see it, check the debug log on first proxy use; if neither binary
is found, the provider raises a clear `EnvironmentError` pointing at the install URLs above.

## Quick Start

### 1. Configuration

Add a `v2ray` block to `proxy_providers` in `~/.config/unshackle/unshackle.yaml`. You can
supply servers in any of three ways — the first non-empty source wins.

```yaml
proxy_providers:
  v2ray:
    subscription_url: https://your-provider.example/sub.yaml
```

Or with an inline list:

```yaml
proxy_providers:
  v2ray:
    servers:
      - vmess://eyJ2IjoiMiIsInBzIjoi8J+HuvCfh7kgVVMgLSBMb3MgQW5nZWxlcyIsImFkZCI6IjEuMi4zLjQiLCJwb3J0IjoiNDQzIiwiaWQiOiJiODMxMzgxZC02MzI0LTRkNTMtYWQ0Zi04Y2RhNDhiMzA4MTEiLCJhaWQiOjAsIm5ldCI6IndzIiwidHlwZSI6Im5vbmUiLCJob3N0IjoiZXhhbXBsZS5jb20iLCJwYXRoIjoiL3JheSIsInRscyI6InRscyIsInNuaSI6ImV4YW1wbGUuY29tIn0=
      - vless://b831381d-6324-4d53-ad4f-8cda48b30811@1.2.3.5:443?encryption=none&security=tls&type=ws&host=jp.example.com&path=/vless&sni=jp.example.com#🇯🇵 JP - Tokyo
      - trojan://secretpass@1.2.3.6:443?security=tls&type=tcp&sni=uk.example.com#🇬🇧 UK - London
      - ss://YWVzLTI1Ni1nY206c3NwYXNz@1.2.3.7:8388#🇩🇪 DE - Berlin
```

### 2. Usage

Use 2-letter country codes (auto-detected from server remarks), specific server indices,
or remark substrings — all prefixed with `v2ray:`:

```bash
unshackle dl SERVICE CONTENT --proxy v2ray:us           # any server detected as US
unshackle dl SERVICE CONTENT --proxy v2ray:us:1         # first US server (1-indexed)
unshackle dl SERVICE CONTENT --proxy v2ray:tokyo        # server whose remark contains "tokyo"
unshackle dl SERVICE CONTENT --proxy v2ray:us:tokyo     # US server whose remark contains "tokyo"
```

## Server Sources

The provider loads servers in priority order — the first non-empty source wins, but you can
mix them (e.g. fall back to a config file when the subscription is down).

### `subscription_url` (str | list[str])

A base64-encoded **or** plain-text subscription endpoint. Subscriptions commonly return
one URI per line in either form; the provider auto-detects which. Each line is parsed
independently, so a single malformed entry never aborts the whole subscription.

```yaml
proxy_providers:
  v2ray:
    subscription_url:
      - https://provider-a.example/sub.yaml
      - https://provider-b.example/sub.txt
```

Optionally cache the parsed server list so a failed subscription fetch falls back to the
last known good copy:

```yaml
proxy_providers:
  v2ray:
    subscription_url: https://your-provider.example/sub.yaml
    cache_path: ~/.cache/unshackle/v2ray-subscription.json
```

### `config_path` (str | Path)

Path to a pre-built V2Ray/Xray JSON config file. The provider walks the `outbounds` array
and extracts every entry whose protocol is `vmess`, `vless`, `trojan`, or `shadowsocks`
(`freedom`, `blackhole`, and other internal protocols are skipped). The extracted servers
take part in the same query/selection logic as subscription- or inline-defined servers, so
you can mix-and-match a hand-rolled config with a subscription.

```yaml
proxy_providers:
  v2ray:
    config_path: /etc/xray/config.json
```

### `servers` (list[str | dict])

Inline list of `vmess://` / `vless://` / `trojan://` / `ss://` URIs, or pre-parsed server
dicts (useful for programmatic setups). Mostly useful for testing or for very small static
server pools.

## Query Format

The query (the part after `v2ray:`) supports four forms, which can be combined:

| Format | Meaning | Example |
| --- | --- | --- |
| `<country>` | Any server detected as that country | `v2ray:us` |
| `<country>:<index>` | The Nth server (1-indexed) in the country pool | `v2ray:us:2` |
| `<country>:<remark>` | Country-filtered remark substring match | `v2ray:us:tokyo` |
| `<remark>` | Remark substring match across all servers | `v2ray:tokyo` |
| `<index>` | The Nth server overall (1-indexed) | `v2ray:1` |

Country codes use ISO 3166-1 alpha-2 (e.g. `us`, `gb`, `jp`, `de`). The provider normalises
`uk` → `gb` automatically. Full country names (e.g. `japan`, `germany`) are also accepted
as queries.

### Country Detection

Each parsed server gets a `country` field derived from its remark (the `ps` / `#fragment`
part of the URI). The heuristic checks, in order:

1. Flag-emoji prefixes (e.g. `🇺🇸 US - Los Angeles` → `us`)
2. Bare 2-letter codes embedded in the remark (e.g. `(DE) Berlin` → `de`)
3. Common full country names (e.g. `Japan - Tokyo` → `jp`)
4. The `uk` alias → `gb`
5. The TLD of the SNI / server hostname (e.g. `server.fr` → `fr`)

If no country is detected, the server still loads but is only reachable by remark
substring. To override the auto-detected country (or give a server a friendly alias), use
`server_map`.

### `server_map` (dict[str, str])

Optional dict mapping a query alias to either a country code, a remark substring, or a
`country:remark` / `country:index` pair. Lets you give friendly names to specific servers
without editing the subscription.

```yaml
proxy_providers:
  v2ray:
    subscription_url: https://your-provider.example/sub.yaml
    server_map:
      stream-us: us:1          # alias "stream-us" -> first US server
      home: us:los angeles     # alias "home" -> US server with "los angeles" in remark
      europe: de               # alias "europe" -> any German server
```

Use as `--proxy v2ray:stream-us`, `--proxy v2ray:home`, `--proxy v2ray:europe`.

## Provider Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `subscription_url` | str \| list[str] | — | Subscription endpoint(s). |
| `config_path` | str \| Path | — | Pre-built V2Ray/Xray JSON config file. |
| `servers` | list[str \| dict] | — | Inline list of URIs or pre-parsed server dicts. |
| `server_map` | dict[str, str] | `{}` | Query alias overrides. |
| `binary` | str \| Path | auto | Explicit path to the `xray` / `v2ray` binary. Auto-discovers via `PATH` and `unshackle/binaries/` when unset (Xray preferred). |
| `bind_host` | str | `127.0.0.1` | Host the local inbounds listen on. Proxies are never exposed publicly by default. |
| `base_port` | int | `11080` | First port to try when allocating local inbounds. Each query gets `base_port + 2N` (SOCKS) and `base_port + 2N + 1` (HTTP). |
| `proxy_scheme` | str | `socks5` | Scheme used for the returned proxy URI. One of `socks5`, `socks5h`, `http`, `https`. |
| `verify_ip` | bool | `true` | Verify the exit IP / country of the spawned proxy via `ipinfo.io` before returning it. On a country mismatch, the proxy is still returned but a warning is logged. |
| `startup_timeout` | float | `30.0` | Seconds to wait for the subprocess to start accepting connections. |
| `auto_cleanup` | bool | `true` | Kill the subprocess on object destruction / interpreter exit. |
| `cache_path` | str \| Path | — | Optional path to cache the parsed subscription server list. Used as a fallback on fetch failure. |

## Supported Protocols

All four mainstream V2Ray/Xray protocols are supported. The provider auto-detects which
parser to use based on the URI scheme.

### VMess (`vmess://`)

Base64-encoded JSON payload with the `v2rayN` shape (`v`, `ps`, `add`, `port`, `id`, `aid`,
`net`, `type`, `host`, `path`, `tls`, `sni`). Supports TCP, WebSocket, gRPC, HTTP/2, and
QUIC transports; `tls` security; and `alterId` for legacy VMess AEAD.

Example:

```
vmess://eyJ2IjoiMiIsInBzIjoi8J+HuvCfh7kgVVMgLSBMT1MtQW5nZWxlcyIsImFkZCI6IjEuMi4zLjQiLCJwb3J0IjoiNDQzIiwiaWQiOiJiODMxMzgxZC02MzI0LTRkNTMtYWQ0Zi04Y2RhNDhiMzA4MTEiLCJhaWQiOjAsIm5ldCI6IndzIiwidHlwZSI6Im5vbmUiLCJob3N0IjoiZXhhbXBsZS5jb20iLCJwYXRoIjoiL3JheSIsInRscyI6InRscyIsInNuaSI6ImV4YW1wbGUuY29tIn0=
```

### VLESS (`vless://`)

Standard VLESS URI with query-string transport / security options. Supports XTLS Vision
via the `flow` parameter, and XTLS-Reality via `security=reality` (with `pbk`, `sid`, `fp`
parameters).

```
vless://b831381d-6324-4d53-ad4f-8cda48b30811@1.2.3.4:443?encryption=none&security=tls&type=ws&host=example.com&path=/vless&sni=example.com#🇺🇸 US - LA

# XTLS Reality
vless://b831381d-6324-4d53-ad4f-8cda48b30811@1.2.3.4:443?encryption=none&security=reality&type=tcp&sni=www.google.com&pbk=ABC&sid=def&fp=chrome#Reality

# XTLS Vision flow
vless://b831381d-6324-4d53-ad4f-8cda48b30811@1.2.3.4:443?encryption=none&security=tls&type=tcp&flow=xtls-rprx-vision&sni=example.com#Vision
```

### Trojan (`trojan://`)

Standard Trojan URI with `password@host:port` form and TLS settings. Defaults to port 443
and `security=tls` when not specified.

```
trojan://secretpass@1.2.3.4:443?security=tls&type=tcp&sni=uk.example.com#🇬🇧 UK - London
```

### Shadowsocks (`ss://`)

Both SIP002 (`ss://base64(method:password)@host:port#remark`) and the legacy form
(`ss://base64(method:password@host:port)#remark`) are supported, as well as plain-text
credentials (which some servers ship).

```
# SIP002
ss://YWVzLTI1Ni1nY206c3NwYXNz@1.2.3.4:8388#🇩🇪 DE - Berlin

# Legacy
ss://YWVzLTI1Ni1nY206c3NwYXNzQDEuMi4zLjQ6ODM4OA==#🇩🇪 DE - Berlin

# Plaintext creds (not recommended but supported)
ss://aes-256-gcm:plaintextpass@1.2.3.4:8388#Plain
```

## Subprocess Lifecycle

Each unique query gets its own V2Ray/Xray subprocess on a dedicated local port pair
(SOCKS5 + HTTP). The subprocess is reused for the rest of the unshackle process — calling
`get_proxy("us")` twice returns the same URI without re-spawning. The provider:

1. Writes a minimal JSON config to a `0600` temp file under `$TMPDIR/unshackle-v2ray/`,
   containing one inbound (SOCKS5 on the allocated port) and one outbound (the selected
   server), plus a `direct` outbound for private-IP routing.
2. Spawns the binary as `[xray|v2ray, run, -c, <config_path>]` with `stdin=DEVNULL` and
   captured `stdout` / `stderr`. On Windows the subprocess is started with
   `CREATE_NO_WINDOW` so no console pops up.
3. Polls the SOCKS5 inbound until it accepts connections (up to `startup_timeout`).
4. Optionally verifies the exit IP via `ipinfo.io` (see `verify_ip`).
5. Returns `socks5://127.0.0.1:<port>` (or `http://...` if `proxy_scheme=http`).

On interpreter exit (or object destruction, when `auto_cleanup=true`), every subprocess is
terminated (`SIGTERM` → 5s grace → `SIGKILL`) and its config file is securely deleted
(overwritten with zeros then unlinked, mirroring the Gluetun provider's env-file handling)
so credentials are not left on disk.

## Troubleshooting

### "V2Ray/Xray binary not found"

Neither `xray` nor `v2ray` is on your `PATH` (or in `unshackle/binaries/`). Install one of:

- Xray: https://github.com/XTLS/Xray-core/releases
- V2Ray: https://github.com/v2fly/v2ray-core/releases

Or set `binary: /explicit/path/to/xray` in the YAML block.

### "V2Ray proxy provider has no servers configured"

None of `subscription_url`, `config_path`, or `servers` was provided (or all of them came
back empty). Check that:

- The subscription URL is reachable and returns a valid base64/plain response.
- The config file exists and has an `outbounds` array.
- The inline URIs use one of the supported schemes (`vmess://`, `vless://`, `trojan://`,
  `ss://`).

The provider logs a warning for each failed source — enable debug logging
(see [DEBUG_LOGGING.md](DEBUG_LOGGING.md)) for details.

### "subprocess did not become ready within Ns"

The V2Ray/Xray binary started but the SOCKS5 inbound didn't accept connections within
`startup_timeout` seconds (default 30s). Common causes:

- The selected server's credentials are wrong or expired (re-fetch the subscription).
- The selected server's transport doesn't match what's actually deployed (e.g. a `ws` path
  was renamed server-side).
- The binary itself crashed on startup — check the captured stderr (it's logged at WARNING
  level along with the timeout message).

Increase `startup_timeout` if the binary is just slow to initialise (e.g. on a cold
container).

### Country mismatch warning

When `verify_ip=true` (the default), the provider queries `ipinfo.io` through the spawned
proxy and compares the exit country to the server's auto-detected country. If they differ,
a warning is logged but the proxy is still returned — this is intentional, because some
CDN-fronted servers legitimately exit in a different country than their SNI suggests. To
silence the warning, set `verify_ip: false`.

### Subscription fetch failures

When a subscription fetch fails and `cache_path` is set, the provider falls back to the
last successfully cached server list (with a warning log). Without a cache, the failure
propagates and the provider starts with an empty server pool — but other sources (e.g.
`config_path`) are still loaded, so a mixed setup is resilient to a single-point
subscription outage.
