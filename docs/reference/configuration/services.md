# Services & authentication { #services-auth }

## `services`

- **Type:** `dict` keyed by service tag &nbsp;·&nbsp; **Default:** `{}`

Per-service configuration, keyed by the canonical service tag (for example `EXAMPLE1`, `EXAMPLE2`,
`EXAMPLE3`). unshackle reads several well-known sub-keys out of each service's block:

- **`proxy_map`**: remaps the region query you passed to `-p/--proxy` for this service.
  A config key is that query, or `"provider:query"` when the flag also named a proxy provider. The value is
  the query asked of the proxy provider instead. It has no effect when you give `-p` a full proxy URL.
- **`title_map`**: an exact-match rename map applied to fetched titles (source name →
  desired name), so a service that names a title differently from your library still matches.
- **`dl`**: per-service download defaults, using the same keys as the global
  [`dl`](download.md#dl) block.

Individual services may read any additional keys they define. unshackle hands the merged result
(the service's own `config.yaml` with this block layered on top) to the service as `self.config`.

User- or device-specific values (API keys, account IDs, device attributes) belong in this block
rather than in the service's own `config.yaml`, which holds shared defaults. See
[creating a service](../../dev/creating-a-service.md).

```yaml
services:
  EXAMPLE1:
    proxy_map:
      us: us-nyc
      "nordvpn:ca": ca-toronto
    title_map:
      "My Show: The Movie": "My Show Presents The Movie"
```

## `credentials`

- **Type:** `dict` keyed by service tag &nbsp;·&nbsp; **Default:** `{}`

Per-service login credentials. Each value is a `username:password[:extra]` string, the same
data as a `[username, password]` (or `[username, password, extra]`) list, or a dict of profile
name to either of those. With the dict form, `-p/--profile` selects the entry, and unshackle
uses the `default` config key when you give no profile, or when the named profile is missing.
unshackle parses these into `Credential` objects. It also uses the credential's SHA-1 as an
account hash for cache keys.

```yaml
credentials:
  EXAMPLE2: user@example.com:hunter2
  EXAMPLE1:
    default: primary@example.com:pw1
    second: secondary@example.com:pw2
```

!!! tip "Cookies vs credentials"
    unshackle stores cookies as files under [`directories.cookies`](directories.md), not in this config key.
    A service's `authenticate()` accepts cookies, credentials, or both.

## `firefox_cookies`

- **Type:** `dict` keyed by service tag &nbsp;·&nbsp; **Default:** `{}`

Settings for extracting cookies directly from a local Firefox profile. A service block must give
`hosts`, a list of cookie hostnames. unshackle ignores an entry shorter than 3 characters, to
prevent a broad match dumping most of the profile. With an optional `local_storage` boolean,
unshackle also pulls matching entries from `webappsstore.sqlite`, which only services that keep
auth tokens in localStorage rather than in HTTP cookies need. Extraction is read-only.

!!! note "Firefox does not need to be closed"
    The extractor copies **both** `cookies.sqlite` **and** its WAL file into a `0700` temp
    directory, so the copy holds the writes Firefox has not yet flushed to the main DB. Extraction
    fails only if Firefox holds an exclusive write lock at the instant of the copy. The live
    profile is not modified.

!!! warning "Extraction falls back silently to file cookies"
    If extraction yields no cookies or fails for **any** reason, unshackle silently falls back
    to the normal file-based cookie path (`cookies/<SERVICE>.txt` or
    `cookies/<SERVICE>/<profile>.txt`), with no error reported.

## `remote_services`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Definitions of remote unshackle service servers, used by the `--remote` mode. Each entry is
named by you (pick it with `--server`, or do not write that flag when you configure only one) and gives
the server's `url` (required), an optional `api_key`, an optional `auth_headers` list, an
optional `server_cdm` boolean, and an optional `services` sub-dict of per-service local
overrides such as `title_map`.

Leave `server_cdm` unset and the client follows the server: it uses the server CDM for each
service the API key has it on, and the local CDM for the others. `true` asks for the server CDM
and falls back to the local CDM when the server refuses; `false` always uses the local CDM.

`auth_headers` lists extra header names to send the API key in, tried before the defaults
`X-Secret-Key` and `X-Api-Key`, which unshackle always appends as fallbacks. It sends the
first name. If the server answers `401`, it retries the same request with the next name, and
keeps the one that works for the rest of the HTTP session. Names you give keep your spelling and
are not repeated in the fallbacks, so unshackle tries
`auth_headers: ["Authorization", "x-secret-key"]` as `Authorization`, `x-secret-key`, `X-Api-Key`.

In `--remote` mode unshackle turns the server's service list into synthetic CLI
commands that operate against it, falling back to the tags in that `services` sub-dict when
unshackle cannot fetch the list. Each synthetic command carries the server-side service's options and
documentation, so `unshackle dl --remote <TAG> -h` shows the same help text as it does on the
server. Service names resolve against the server's tags and aliases, not your local services.
If the server does not offer a name to your API key, unshackle rejects the name and lists the
tags it does offer. See [remote sessions](../../dev/rest-api/remote-sessions.md) for the
full setup.

## `serve`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Configuration for the `serve` command (the built-in REST API server). The full server guide
is the [REST API](../../dev/rest-api/index.md) section. These are the config keys.

| Sub-key | Type | Default | Description |
|---------|------|---------|-------------|
| `api_secret` | str | *(unset)* | Master secret the server accepts in the `X-Secret-Key` header. Required unless you start the server with `--no-key`. |
| `users` | dict | `{}` | Per-user API keys and their allowlists (see below). |
| `tiers` | dict | `{}` | Named settings that `users` entries reference by name (see below). |
| `services` | list | *(unset)* | Global service allowlist. Omit to allow all. |
| `remote_only` | bool | `false` | Expose only the remote service session endpoints (health, services, search, session) and disable the rest of the REST API. |
| `dashboard` | dict | *(unset)* | Developer dashboard: `key` is the API key for the read-only `/api/dashboard/` endpoints (status, remote sessions, remote session logs, jobs, keys, services, health, logs, SSE events). Unset leaves those routes unregistered. See [dashboard endpoints](../../dev/rest-api/dashboard.md). |
| `session_ttl` | int (s) | `300` | Lifetime of an interactive auth session. |
| `max_sessions` | int or null | `100` | Maximum concurrent remote sessions; at the cap the server evicts the oldest. Set it to `null` or `0` for no limit, which is also what `/api/dashboard/status` then reports, so a dashboard can tell "no cap" from a cap that happens to sit at the default. |
| `history_limit` | int | `100` | How many finished jobs to retain in history. |
| `compression_level` | int | `1` | gzip level for responses. |
| `services_refresh_interval` | int (s) | `0` | How often the server pulls the git-backed service repositories in `directories.services` and hot-reloads the services that changed. `0` turns it off. A service with a running or queued job swaps to the new code as soon as its last job finishes. |
| `global_speed_limit` | str | *(unlimited)* | Server-wide download speed cap, e.g. `10M`, `1.5G` or plain bytes/sec (same format as `speed_limit`). One shared budget across all concurrent jobs; the server ignores per-job speed limits while it is set. |
| `cdm_overrides` | list or bool | *(unset)* | Allowed per-request CDM overrides: a list of permitted device names, or `true` for any. Unset rejects every override. |
| `allow_job_credentials` | bool | `false` | Permit clients to supply credentials per job. |
| `server_accounts` | dict | `{}` | Services whose remote sessions use the server's own `credentials` and cookie files instead of the client's (see below). |
| `devices` | list | *(auto)* | Widevine devices offered; auto-filled from `directories.wvds`. |
| `playready_devices` | list | *(auto)* | PlayReady devices; auto-filled from `directories.prds`. |

Each entry under `users` uses that user's API key as its name, and can set its own `services`,
`devices`, and `playready_devices` allowlists, narrowing the global ones, plus an optional
`username` used as the log label for that API key (defaults to a truncated form of the API key).
A user with no `playready_devices` config key gets no PlayReady access at all, not the global
list.

`server_cdm` decides whether the server runs the CDM licensing for that API key. Set it to
`true` to enable every service, or to a list of service tags to enable only those. It is `false`
unless the entry sets it. For a service the API key does not cover, the server tells a remote
client configured with `server_cdm: true` to license with its own local CDM instead, and a
client that asks anyway gets a `FORBIDDEN` error. Because a download job always licenses with the server's CDM,
an API key without `server_cdm` for that service also cannot submit or retry `/api/download`
jobs. Keys that have no `users` entry, such as `api_secret`, keep server CDM access.

A `tier` names an entry under `serve.tiers`, which holds the settings that several API keys
share. Today a tier carries `rate_limit`, the requests per hour that API key may make; an API
key can also set its own `rate_limit`, which wins over its tier's. An API key with neither has
no limit. Going over the limit answers `429` with a `Retry-After` header. The server keeps a
fixed window rather than a sliding one: it opens on the first request and resets an hour
later. A `tier` that names no entry under `serve.tiers`, or a `rate_limit` that is not a
positive whole number, stops the server at startup, because the alternative is an API key that
silently gets no limit. The rate limit never applies to the dashboard key, and never to
`/api/health`.

`server_accounts` lists, per service, the accounts the server lends to remote sessions. A remote
session for a listed service ignores the credentials, cookies, and cache the client sends and
uses one of the server's own profiles from `credentials`. For a service that is not listed the
server never uses its own accounts: a client that sends nothing authenticates with nothing.

```yaml
credentials:
  EXAMPLE1:
    ca_main: user1@example.com:pw1
    eu_box: user2@example.com:pw2
    shared: user3@example.com:pw3

serve:
  server_accounts:
    EXAMPLE1:
      ca_main: ca             # one country
      eu_box: [gb, fr, de]    # a list of countries
      shared: global          # any region
    EXAMPLE2: true            # every profile (or the single credential), any region
```

Each entry under a service names a profile from `credentials`, or a cookie file
`cookies/<TAG>/<profile>.txt` for a service that has no credentials. Its value is the region or
regions that account works in: a two-letter country code, a list of them, or `global`. Quote
`no` (Norway), because YAML reads the bare word as `false`.

The region of a remote session is the country the client asked for with `--proxy` (`ca`, `ca1`,
`provider:ca`), the server's own region with `--no-proxy`, or the client's own region otherwise.
A full proxy URI carries no country, so with one only a `global` account matches. The server
picks the profiles that cover that region (or are `global`) and rotates through them
round-robin, one account per remote session. With no matching account it rejects the remote
session. These services ignore the client's `-p/--profile`.

Cookies come only from the profile's own file (`cookies/EXAMPLE1/ca_main.txt`; the server does
not read a bare `cookies/EXAMPLE1.txt`), and each account keeps its own persistent cache under
`cache/_accounts/EXAMPLE1/<profile>/`, so refreshed tokens survive the remote session but never
cross accounts. `serve` refuses to start when a listed profile has neither an entry under
`credentials` nor a cookie file.
`/api/search`, `/api/list-titles` and `/api/list-tracks` use the same pool, matched on the
client's reported region, and return `FORBIDDEN` to an API key without the opt-in. Rotation is
per process and does not track which accounts are in use, so concurrent remote sessions can
share one account.

A `users` entry opts in to those accounts with `server_accounts: true` (every listed service)
or a list of service tags. It is off unless set: an API key without it authenticates with the
credentials the client sends, as for any other service, and `/api/services` does not advertise
the server's accounts to it. API keys with no `users` entry, such as `api_secret`, keep access.

`server_proxy` decides whether that API key may use the proxy providers the server has
configured. There is no implicit access: only the boolean `true` on the API key's `users` entry
grants it, and an API key without a `users` entry has no access either. Without the opt-in the
server never spends its own proxy subscriptions on a remote client: not to resolve a country code
in `proxy`, not to match the client's region, and not for a service geofence. This applies to
remote sessions, search, title lists, and `/api/download` jobs alike. Such a client must send a
full proxy URI, or `no_proxy` to accept the server's own connection. Set `server_proxy: true`
to let that API key send a country code or a `provider:country` value, and to let the server
pick a proxy for the client's region and for a service geofence. The client reports its own
region; a client that reports none is not blocked.

```yaml
serve:
  api_secret: change-me
  remote_only: true
  services: [EXAMPLE1, EXAMPLE2]
  services_refresh_interval: 3600   # pull the service repositories hourly and reload what changed
  # server-wide download defaults (same keys as the `dl:` block)
  downloads: 3
  best_available: true
  tiers:
    bot: { rate_limit: 600 }      # requests per hour, for every key on this tier
  users:
    a1b2c3d4:                     # this user's API key
      services: [EXAMPLE1]        # may only use EXAMPLE1
      tier: bot                   # 600 requests/hour; 429 with Retry-After once over
    e5f6a7b8:
      server_cdm: true            # this key may have the server do the licensing
    c9d0e1f2:
      services: [EXAMPLE1, EXAMPLE2]
      server_cdm: [EXAMPLE1]      # the server licenses only EXAMPLE1; EXAMPLE2 needs a local CDM
      server_accounts: [EXAMPLE1] # may authenticate EXAMPLE1 with one of the server's own accounts
      server_proxy: true          # this key may also use the server's proxy providers
```

!!! note "`dl` keys inside `serve`"
    Most [`dl`](download.md#dl) flag keys (`downloads`, `workers`, `best_available`, and so on)
    can be set directly inside `serve`, where they apply to every request the server handles.
    The server recognises a fixed subset of download parameters, so a few CLI-only flags are
    ignored here.
