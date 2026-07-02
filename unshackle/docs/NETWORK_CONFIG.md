# Network & Proxy Configuration

This document covers network and proxy configuration options for bypassing geofencing and managing connections.

## proxy_providers (dict)

Enable external proxy provider services. These proxies will be used automatically where needed as defined by the
Service's GEOFENCE class property, but can also be explicitly used with `--proxy`. You can specify which provider
to use by prefixing it with the provider key name, e.g., `--proxy basic:de` or `--proxy nordvpn:de`. Some providers
support specific query formats for selecting a country/server.

### basic (dict[str, str|list])

Define a mapping of country to proxy to use where required.
The keys are region Alpha 2 Country Codes. Alpha 2 Country Codes are `[a-z]{2}` codes, e.g., `us`, `gb`, and `jp`.
Don't get this mixed up with language codes like `en` vs. `gb`, or `ja` vs. `jp`.

Do note that each key's value can be a list of strings, or a string. For example,

```yaml
us:
  - "http://john%40email.tld:password123@proxy-us.domain.tld:8080"
  - "http://jane%40email.tld:password456@proxy-us.domain2.tld:8080"
de: "https://127.0.0.1:8080"
```

Note that if multiple proxies are defined for a region, then by default one will be randomly chosen.
You can choose a specific one by specifying it's number, e.g., `--proxy basic:us2` will choose the
second proxy of the US list.

### nordvpn (dict)

Set your NordVPN Service credentials with `username` and `password` keys to automate the use of NordVPN as a Proxy
system where required.

You can also specify specific servers to use per-region with the `server_map` key.
Sometimes a specific server works best for a service than others, so hard-coding one for a day or two helps.

You can also select servers by city using the format `--proxy nordvpn:us:seattle` or `--proxy nordvpn:ca:calgary`.

For example,

```yaml
username: zxqsR7C5CyGwmGb6KSvk8qsZ # example of the login format
password: wXVHmht22hhRKUEQ32PQVjCZ
server_map:
  us: 12 # force US server #12 for US proxies
```

The username and password should NOT be your normal NordVPN Account Credentials.
They should be the `Service credentials` which can be found on your Nord Account Dashboard.

Once set, you can also specifically opt in to use a NordVPN proxy by specifying `--proxy nordvpn:gb` or such.
You can even set a specific server number this way, e.g., `--proxy nordvpn:gb2366`.

Note that `gb` is used instead of `uk` to be more consistent across regional systems.

### protonvpn (dict)

Enable Proton VPN as a proxy provider. Proton mints short-lived HTTPS proxy credentials per session, so
authentication reuses a Proton account session (obtained via TV login or an exported browser session) rather than
static credentials. **Free Proton accounts work**, limited to the free-tier exit countries.

There are two ways to obtain that session — TV login or cookie export. You only need one. They can also coexist: a
cached or cookie session is always tried first, and TV login is only used as a fallback when no session is present.

#### TV login (self-sustaining, recommended)

Set `enable: true` and run any command that uses the proxy. On first use, when no session is cached, unshackle
prints a code and pauses:

1. Open `https://account.proton.me/vpn/tv/code` in a browser where you are signed in to Proton.
2. Enter the code shown in the terminal and approve it.
3. Return to the terminal and **press Enter** to continue.

The minted refresh token is cached at `<cache>/vpn/protonvpn.json` and **refreshes on its own forever after**, so
this is genuinely a one-time step — later runs load straight from the cache without prompting. The prompt is **off
by default** (`enable: false`) so nobody is prompted unexpectedly, and it is skipped automatically when there is no
interactive terminal (e.g. under `serve`, cron, or piped input) — in those contexts, use a cookie export or run an
interactive command once to seed the cache first.

#### Cookie export (easiest to collect, but lapses)

Export the cookies for `account.proton.me` **or** `account.protonvpn.com` (Netscape `cookies.txt` or a JSON cookie
list) and save them at `<cookies>/vpn/protonvpn.txt`. The provider auto-loads when that file exists (no YAML
required); cookies for other Proton apps (e.g. `mail.proton.me`) are ignored automatically.

