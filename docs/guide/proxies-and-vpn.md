# Proxies & VPN

Most streaming services are geo-restricted, region-priced, or refuse to hand
over a manifest from the "wrong" country. unshackle routes its traffic through a proxy
so the service sees the location you choose instead of your own. You can point it at a
single proxy URL, or configure one of several **proxy providers** (commercial VPNs and
self-hosted tunnels), then ask for a country by its two-letter code and let
unshackle pick a working server for you.

The `--proxy` flag controls proxying, and the proxy providers unshackle supports change
a query to a real proxy URL: [basic static proxies](#basic-static-proxies),
[Control D](#control-d), [Gluetun](#gluetun) (documented in the most detail, since it is the most flexible),
[ExpressVPN](#expressvpn), [NordVPN](#nordvpn), [Proton VPN](#proton-vpn),
[Hola](#hola), [Surfshark](#surfshark), and [Windscribe](#windscribe).

!!! note "Where proxies are configured"
    All proxy provider configuration lives under the top-level `proxy_providers:`
    config key in your `unshackle.yaml`. Each proxy provider has its own sub-key, and
    the config keys inside it map directly onto that proxy provider's settings. See the
    [Configuration file](../getting-started/configuration-file.md) page for where the
    file lives and how unshackle loads it.

## The `--proxy` flag

Four options on the `dl` command control proxying at download time:

| Option | Effect |
|---|---|
| `--proxy` | The proxy to use. Either an explicit URI, or a query that unshackle resolves against your configured providers. |
| `--no-proxy` | Force **all** proxy use off. No providers are initialised and no proxy query is resolved. |
| `--no-proxy-download` | Use the proxy for the manifest, licence, and authentication, but bypass it for the **downloads** themselves. |
| `--proxy-download` | Use `--proxy` for the manifest, licence, and authentication, and a different proxy for the **downloads**. Takes the same forms as `--proxy`. |

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

### `--proxy-download`

`--proxy-download` is the middle ground: the manifest, licence, and authentication go through
`--proxy`, and the downloads go through a second proxy. Use it when one proxy passes the
geo-check and another one is faster for bulk traffic. The value takes the same forms as
`--proxy`, so a country code or `provider:region` resolves against the same providers.

```shell title="Manifest and licence over NordVPN, downloads over Windscribe"
unshackle dl --proxy nordvpn:us --proxy-download windscribevpn:us EXAMPLE 10a1234
```

`--no-proxy` and `--no-proxy-download` both override `--proxy-download`. The same
region warning applies: if the service ties segment delivery to the manifest region, keep
both proxies in that region.

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
   `Using explicit Proxy: ...` and does no lookup. A `host:port` value such as
   `localhost:8080` is also an explicit proxy.
2. **A provider-prefixed query**: `provider:query`, for example `nordvpn:us` or
   `gluetun:windscribe:us`. unshackle finds the proxy provider whose name matches
   the prefix (case-insensitive). It then asks *only* that proxy provider to find a
   proxy for the remainder. If unshackle loads no such proxy provider, or if that
   proxy provider returns no proxy, the download errors.
3. **A bare query**: a region like `us`, `gb`, or `us1234`. unshackle asks each
   loaded proxy provider **in order** and uses the first proxy any of them returns. A
   bare city query such as `us:seattle` does not work: unshackle reads `us` as a proxy
   provider name. Use the proxy provider prefix, for example `nordvpn:us:seattle`.
   If no proxy provider has a proxy for the query, unshackle stops with
   `No proxy provider had a proxy for <query>` and does not continue without a proxy.

unshackle compares the query against a region grammar: a country or location code of two
to four letters, with an optional `_code` part (`res_yyz`), then an optional server
number and/or a `:city` or `-city` part. A `host:port` value never matches. It then
lowercases the query and hands it to the proxy provider. Its section below gives the
exact forms each proxy provider accepts.

A Control D resolver, `controld://<resolver>@dns.controld.com`, is a fourth form. A
`--remote` client sends it to a server in place of a proxy URI, and the server starts a
forwarder for it. See [Control D](#control-d).

### Provider load order

When you do a download without `--no-proxy`, unshackle instantiates every configured
proxy provider once, in this fixed order. It uses that same order for bare-query
resolution:

**Basic → ExpressVPN → NordVPN → Proton VPN → Surfshark → Windscribe → Gluetun → Hola → Control D**

So if you have both Basic and NordVPN configured and use `--proxy us`, a `us` entry in
your Basic config wins, because unshackle tries Basic first. To skip ahead to a specific
proxy provider, prefix the query (`--proxy nordvpn:us`).

If a proxy provider cannot load, for example because its server list cannot be fetched
or its config is not valid, unshackle logs a warning and continues with the other proxy
providers. A bare query skips the proxy provider that did not load. A query that names it,
such as `--proxy nordvpn:us`, fails and shows the reason it did not load.

!!! note "A bare region reaches Control D last"
    unshackle asks Control D for a bare region such as `--proxy us` only when no other
    loaded proxy provider has a proxy for it. Hola loads automatically when the
    `hola-proxy` binary is on your `PATH`, and it comes before Control D. When Control D answers, it changes your Control D
    account: it points a profile at that country, and it can create an `unshackle-us`
    profile and endpoint. The geofence auto-proxy uses the same order. To use Control D
    directly, prefix the query (`--proxy controld:us`).

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

### Exit check

Before the service sends a request, unshackle looks up the proxy exit IP through the
proxy. The service stops with `Proxy check failed` in two cases:

- No IP lookup gets through the proxy. The proxy is down, or it refused the connection.
- The proxy exit IP is the same as your own IP, so traffic does not go through the proxy.

unshackle tries each geolocation API in turn, and a 429 moves on to the next one. If at
least one API answers through the proxy but none of them gives an IP, the proxy passes
the check without the IP comparison. unshackle does not compare the exit country with the region you asked for,
because geolocation data can be out of date.

Over the REST API, the server returns this failure to the client as `INVALID_PROXY`.

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

## Control D

[Control D](https://controld.com) is a DNS service, not a proxy. It answers the hostnames
it redirects with the IP of a transparent proxy in the region you chose, and that proxy
relays onwards by SNI. There is no proxy URI to hand to Requests, so this proxy provider runs a
small **forwarder on loopback**: it resolves each connection through your Control D
resolver and relays the bytes. Everything downstream (the service session, the downloads,
`--proxy-download`) sees an ordinary HTTP proxy.

unshackle picks the region itself, by pointing a profile's **default rule** at it. A profile
holds one region at a time, so unshackle gives each region in use a profile of its own,
each with its own endpoint and its own forwarder port. `--proxy controld:yyz
--proxy-download controld:yul` therefore uses two profiles, when two are free: Toronto for
the service and Montreal for the downloads. When no second profile is free, the second
region moves the first profile (see [step 5](#how-unshackle-chooses-a-profile)).

Keep both locations in one country. Some CDNs also check the country of the download IP,
and refuse a `--proxy-download` exit in another country with a 403. This applies to every
proxy provider.

`unshackle search --proxy controld:ca` works the same way as `dl`.

### What you need

- **A plan with traffic redirection.** On the personal plans that is *Full Control*;
  *Some Control* has no redirection, and every redirect fails. The 14-day trial includes
  it. See [Control D's plans](https://controld.com/plans) for current prices.
- **An API token of type *Write*.** A *Read* token cannot change a profile's default rule.
  Create it in the dashboard under *API*. If you set *Allowed IPs* on the token, include
  the address of each machine that runs unshackle with it.

```yaml title="unshackle.yaml"
proxy_providers:
  controld:
    token: api.xxxxxxxxxxxx     # a Write API token, from the Control D dashboard
```

```shell
unshackle dl --proxy controld:ca EXAMPLE 81234567
```

The token is the only required key. You do not need to create a profile or an endpoint,
or to give a `profile` and `resolver`: unshackle creates an `unshackle-<region>` profile
and endpoint on your account the first time it needs one, and uses it again on the next
run. Give `profile` and `resolver` only to use a profile that you made yourself (see
[How unshackle chooses a profile](#how-unshackle-chooses-a-profile)).

The returned proxy is HTTP on loopback,
`http://127.0.0.1:{port}`. The locations themselves are read from Control D at startup
(107 of them across 68 countries, at the time of writing), so there is nothing to list:

| Query | Meaning |
|---|---|
| `ca` | One of Control D's Canadian locations, at random. |
| `yul` | That exact location, by its code. |
| `res_yyz` | A residential location, by its code. A country pick never uses a residential location, so name it this way. |

The [configuration reference](../reference/configuration/network.md#controld) lists
every `controld` config key.

### How unshackle chooses a profile

Each region query gets a profile and an endpoint named after it: `--proxy controld:ca` uses
`unshackle-ca`. For each region, unshackle uses the first match in this list:

1. A profile that this process already pointed at that region.
2. The endpoint named `unshackle-<region>` on your account, made by an earlier run.
3. A profile from your config that this process does not use yet. These are optional:

    ```yaml title="unshackle.yaml"
    proxy_providers:
      controld:
        token: api.xxxxxxxxxxxx
        profile: abcd123        # a profile kept for unshackle, and nothing else
        resolver: efgh456       # the resolver ID of an endpoint using that profile
        profiles:               # more pairs, in the same shape
          - profile: ijkl789
            resolver: mnop012
        max_profiles: 4         # the most profiles unshackle uses, yours included (default 4)
    ```

    The resolver ID is the last part of the endpoint's DoH URL,
    `https://dns.controld.com/<resolver>`. You can also give the full URL.

4. A new profile and endpoint named `unshackle-<region>`, while your config pairs plus the
   `unshackle-*` endpoints on the account are fewer than `max_profiles`. unshackle keeps
   them for the next run. `max_profiles` limits only this step: unshackle uses every pair
   in your config, even more than `max_profiles`.
5. When no profile is free, a profile moves to the new region, with everything that still
   uses it. unshackle first moves a profile that this process has not pointed at a region,
   which another process can still use. Next, it moves the profile that this process
   pointed at a region first. An `unshackle-*` profile that moves, and its endpoint, get the
   new region's name. unshackle logs `Every Control D profile is in use; moving profile … to …`,
   and connections already open keep the old exit until they close.

Because the names live on the account, every unshackle process agrees on them: two runs,
or two jobs on a `serve` instance, that ask for `ca` both use `unshackle-ca`. Two processes
can still move a profile under each other when they run out of profiles (step 5).

unshackle changes only the profiles in your config and the profiles of `unshackle-*`
endpoints. It skips an `unshackle-*` endpoint whose profile an endpoint without that name
also uses, so unshackle never redirects a profile that serves your own devices. Leave the `unshackle-*`
profiles empty: no services, no custom rules. unshackle sets their default rule to
`REDIRECT`, and the *Services* and *Custom Rules* tabs stay at zero by design. To remove
them, delete the endpoints and profiles in the dashboard. unshackle creates new ones when
it next needs them.

!!! warning "Business accounts pay per endpoint"
    Personal plans allow an unlimited number of profiles and endpoints. Business plans bill
    each endpoint every month, so each `unshackle-<country>` endpoint adds to the bill. On a business
    account, configure your own pairs, as many as the countries you use at the same time,
    and set `max_profiles` to that number. unshackle then never creates a profile.

!!! note "Everything goes through the proxy, including the segments"
    With the default rule redirecting, the whole download leaves through Control D's
    transparent proxy. Control D serves it on a best-effort basis. Pair it with `--no-proxy-download` to
    keep the geo-check on the manifest and the licence while pulling the files over your
    own connection.

!!! note "The switch is not instant"
    unshackle drops its own DNS cache when it changes region, but Control D's edge may
    still serve the previous answer for the remainder of the record's TTL.

!!! note "Same machine, same IP, over IPv4"
    Control D authorises its proxies by source IP: the address that queried DNS is the one
    allowed to use the proxy. The forwarder resolves and connects from the same host, so
    this holds. Control D also answers a redirected name only to a query over IPv4, so the
    forwarder asks over IPv4 even on a host with IPv6, and ignores `HTTPS_PROXY` for these
    queries.

    The forwarder refuses a connection to an IP address with a `502`, because an IP address
    skips Control D's DNS and connects from your own address. A request that names a host
    by IP address fails with this proxy provider.

!!! note "With `--remote`, the server resolves"
    unshackle does not send your API token to a server. For `--proxy controld:ca` with
    `--remote`, unshackle points `unshackle-ca` at Canada with your token, then sends the
    server only that endpoint's resolver ID, as `controld://<resolver>@dns.controld.com`.
    The server runs its own forwarder with it, from its own address, and needs no Control D
    configuration. The resolver ID lets anyone who has it use that endpoint's proxy, so treat
    it as a secret: unshackle masks it in logs and in stored job parameters.

    The server must run a version of unshackle that knows `controld://`. An older server
    rejects it with `INVALID_PROXY`: `Proxy provider 'controld' not found` when your API key
    has `server_proxy`, else a message that asks for a full proxy URI. A bare region, such
    as `--proxy ca`, skips Control D with `--remote`, because the proxy it gives is on your
    machine. A service whose accounts the server manages does not accept a proxy from the
    client, and that includes a Control D resolver.

### Troubleshooting Control D

| Symptom | Cause and fix |
|---|---|
| `Proxy check failed`, because the exit IP is your own IP. | The profile does not redirect. Make sure that the plan includes traffic redirection (*Full Control*, not *Some Control*). After a region change, wait for the TTL of the previous answer to end. |
| A `502 Bad Gateway` for one host. With `-d`, the log shows `Control D returned no A record for …`. | Control D gave no IPv4 address for that host. |
| A `502 Bad Gateway` for one host only. With `-d`, the log shows `Refused …, an address that Control D cannot redirect`. | The request names the host by IP address. The forwarder refuses it. See the note on IPv4 above. |
| An HTTP `401` or `403` from `api.controld.com`. | The token is a *Read* token, or its *Allowed IPs* do not include this machine. Use a *Write* token. |
| `Control D needs both a profile and a resolver` or `Control D needs max_profiles of 1 or more` at startup. | The `controld` config is not valid. Give `profile` and `resolver` together, and a `max_profiles` of 1 or more. Every run fails until you correct it, also a run that does not use Control D. |
| `Could not read Control D's locations`. | unshackle could not get the location list. It tries again at the next Control D query. |
| A `403` for the segments only, with `--proxy-download` in another country. | The CDN checks the country of the download IP. Keep `--proxy-download` in the same country as `--proxy`. |

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
   for NordVPN). Surfshark has no numbered servers, so map a Surfshark region to a
   server name such as `us-dal.prod.surfshark.com` in `server_hostnames`.
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
| `nordvpn:us:seattle` | A recommended server in Seattle. |
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

Hola requires no configuration at all. It uses the `hola-proxy` binary, and
**auto-loads** whenever that binary is found on your `PATH`. If you query Hola but the
binary is missing, the proxy provider raises an error that tells you to install it.

The `hola-proxy` GitHub repository is no longer online, so build the binary with Go. The
Go module proxy still has the source for version 1.18.2. `go install` puts the binary in
`$(go env GOPATH)/bin`, which must be on your `PATH`.

```shell title="Build hola-proxy, then query a country"
go install github.com/Snawoot/hola-proxy@v1.18.2
unshackle dl --proxy hola:us EXAMPLE 81234567
```

**Query forms:** a two-letter country code (e.g. `us`, `gb`). unshackle asks `hola-proxy`
for available proxies in that country and picks one at random, returning an HTTP proxy.

!!! warning "Temporary bans"
    Hola's free tier can rate-limit or temporarily ban you if queried too aggressively. If
    you see a *temporary ban detected* error, wait before retrying. While a ban is active,
    `hola-proxy` does not stop, so unshackle waits 30 seconds and then reports the ban.
    Hola currently uses only datacenter proxies.

## Surfshark

Surfshark, like NordVPN, uses **Service Credentials** rather than your login. Make
them from the [Surfshark manual-setup page](https://my.surfshark.com/vpn/manual-setup/main/openvpn).

```yaml title="unshackle.yaml"
proxy_providers:
  surfsharkvpn:
    username: YOUR_SERVICE_USERNAME
    password: YOUR_SERVICE_PASSWORD
    server_map:                # optional: region -> server name
      us: us-dal
      us:seattle: us-sea
```

**Required keys:** `username`, `password`, validated with the same 48-character rule as
NordVPN (combined username+password must be 48 alphanumeric characters, case-insensitive, no `@`).
`server_map` optionally pins a server per region. Give the server name from Surfshark's
server list, for example `us-dal`. A full hostname such as `us-dal.prod.surfshark.com` also
works. Surfshark does not use numeric server IDs. unshackle ignores a numeric or empty value
and logs a warning. A `us:seattle` entry applies only to that city query, and it does not
fall back to the `us` entry.

**Query forms:**

| Query | Meaning |
|---|---|
| `surfsharkvpn:us` | A random US server. `uk` and `gb` both give a UK server. |
| `surfsharkvpn:us-bos` | A specific server from Surfshark's server list. |
| `surfsharkvpn:us:seattle` | A random server in Seattle. |

unshackle ignores case, spaces, hyphens and accents in a city name, so `us:new-york`
matches New York. A city also matches the start of a longer name: `de:frankfurt` matches
Frankfurt am Main. A bare `us` also reaches Surfshark. A bare `us-bos` can stop at a
proxy provider earlier in the order, and a bare `us:seattle` does not work, so use the
`surfsharkvpn:` prefix for those forms.

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
| `windscribevpn:us:seattle` | A random server in Seattle. |

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

    - `unshackle/commands/dl.py` inlines its own logic and loads **all nine** providers,
      including Windscribe and Gluetun.
    - `unshackle/core/proxies/resolve.py::initialize_proxy_providers()` is the shared
      resolver used by the REST API handlers and the remote-service client. It **omits**
      Windscribe and Gluetun.

    Both paths accept the same query grammar (a direct URI, a `provider:query`, a bare
    country query tried against providers in order, or a `controld://` resolver), but the
    set of available providers differs. Keep this in mind when you add a proxy provider:
    an entry in `dl.py` does not make it available over the API, and the opposite is also
    true.

    The remote-service client resolves `--proxy` with its own
    `resolve_remote_proxy_arg()`. It changes a `controld:` query to a `controld://`
    resolver for the server with `get_remote_proxy()`, and it removes Control D from a bare
    query, because a loopback forwarder is of no use to a server on another machine.

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
