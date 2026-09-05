# Proxies & VPN

Most streaming services are geo-restricted, region-priced, or refuse to hand
over a manifest from the "wrong" country. unshackle routes its traffic through a proxy
so the service sees the location you choose instead of your own. You can point it at a
single proxy URL, or configure one of several **proxy providers** (commercial VPNs and
self-hosted tunnels), then ask for a country by its two-letter code and let
unshackle pick a working server for you.

The `--proxy` flag controls proxying, and the proxy providers unshackle supports change
a query to a real proxy URL: [basic static proxies](#basic-static-proxies),
[Gluetun](#gluetun) (documented in the most detail, since it is the most flexible),
[ExpressVPN](#expressvpn), [NordVPN](#nordvpn), [Proton VPN](#proton-vpn),
[Hola](#hola), [Surfshark](#surfshark), and [Windscribe](#windscribe).

!!! note "Where proxies are configured"
    All proxy provider configuration lives under the top-level `proxy_providers:`
    config key in your `unshackle.yaml`. Each proxy provider has its own sub-key, and
    the config keys inside it map directly onto that proxy provider's settings. See the
    [Configuration file](../getting-started/configuration-file.md) page for where the
    file lives and how unshackle loads it.

## The `--proxy` flag

Three options on the `dl` command control proxying at download time:

| Option | Effect |
|---|---|
| `--proxy` | The proxy to use. Either an explicit URI, or a query that unshackle resolves against your configured providers. |
| `--no-proxy` | Force **all** proxy use off. No providers are initialised and no proxy query is resolved. |
| `--no-proxy-download` | Use the proxy for the manifest, licence, and authentication, but bypass it for the **downloads** themselves. |

```shell title="Explicit proxy URI"
unshackle dl --proxy http://user:pass@1.2.3.4:8080 EXAMPLE 81234567
```

```shell title="Resolve a US server from your configured providers"
unshackle dl --proxy us EXAMPLE 81234567
```

```shell title="Target a specific provider"
unshackle dl --proxy nordvpn:us EXAMPLE 81234567
```

!!! tip "Set a default proxy in config"
    Like every `dl` flag, you can give `--proxy` a default in your configuration, so you
    do not have to pass it on every command. See
    [Downloading](downloading.md) for how `dl` options and config defaults interact.

### `--no-proxy-download`

Segment downloads are usually the largest share of traffic, and a residential-grade VPN
proxy is frequently the slowest part of the chain. `--no-proxy-download` lets you keep the
proxy where it matters (passing the geo-check for the manifest and the licence) while
pulling the files themselves over your normal connection at full speed.

```shell title="Proxy the manifest and licence, download segments direct"
unshackle dl --proxy gb --no-proxy-download EXAMPLE 10a1234
```

!!! warning "When this is unsafe"
    Some services tie segment delivery (CDN tokens, per-segment geo-checks) to the same
    region as the manifest. On those services, bypassing the proxy for downloads will
    cause segment fetches to fail or return the wrong region. If downloads break with
    `--no-proxy-download`, remove it.

## How proxy resolution works

unshackle interprets the value you pass to `--proxy` in one of three ways. It tries
them in this order.

```mermaid
flowchart TD
    A["--proxy value"] --> B{"Looks like a URI?<br/>http:// https:// socks"}
    B -- yes --> C["Use it verbatim"]
    B -- no --> D{"Has a provider prefix?<br/>provider:query"}
    D -- yes --> E["Ask that one provider<br/>for the query"]
    D -- no --> F["Ask every provider in order,<br/>first match wins"]
```

1. **An explicit URI.** unshackle uses anything shaped like `http://...`,
   `https://...`, or a `socks...` URI exactly as you give it. unshackle logs
   `Using explicit Proxy: ...` and does no lookup.
2. **A provider-prefixed query**: `provider:query`, for example `nordvpn:us` or
   `gluetun:windscribe:us`. unshackle finds the proxy provider whose name matches
   the prefix (case-insensitive). It then asks *only* that proxy provider to find a
   proxy for the remainder. If unshackle loads no such proxy provider, or if that
   proxy provider returns no proxy, the download errors.
3. **A bare query**: a region like `us`, `gb`, `us:seattle`, or `us1234`. unshackle
   asks each loaded proxy provider **in order** and uses the first proxy any of them
   returns.

unshackle compares the query against a region grammar: a two-letter country code,
optionally followed by a server number and/or a `:city` or `-city` part. It then
lowercases the query and hands it to the proxy provider. Its section below gives the
exact forms each proxy provider accepts.

### Provider load order

When you do a download without `--no-proxy`, unshackle instantiates every configured
proxy provider once, in this fixed order. It uses that same order for bare-query
resolution:

**Basic → ExpressVPN → NordVPN → Proton VPN → Surfshark → Windscribe → Gluetun → Hola**

So if you have both Basic and NordVPN configured and use `--proxy us`, a `us` entry in
your Basic config wins, because unshackle tries Basic first. To skip ahead to a specific
proxy provider, prefix the query (`--proxy nordvpn:us`).

!!! note "Auto-loading providers"
    Most providers only load when you configure them under `proxy_providers:`. Three are
    special: **ExpressVPN** and **Proton VPN** also load automatically once their cache
    file exists on disk (even with no YAML), and **Hola** loads automatically
    whenever the `hola-proxy` binary is found on your `PATH`.

### Connection feedback

After a proxy provider resolves a query, unshackle prints a line so you can see
where you came out:

- **Gluetun** reports the verified exit IP, country, and city of the container.
- **ExpressVPN** and **Proton VPN** print a location summary such as
  `(USA - New York, #3 of 5): .214`.
- Other proxy providers log the resolved proxy URL.

## Basic (static proxies)

The `Basic` proxy provider is pure static configuration, with no accounts and no network
calls. You record proxy URLs under each country code, and unshackle serves them back
when you query that country. This is the right choice when you already have proxy endpoints (from a proxy
seller, your own servers, or a corporate gateway).

Under each two-letter country code you can put either a single proxy string or a list of
them:

```yaml title="unshackle.yaml"
proxy_providers:
  basic:
    us: http://user:pass@1.2.3.4:8080
    de:
      - http://a.example:8080
      - socks5://b.example:1080
```

**Query forms:**

| Query | Meaning |
|---|---|
| `us` | A proxy for the US. If several are listed, one is chosen at random. |
| `us2` | The **2nd** entry in the US list (1-based). |

- If a country has a single proxy string, that proxy is always returned.
- If it has a list and you give a plain country code, unshackle picks one at random.
  Append an index (`de1`, `de2`) to pin a specific entry.
- unshackle normalises each URL (a missing scheme defaults to `http`) and validates it,
  so it rejects a malformed URI up front and does not fail mid-download.

```shell
unshackle dl --proxy de2 EXAMPLE 81234567
```

## Gluetun

Gluetun is unshackle's most flexible proxy backend, and the one to reach for if you want
one configuration to cover many VPN providers. Instead of returning a remote proxy URL,
Gluetun **launches a local Docker container** running the
[`qmcgaw/gluetun`](https://github.com/qdm12/gluetun) image. That container establishes a
WireGuard or OpenVPN tunnel to your chosen VPN provider and exposes a plain HTTP proxy on
`localhost`, which unshackle then routes traffic through. When the download finishes, the
container is torn down automatically.

This means you get access to the **50+ VPN providers** that Gluetun supports, using your own
VPN subscription, through a single uniform interface, with no per-provider integration
in unshackle itself.

!!! warning "Not available with `--remote`"
    Gluetun listens on your machine, so a server on another machine cannot reach it. With
    `--remote`, unshackle stops with an error for `--proxy gluetun:...`, and for any proxy that
    resolves to `localhost` or a loopback address, such as a region query that Gluetun answers.
    Give `--remote` a proxy the server can reach, or run the service locally. A server on this
    machine (a `remote_services` URL on `127.0.0.1`) can reach a local proxy, so it is exempt.

!!! warning "Docker is required"
    Gluetun needs a working [Docker](https://docs.docker.com/get-docker/) installation on
    your `PATH`. If Docker is not found, the proxy provider fails to load with an error linking
    to the install docs. The container also needs the `NET_ADMIN` capability and access
    to `/dev/net/tun` to bring up the tunnel. unshackle requests both automatically.

### Query format

Gluetun uses a nested selector: the proxy provider prefix picks Gluetun, and the rest names the
**VPN provider** and the **region**.

```
gluetun:<vpn-provider>:<region>
```

```shell title="A Windscribe US exit via Gluetun"
unshackle dl --proxy gluetun:windscribe:us EXAMPLE 81234567
```

```shell title="A NordVPN Germany exit via Gluetun"
unshackle dl --proxy gluetun:nordvpn:de EXAMPLE 81234567
```

After unshackle strips the leading `gluetun:`, the proxy provider receives exactly
`provider:region`. It must be two colon-separated parts, and anything else is an error.
The `<vpn-provider>` must be one you have configured under `gluetun.providers`, and
unshackle normalises friendly names to Gluetun's identifiers (for example `pia` and
`privateinternetaccess` both map to `private internet access`).

### Configuration

Each VPN provider you want to use gets an entry under `providers:`, carrying its VPN type,
credentials, and how your region aliases map onto Gluetun's server selectors.

```yaml title="unshackle.yaml"
proxy_providers:
  gluetun:
    providers:
      windscribe:
        vpn_type: wireguard
        credentials:
          private_key: YOUR_WIREGUARD_PRIVATE_KEY
          addresses: YOUR_WIREGUARD_ADDRESS
          preshared_key: YOUR_PRESHARED_KEY
        server_countries:
          us: US
          uk: GB
        server_cities: {}
        server_hostnames: {}
        extra_env: {}
      nordvpn:
        vpn_type: wireguard
        credentials:
          private_key: YOUR_WIREGUARD_PRIVATE_KEY
        server_countries:
          us: US
          de: DE

    # Global settings (all optional)
    base_port: 8888
    auto_cleanup: true
    container_prefix: unshackle-gluetun
    auth_user: null
    auth_password: null
    verify_ip: true
```

**Per-provider keys:**

| Key | Purpose |
|---|---|
| `vpn_type` | `wireguard` (default) or `openvpn`. |
| `credentials` | The secrets for the tunnel; see the credential rules below. |
| `server_countries` | Maps your region alias (e.g. `us`) to Gluetun's country value (e.g. `US`). |
| `server_cities` | Optional. Maps an alias to a Gluetun city. |
| `server_hostnames` | Optional. Maps an alias directly to a specific server hostname. |
| `extra_env` | Optional. Raw environment variables merged into the container last, for any Gluetun setting not covered above. |

**Credential rules by VPN type:**

=== "WireGuard"

    - `private_key` is **always** required.
    - `surfshark`, `mullvad`, and `ivpn` also require `addresses`.
    - `windscribe` additionally requires **both** `addresses` and `preshared_key`
      (`preshared_key` may be an empty string, but the config key must be present).
    - NordVPN and Proton VPN need only `private_key`.

=== "OpenVPN"

    - Requires `username` and `password`.

!!! tip "Which `vpn_type` should I pick?"
    WireGuard is the default because it **can be faster**, but it needs the fiddlier
    credential setup above (private keys, and for some providers `addresses` and a
    `preshared_key`). OpenVPN is the **simplest** to configure (only a username and
    password) at the cost of some speed. If you start out, or if you have credential
    trouble, OpenVPN is the path of least resistance. Switch to WireGuard when you want
    the extra throughput.

**Global keys:**

| Key | Default | Purpose |
|---|---|---|
| `base_port` | `8888` | The first local port to expose proxies on. Additional containers take the next free ports. |
| `auto_cleanup` | `true` | Remove containers on exit. If `false`, containers are only stopped, not deleted. |
| `container_prefix` | `unshackle-gluetun` | Prefix for Docker container names. |
| `auth_user` / `auth_password` | `null` | Optional HTTP-proxy credentials. If set, the local proxy requires auth and the credentials are embedded in the proxy URL. |
| `verify_ip` | `true` | After connecting, verify the container's real exit IP and region. |

### Region selection

For a given query region, Gluetun resolves the server like this:

1. An explicit entry in `server_hostnames`, `server_cities`, or `server_countries` for
   that alias always wins.
2. unshackle changes a `us1239`-style query (country code + number) with no explicit
   mapping into a provider-specific server hostname (for example `us1239.nordvpn.com`
   for NordVPN, `us-1239.prod.surfshark.com` for Surfshark).
3. unshackle expands a bare two-letter code to the full country name Gluetun expects.

A few providers (Windscribe, VyprVPN, VPN Secure) select by **region** rather than
country, and unshackle converts Windscribe region names automatically. For instance `us`
becomes `US East` and `uk`/`gb` become `United Kingdom`.

### Container lifecycle

Understanding what Gluetun does behind the scenes helps when something goes wrong:

1. **Image check.** unshackle pulls `qmcgaw/gluetun:latest` if it is not already
   present.
2. **Reuse.** If a container for this exact `provider:region` already runs (from a
   concurrent unshackle process), unshackle adopts it and does not start a new one. It
   removes stopped or dead containers with the same name first.

!!! note "Expect a slow first request, then instant reuse"
    Bringing up a fresh tunnel takes roughly **10 to 30 seconds** for the first request to a
    given `provider:region`. This is the VPN handshake and readiness wait, not a hang.
    Every subsequent request that reuses that same container is effectively **instant**. A
    slow first run is normal.
3. **Port allocation.** unshackle chooses a free local port, starting from `base_port`.
4. **Start.** unshackle launches the container bound to `127.0.0.1:{port}`. It passes
   credentials through a temporary, `0600`-permission env-file (never as `-e` flags),
   and it overwrites and deletes that file immediately after.
5. **Readiness wait.** unshackle polls the container logs (up to 60 seconds) until the
   HTTP proxy listens **and** the VPN reports a completed connection. Fatal errors like
   `authentication failed` or `invalid credentials` abort early, and unshackle attaches
   recent logs to the error to help you diagnose it.
6. **IP verification.** When `verify_ip` is on, unshackle queries the container's exit IP
   and checks the country matches what you asked for, raising a *region mismatch* error if
   it does not.
7. **Proxy handed back.** You get `http://localhost:{port}` (or with auth credentials if
   configured).

!!! note "Cleanup and Ctrl+C"
    unshackle cleans up containers with an `atexit` handler on normal exit. Gluetun
    deliberately does **not** install its own signal handlers, so `Ctrl+C` continues to
    work as you expect. If something kills a run hard (for example `kill -9`), a container
    can stay behind. With `auto_cleanup: true` the next run that adopts that name still
    works, or you can remove it manually with `docker rm -f`.

### Troubleshooting

!!! example "Common Gluetun issues"
    - **`RuntimeError` about Docker not found.** Install Docker and make sure that
      `docker` is on your `PATH`.
    - **`invalid credentials` / `authentication failed` in the readiness wait.** Your
      WireGuard/OpenVPN credentials are wrong or in the wrong fields. Double-check
      `private_key`, `addresses`, and (for Windscribe) `preshared_key`.
    - **Region mismatch error.** The VPN connected but exited in a different country than
      requested. Examine your `server_countries` mapping, or set `verify_ip: false` if
      you intentionally use a nearby region.
    - **Timeout after 60 seconds.** The tunnel never came up. Inspect the container logs
      (`docker logs <container>`) for the underlying VPN error.

## ExpressVPN

ExpressVPN mirrors the ExpressVPN Android TV app: it signs in through Express's OAuth 2.0
device-login flow, obtains proxy-capable locations from Express's API, and returns an
authenticated HTTPS proxy. The device login runs once. After that, the cached refresh
token keeps you signed in headlessly.

Enable it and do a download interactively once:

```yaml title="unshackle.yaml"
proxy_providers:
  expressvpn:
    enable: true
```

With `enable: true` and an interactive terminal, unshackle prints a code and a URL to
enter it at. Once you approve it, unshackle caches the tokens and reuses them
automatically, so later runs need no terminal. ExpressVPN also **auto-loads** once its token cache exists, so you can
drop `enable` after the first login, but you can keep a YAML block to pin regions or
servers:

```yaml title="unshackle.yaml (optional)"
proxy_providers:
  expressvpn:
    region_map:
      us: ny-02       # prefer New York, server 02 for the US
      gb:             # empty = smart random location
    server_map:
      myserver: usny-newyork-2
```

**Default file location:**

| File | Default path |
|---|---|
| Token cache | `{cache}/vpn/expressvpn_tokens.json` |

Where `{cache}` is your configured cache directory.

**Query forms:**

| Query | Meaning |
|---|---|
| `us` | A smart, random US location. |
| `us-ny` | US, New York. |
| `us-ny-2` / `us-ny2` | US, New York, the 2nd server. |
| `usny-newyork-2` | A full location slug. |
| `hostname.expressprovider.com` | A direct endpoint hostname. |

The resolved proxy is an HTTPS proxy on port `443`. On success, unshackle prints a
location summary such as `(USA - New York, #3 of 5): .214`.

!!! note "Cache safety"
    unshackle writes ExpressVPN's token cache with `0600` permissions, so the refresh
    token is never briefly world-readable.

## NordVPN

NordVPN uses your **Service Credentials**, not your normal NordVPN login. Make them
from the [NordVPN dashboard](https://my.nordaccount.com/dashboard/nordvpn/) under manual
setup. They are a long random username/password pair, not your email and password.

```yaml title="unshackle.yaml"
proxy_providers:
  nordvpn:
    username: YOUR_SERVICE_USERNAME
    password: YOUR_SERVICE_PASSWORD
    server_map:                # optional: pin specific server IDs
      us: 1234
      us:seattle: 5678
```

**Required keys:** `username`, `password`. unshackle validates them up front: the
combined username and password must be exactly 48 alphanumeric characters (case-insensitive), and
the username must not contain an `@`. If validation fails, you have almost certainly used
your login credentials instead of your service credentials.

`server_map` is optional and maps a region (or `code:city`) to a pinned NordVPN server ID.
Its main use is to tune performance: for a given streaming service one specific server
often performs noticeably better than NordVPN's recommended pick. So you can hard-code
that server for a day or two as a workaround, until it degrades and you swap in another.

!!! note "Use `gb`, not `uk`, for the United Kingdom"
    Query the UK as `gb`. unshackle uses the Alpha-2 country code `gb` deliberately to stay
    consistent with its other regional/country-code systems and avoid the language-vs-
    country-code confusion that `uk` invites.

**Query forms:**

| Query | Meaning |
|---|---|
| `us` | A recommended US server. |
| `us1234` | The specific server `us1234`. |
| `us:seattle` | A recommended server in Seattle. |
| `228` | A NordVPN numeric country ID. |

The returned proxy is HTTPS on **port 89** (NordVPN disabled its plain-HTTP proxies on
port 80 in 2021).

```shell
unshackle dl --proxy nordvpn:us:seattle EXAMPLE 81234567
```

## Proton VPN

Proton VPN supports two authentication methods, and unshackle can work with either. Note
that free Proton accounts work, but Proton limits them to its free-tier exit countries.

=== "TV login (recommended)"

    Proton's TV device-login flow gives a self-sustaining, refreshable sign-in. This is
    the only method that can refresh itself headlessly, so it keeps working without
    re-exporting anything. Enable it and do a download interactively once:

    ```yaml title="unshackle.yaml"
    proxy_providers:
      protonvpn:
        enable: true
    ```

    With `enable: true` and an interactive terminal, unshackle prints a code and asks you
    to enter it at [account.proton.me/vpn/tv/code](https://account.proton.me/vpn/tv/code).
    Once you approve it, unshackle caches the sign-in and reuses it automatically.

=== "Exported cookies"

    Export your `account.proton.me` cookies (the `AUTH-<UID>` cookie) to the Proton
    cookie file. This method is **access-only**: it cannot refresh, so you must re-export
    when it expires.

    !!! warning "The `AUTH-<UID>` cookie is HttpOnly"
        This is the single most common cause of a failed Proton cookie import: the required
        `AUTH-<UID>` cookie is **HttpOnly**, and many one-click cookie-export extensions
        silently skip HttpOnly cookies by default. If your import "does not work," use an
        exporter that explicitly includes HttpOnly cookies.

    !!! note "Why this session can't refresh, and lapses after ~24h"
        Proton rotates browser refresh tokens and ties them to the live browser, so a
        headless refresh is impossible. unshackle therefore deliberately uses only the
        cookie's short-lived **access token** and never attempts a refresh. That is why
        the sign-in lapses at Proton's ~24h TTL and you must export it again. This is
        exactly why TV login is the better method for set-and-forget use.

**Default file locations:**

| File | Default path |
|---|---|
| Cookies | `{cookies}/vpn/protonvpn.txt` |
| Session cache | `{cache}/vpn/protonvpn.json` |

Proton VPN **auto-loads** if its cookie file exists. Like ExpressVPN, unshackle writes
its cache file with `0600` permissions.

!!! note "Cache vs cookie precedence"
    If both a TV-login cache and a cookie file exist, unshackle **prefers the cache**
    unless the cookie file is newer. unshackle reads cookies fresh every run and never
    writes them back to the cache. So to deliberately force use of the cookie file,
    re-save (re-export) it so its modification time is newer than the cache.

**Query forms (after the `protonvpn:` prefix):**

| Query | Meaning |
|---|---|
| `us` | A random US server (paid tiers preferred over free). |
| `us12` | Proton US server #12. |
| `us:ny` | A server in New York. |

The returned proxy is HTTPS on **port 4443** (or **port 443** for Secure Core servers).
unshackle excludes Secure Core and Tor servers from selection because they are too slow
for a plain proxy. On success, unshackle prints a summary such as `(Name - City): server-host`.

!!! tip "Proton server numbers aren't sequential"
    When pinning a server with `protonvpn:deNN`, do not guess a low number. A country may
    have non-contiguous IDs like `#203`, `#813`, and so on. Copy the number from a
    previous run's log line (e.g. `DE#203`) instead.

## Hola

Hola requires no configuration at all. It uses the
[`hola-proxy`](https://github.com/Snawoot/hola-proxy) binary, and **auto-loads** whenever
that binary is found on your `PATH`. If you query Hola but the binary is missing, the
proxy provider raises an error that tells you to install it.

```shell title="Install hola-proxy, then just query a country"
unshackle dl --proxy hola:us EXAMPLE 81234567
```

**Query forms:** a two-letter country code (e.g. `us`, `gb`). unshackle asks `hola-proxy`
for available proxies in that country and picks one at random, returning an HTTP proxy.

!!! warning "Temporary bans"
    Hola's free tier can rate-limit or temporarily ban you if queried too aggressively. If
    you see a *temporary ban detected* error, wait before retrying. Hola currently uses
    only datacenter proxies.

## Surfshark

Surfshark, like NordVPN, uses **Service Credentials** rather than your login. Make
them from the [Surfshark manual-setup page](https://my.surfshark.com/vpn/manual-setup/main/openvpn).

```yaml title="unshackle.yaml"
proxy_providers:
  surfsharkvpn:
    username: YOUR_SERVICE_USERNAME
    password: YOUR_SERVICE_PASSWORD
    server_map:                # optional
      us: 1234
```

**Required keys:** `username`, `password`, validated with the same 48-character rule as
NordVPN (combined username+password must be 48 alphanumeric characters, case-insensitive, no `@`).
`server_map` optionally pins server IDs per region.

**Query forms:**

| Query | Meaning |
|---|---|
| `us` | A random US server. |
| `us-bos` | A specific server. |
| `us:seattle` | A random server in Seattle. |

The returned proxy is HTTPS on **port 443**.

## Windscribe

Windscribe uses **Service Credentials** from its
[OpenVPN config page](https://windscribe.com/getconfig/openvpn). unshackle only makes
sure that a username and a password are present (there is no 48-character rule).

```yaml title="unshackle.yaml"
proxy_providers:
  windscribevpn:
    username: YOUR_SERVICE_USERNAME
    password: YOUR_SERVICE_PASSWORD
    server_map:                       # optional: alias -> hostname
      us: us-east.totallyacdn.com
      us:seattle: us-seattle.totallyacdn.com
```

**Required keys:** `username`, `password`. `server_map` optionally maps a region (or
`region:city`) directly to a hostname.

**Query forms:**

| Query | Meaning |
|---|---|
| `us` | A random US server. |
| `us150` / `sg007` | A specific numbered server. |
| `us:seattle` | A random server in Seattle. |

The returned proxy is HTTPS on **port 443**.

!!! warning "Windscribe is not loaded by the REST API resolver"
    The `dl` command loads the standalone Windscribe proxy provider (and Gluetun), but
    the proxy resolver that the REST API and the remote-download client use does **not**.
    If you drive downloads through the API and need Windscribe or Gluetun exits, get the
    proxy on the CLI side, or use a different proxy provider for API jobs. See the
    developer note below.

## Developer notes

!!! note "Two resolution paths (developers)"
    unshackle has **two** proxy-resolution code paths that are intentionally not
    identical:

    - `unshackle/commands/dl.py` inlines its own logic and loads **all eight** providers,
      including Windscribe and Gluetun.
    - `unshackle/core/proxies/resolve.py::initialize_proxy_providers()` is the shared
      resolver used by the REST API handlers and the remote-service client. It **omits**
      Windscribe and Gluetun.

    Both paths accept the same query grammar (a direct URI, a `provider:query`, or a bare
    country query tried against providers in order), but the set of available providers
    differs. Keep this in mind when you add a proxy provider: an entry in `dl.py` does not
    make it available over the API, and the opposite is also true.

!!! note "Implementing a new provider (developers)"
    Every proxy provider subclasses `unshackle.core.proxies.proxy.Proxy` and has three
    methods:

    - `__init__(self, **kwargs)` receives its `proxy_providers` sub-dict splatted as
      keyword arguments, so YAML keys map 1:1 to constructor parameters. Do authentication
      and catalogue pre-fetching here.
    - `__repr__(self)` is a human summary, conventionally `"{n} Countries ({m} Servers)"`.
    - `get_proxy(self, query)` returns a Requests-compatible proxy URI, or `None` only
      when the query was valid but no proxy exists. Raise on genuine errors or bad queries.

    Optionally add `get_connection_info(query)` (as Gluetun does) or
    `last_connection_display()` (as ExpressVPN and Proton VPN do) to make `dl` print a
    friendly connection line instead of the raw proxy URL.

## See also

- [Downloading](downloading.md): the full `dl` command guide, including where proxy
  flags sit in the download flow.
- [Configuration file](../getting-started/configuration-file.md): the `unshackle.yaml`
  reference, including directories for cookies and cache used by the cookie-based
  providers.
- [Configuration reference](../reference/api/config.md): the developer-facing
  configuration API, including the `proxy_providers` structure.