- The export **must include the `AUTH-<UID>` cookie**. It is **HttpOnly** — many one-click cookie-export
  extensions skip HttpOnly cookies by default, which is the most common reason an export "doesn't work". Use an
  exporter that includes them.
- A browser session **cannot be refreshed headlessly** — Proton rotates browser refresh tokens and ties them to
  the live browser. unshackle therefore uses the cookie's access token only (it never tries to refresh it) and the
  session works until that token expires (Proton's TTL, typically ~24h); after that you must re-export. For a
  set-and-forget setup, prefer TV login.
- Cookies are read fresh from the file on every run and are **never written to the cache**. If a TV-login cache
  also exists, it is preferred unless the cookie file is newer — so to force the cookie session, just re-save
  (re-export) the cookie file.

#### Resetting

To start clean, delete both `<cookies>/vpn/protonvpn.txt` and `<cache>/vpn/protonvpn.json`.

#### Troubleshooting

- **`Proton: no session available …`** — no cookies and no cache were found. Either export cookies, or set
  `enable: true` and run from an interactive terminal (the prompt is skipped on non-tty stdin).
- **`Proton: session expired; re-export … cookies`** — a cookie session's access token lapsed and cannot be
  refreshed (expected for cookie auth). Re-export the cookies, or switch to TV login.

#### Configuration

Configuration is optional — only add a block to enable TV login or override defaults:

```yaml
proxy_providers:
  protonvpn:
    enable: true  # prompt the one-time TV login when no session is cached (default false)
    cookie_path: /path/to/protonvpn.txt  # optional; defaults to <cookies>/vpn/protonvpn.txt
    cache_path: /path/to/protonvpn.json  # optional; defaults to <cache>/vpn/protonvpn.json
    timeout: 10  # optional; per-request timeout in seconds (default 10)
    server_map:
      stream-us: us:ny  # optional alias -> country / country:city / countryNN
```

Query formats (after the provider prefix). City uses the NordVPN-style colon, like `nordvpn:us:seattle`:

- `--proxy protonvpn:us` — random server in the country (the chosen server name is logged)
- `--proxy protonvpn:us:ny` / `protonvpn:ca:vancouver` — by city (name or initials)
- `--proxy protonvpn:de203` — pin Proton server **#203** (the number shown in the log, e.g. `DE#203`)

Use `uk` (Proton's code for the United Kingdom); `gb` is accepted and mapped to `uk` automatically. Proton's
server numbers are not sequential (a country may have `#203`, `#813`, …), so pick the number from a previous
run's log line rather than guessing.

Selection only offers servers your plan can use (paid accounts are detected automatically) and **prefers paid
servers over the free pool**, falling back to free only when no paid server is available — a pinned `#NN` always
wins. **Tor and Secure Core servers are excluded** (both are slow for a plain proxy). Secure Core servers use
port 443, all others 4443 — handled automatically.

### surfsharkvpn (dict)

Enable Surfshark VPN proxy service using Surfshark Service credentials (not your login password).
You may pin specific server IDs per region using `server_map`.

You can also select servers by city using the format `--proxy surfsharkvpn:us:seattle`.

```yaml
username: your_surfshark_service_username # https://my.surfshark.com/vpn/manual-setup/main/openvpn
password: your_surfshark_service_password # service credentials, not account password
server_map:
  us: 3844 # force US server #3844
  gb: 2697 # force GB server #2697
  au: 4621 # force AU server #4621
```

### hola

Enable Hola VPN proxy service. Requires the `hola-proxy` binary to be installed and available in your PATH.
No configuration is needed under `proxy_providers`. Hola is loaded automatically when the `hola-proxy` binary
is detected.

Once available, use `--proxy hola:us` or similar to connect through Hola.

### windscribevpn (dict)

Enable Windscribe VPN proxy service using static OpenVPN service credentials.

Use the service credentials from https://windscribe.com/getconfig/openvpn (not your account login credentials).

```yaml
proxy_providers:
  windscribevpn:
    username: openvpn_username  # From https://windscribe.com/getconfig/openvpn
    password: openvpn_password  # Service credentials, NOT your account password
```

#### Server Mapping

You can optionally pin specific servers using `server_map`:

```yaml
proxy_providers:
  windscribevpn:
    username: openvpn_username
    password: openvpn_password
    server_map:
      us: us-central-096.totallyacdn.com  # Force specific US server
      gb: uk-london-001.totallyacdn.com   # Force specific UK server
```

Once configured, use `--proxy windscribevpn:us` or `--proxy windscribevpn:gb` etc. to connect through Windscribe.

You can also select specific servers by number (e.g., `--proxy windscribevpn:sg007`) or filter by city
(e.g., `--proxy windscribevpn:ca:toronto`).

### gluetun (dict)

Docker-managed VPN proxy supporting 50+ VPN providers via Gluetun. See [GLUETUN.md](GLUETUN.md) for full
configuration and usage details.

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

Usage: `--proxy gluetun:windscribe:us`

### v2ray (dict)

V2Ray / Xray local proxy provider. Spins up a local V2Ray or Xray subprocess with an
ephemeral SOCKS5 (and HTTP) inbound on `127.0.0.1`, then routes traffic through a
user-selected outbound. Supports VMess, VLESS, Trojan, and Shadowsocks outbounds, fed
from a subscription URL, a pre-built V2Ray/Xray JSON config file, or an inline list of
`vmess://` / `vless://` / `trojan://` / `ss://` URIs. See [V2RAY.md](V2RAY.md) for the
full configuration reference and protocol/query details.

```yaml
proxy_providers:
  v2ray:
    subscription_url: https://example.com/sub.yaml   # or a list of URLs
    # config_path: /path/to/v2ray.json               # alternative to subscription
    # servers:                                        # or inline URIs
    #   - vmess://...
    #   - vless://...
    server_map:
      stream-us: us:1                                # optional friendly alias
    proxy_scheme: socks5                             # socks5 | socks5h | http
    verify_ip: true                                  # confirm exit IP on startup
    auto_cleanup: true                               # kill subprocess on exit
```

Usage: `--proxy v2ray:us`, `--proxy v2ray:us:1`, `--proxy v2ray:tokyo`, `--proxy v2ray:stream-us`

---

## headers (dict)

Case-Insensitive dictionary of headers that all Services begin their Request Session state with.
All requests will use these unless changed explicitly or implicitly via a Server response.
These should be sane defaults and anything that would only be useful for some Services should not
be put here.

Avoid headers like 'Accept-Encoding' as that would be a compatibility header that the underlying
HTTP backend (rnet) will set for you as part of its browser impersonation profile.

I recommend using,

```yaml
Accept-Language: "en-US,en;q=0.8"
User-Agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
```

---

## HTTP Session Backend

unshackle uses [`rnet`](https://github.com/0x676e67/rnet) (Rust + BoringSSL) for HTTP with TLS
fingerprinting. `RnetSession` is a drop-in `requests.Session` replacement and is what
`self.session` exposes to services. It supports:

- Browser/app impersonation via named `rnet.Impersonate` presets (Chrome, Edge, Firefox, Safari,
  OkHttp, etc.) — picks JA3, ALPN, HTTP/2 SETTINGS and header order to match the chosen client.
- Native rnet proxy support (HTTP, HTTPS, SOCKS5) — used by all proxy providers below.
- Cookie-jar and `requests`-style `data=` / `json=` / `headers=` kwargs for compatibility.

The legacy `curl_cffi` backend has been removed. The config key is still spelled
`curl_impersonate` for backward compatibility, but its value now selects an rnet preset.

### curl_impersonate (dict)

```yaml
curl_impersonate:
  browser: Chrome131   # exact rnet.Impersonate preset name
```

`browser` must be an exact `rnet.Impersonate` preset name (e.g. `Chrome131`, `Chrome124`,
`Edge101`, `Firefox133`, `Safari18`, `OkHttp4_12`). See the rnet README for the full list.
Default when unset: `Chrome131`.

---
