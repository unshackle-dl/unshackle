# Remote Service Sessions

A **remote session** lets one machine do the hard, service-specific work
(authenticating to a streaming service, listing titles, resolving tracks and
manifests, and, optionally, DRM licensing) while another machine runs the actual
download, decryption, and muxing locally.

This is the mechanism behind unshackle's client/server "remote download" workflow.
A thin local client (`RemoteService`) talks to an unshackle `serve` instance over
HTTP. The server keeps an authenticated service instance alive between requests so
the client can authenticate once and then make many follow-up calls against the
same remote session.

!!! note "Why you'd use this"
    - The server has service accounts, cookies, or a CDM you do not want to copy to
      every machine.
    - You want to do downloads on a fast/local machine but keep credentials and
      licensing centralised.
    - A service is region-locked to where the server lives, and the client is not.

---

## The big picture

```
┌──────────────────────────┐        HTTP (X-Secret-Key)         ┌──────────────────────────┐
│  Local machine (client)  │  ───────────────────────────────▶ │   unshackle serve        │
│                          │                                    │                          │
│  unshackle dl --server   │   POST /api/session/create         │  authenticated Service   │
│                          │   GET  /api/session/{id}/titles    │  instance, kept alive in │
│  • track selection       │   POST /api/session/{id}/tracks    │  an in-memory session    │
│  • download / decrypt    │   POST /api/session/{id}/segments  │  store (TTL-based)       │
│  • mux                   │   POST /api/session/{id}/license   │                          │
│                          │   DELETE /api/session/{id}         │                          │
└──────────────────────────┘                                    └──────────────────────────┘
```

The client implements the same interface `dl` expects from a normal service, but
every service-facing method proxies to the server. Everything after track
selection (downloading segments, decrypting, and muxing) runs locally.

!!! note "Migration: `remote_dl` is gone"
    The `RemoteService` adapter lives inside the ordinary `dl` command and replaces
    the old standalone `remote_dl` command. If you have prior scripts or docs that
    invoke `remote_dl`, use `dl` with a configured remote server instead. The
    functionality now rides on `dl` through `RemoteService`.

For the auth-facing endpoints, header format, and error shapes referenced below,
see [Authentication](authentication.md) and the endpoint reference in
[the REST API index](index.md).

---

## Configuring a remote server (client side)

On the **client**, add a `remote_services` block to your `unshackle.yaml`. Each
entry names a server and provides its URL and API key.

```yaml title="unshackle.yaml (client)"
remote_services:
  my_server:
    url: "https://my-box.example:8786"
    api_key: "your-api-key"
    # Optional: let the server's CDM do the licensing instead of a local CDM
    server_cdm: false
    # Optional: per-service config overrides applied locally (title_map, cdm, etc.)
    services:
      EXAMPLE:
        title_map:
          "0ABCDEF": "The Show (Renamed Locally)"
```

| Key | Required | Meaning |
|---|---|---|
| `url` | yes | Base URL of the `unshackle serve` instance (no trailing slash needed) |
| `api_key` | no | Sent as the `X-Secret-Key` header on every request |
| `server_cdm` | no | When `true`, DRM keys are resolved by the server's CDM rather than a local device (default `false`) |
| `services` | no | Per-service local config overrides, keyed by service tag |

!!! warning "Port default"
    The `unshackle serve` default port is **`8786`** (not `8080`). Use the port
    your server binds. Some older example snippets show `8080`, which is
    not the default.

### Selecting a server

If you configure exactly one server, unshackle uses it implicitly:

```bash
unshackle dl EXAMPLE 0ABCDEF
```

If you configure **more than one** server, you must pick one with `--server`:

```bash
unshackle dl --server my_server EXAMPLE 0ABCDEF
```

With no `remote_services` configured at all, unshackle raises a clear error telling
you to add the block shown above.

---

## What happens during a download

From your point of view the command looks like an ordinary download. Under the
hood the client walks a remote session through its lifecycle.

=== "1. Create + authenticate"

    The client calls `POST /api/session/create`, forwarding whatever it can so the
    server does not have to prompt:

    - Local **credentials** for the service/profile (`{username, password, extra?}`)
    - Local **cookies**, compressed and base64-encoded
    - A resolved **proxy**, or, if you did not set one, your detected
      **client region**. A client that reports no region is not blocked. The
      proxy must be a full proxy URI unless the operator gives your API key
      `server_proxy`. Without `server_proxy` the server does not resolve country
      codes with its own proxy providers, and it rejects the remote session when
      it sits in a different region from the reported one, asking the client to
      pass `--proxy` with its own proxy, or `--no-proxy` to accept the server's
      own connection. With `server_proxy` the server picks a proxy that matches
      the client region itself
    - Track-selection hints (`range_`, `vcodec`, `quality`, `best_available`) so
      the server fetches the right manifests
    - Your local per-service **cache files** (e.g. refreshed tokens), but only the
      files for the active profile. The client withholds a file whose name embeds
      a hash of another credential, or the name of another profile. One profile's
      tokens therefore never reach the server while you use a different profile.
      At worst, a withheld file makes the server authenticate again

    The server uses its own accounts only when the operator lists the service in
    `serve.server_accounts` and gives your API key `server_accounts` in `serve.users`.
    For any other service, a client that sends no credentials or cookies authenticates
    with nothing. For a listed service the client sends no credentials, cookies, or
    cache at all: the server picks one of its own accounts that works in your region and
    rotates through them. `--proxy ca` picks a `ca` account.

    `GET /api/services` advertises the regions those accounts cover. When your own
    region is not one of them and you set no `--proxy`, the client resolves a proxy for
    the first advertised region itself, so both sides sit in a region the account
    works in.

    The server responds immediately with a session ID and a status. Authentication
    runs in the background on the server.

=== "2. Interactive prompts (if needed)"

    Some services need a one-time code, PIN, or device confirmation. When the
    server's auth flow asks for input, the remote session enters `pending_input`. The
    client polls, displays the prompt to you locally, collects your answer, and
    sends it back. The server's auth thread resumes with your response.

    ```text
    Enter the 6-digit code sent to your email: 483920
    ```

    A client older than the `service_params` change sends service options as
    flat body keys only. A flat key that shares a name with a transport field
    (such as `profile`, which at the top level always means the credentials
    profile) no longer reaches the service; the server logs a warning naming
    the dropped key. Update the client to send service options under
    `service_params`.

    The server also mirrors the service's own log output (`self.log`) into a
    per-session buffer. The client drains it through
    `GET /api/session/{id}/logs` after each step and prints the records
    locally, so you see the server-side reason when a step fails.

=== "3. Titles + tracks"

    Once authenticated, the client fetches the title list, then the tracks for the
    chosen title. The tracks come back with playback URLs, and the server ships
    DASH/ISM manifests as compressed XML so the client can re-parse them locally for
    downloading. Any `session_headers` / `session_cookies` the server used are
    merged into the client's local HTTP session.

    The tracks response repeats the title. A service can rewrite title fields inside
    `get_tracks`, so the client takes this copy in place of the one from the title
    list. Each track carries the full local field set. After the manifest re-parse the
    client overlays the JSON-safe part of the service-set `track.data`, so a service
    value wins over the parser's. An attachment that the service wrote to a file on the
    server travels as base64, and the client writes it to its own temp directory.

=== "4. Licensing"

    For a DRM-protected title the client either **proxies** its CDM challenge through
    the server, or, if `server_cdm: true`, asks the server to do the full CDM flow
    and return the content keys directly. See
    [Server-side vs. proxied CDM](#server-side-vs-proxied-cdm) below.

=== "5. Download + close"

    The client downloads, decrypts, and muxes locally. On completion it deletes
    the remote session. If the server has updated cache files (for example a refreshed
    token), the delete returns them and the client saves them locally, so the **next**
    remote session can skip interactive auth.

!!! tip "Renaming remote titles locally"
    You can rename titles for a remote service you do not have installed locally by
    adding a `title_map` under that service in your client `remote_services.<name>.services`
    config. The server sends raw titles. Your local map wins.

    This is deliberate: the server does **no** `title_map` remapping of its own and
    sends titles exactly as the service returns them. All remapping happens on the
    client, applied to the titles the server sends back. It keeps the final output
    name fully under the client's control and lets you rename titles even for
    services you have no local install of.

---

## Server-side vs. proxied CDM

There are two ways DRM keys get resolved, chosen by the client's `server_cdm` flag.

=== "Proxied CDM (default)"

    Your **local CDM** builds the license challenge. The client sends that challenge
    to `POST /api/session/{id}/license`, the server forwards it to the service, and
    the raw license bytes come back for your local CDM to parse.

    - Keeps your CDM local. The server only relays the license request.
    - Used when `server_cdm` is `false` (the default).

=== "Server-side CDM (server_cdm: true)"

    The **server's CDM** does everything. The client sends track IDs (or a PSSH),
    the server checks its key vaults, loads the device configured for your API key,
    runs the CDM flow, and returns `KID:KEY` pairs directly. The client needs no
    CDM.

    - Enable with `server_cdm: true` in the server entry.
    - The server tells the client which DRM type it used
      (`widevine` or `playready`).

---

## Remote session lifecycle and expiry

Remote sessions live in an **in-memory store on the server** and expire on a timer. You
generally never touch this directly, but it explains behaviour you might observe.

| Behavior | Value | Notes |
|---|---|---|
| Idle session TTL | **300s** (5 min) default | Refreshed on every request to the session |
| Max concurrent sessions | **100** default | Oldest (least recently used) is evicted when full |
| Auth/input timeout | **600s** (10 min) | Sessions still authenticating or awaiting a prompt use this longer window instead of the TTL |
| Cleanup sweep | every 60s | Expired sessions are removed and their input prompts cancelled |

!!! note "Auth is never rushed"
    A remote session that is still `authenticating` or waiting on `pending_input` is
    **not** subject to the short 300s TTL. It gets the full 600s auth window. This gives
    you time to enter an interactive code without the remote session vanishing
    mid-prompt.

The TTL and max-session limits are read from server config:

```yaml title="unshackle.yaml (server)"
serve:
  session_ttl: 300     # seconds a session may sit idle
  max_sessions: 100    # cap on concurrent sessions
```

---

## Security properties worth knowing

- **API key required.** Every remote session request carries `X-Secret-Key`. The
  server rejects requests without a valid API key (health check aside). See
  [Authentication](authentication.md).
- **IP binding.** A remote session records the creator's IP when the server makes it.
  If a later request for the same remote session comes from a different IP, the server
  returns `403 FORBIDDEN`. Remote sessions are not portable between hosts.
- **Namespaced, isolated cache.** Each remote session gets its own cache directory,
  namespaced by a hash of the API key and the remote session ID, so remote sessions
  cannot read each other's cached tokens. The server deletes the directory when the
  remote session ends. A remote session that uses a server account (`serve.server_accounts`)
  gets a persistent per-account cache instead, so the account's tokens outlive the
  remote session.
- **The server redacts secrets in logs.** It redacts remote session IDs, service tags,
  and other user values before it writes them to the log.

---

## Developer reference

!!! info "This section is for developers"
    The material below documents the server-side implementation and the HTTP
    contract. End users configuring a client can stop at the sections above.

### Remote session endpoints

The server mounts all these routes, even in `--remote-only` mode. Paths use the
`session_id` that `create` returns.

!!! note "Why `--remote-only` exists"
    `--remote-only` narrows the server to only the health, services, search, and
    remote session subset (and emits CORS headers), precisely so it is safe to sit behind
    Cloudflare or serve cross-origin browser clients. That trimmed surface, rather
    than the full `--api-only` mode, is what makes a CORS/Cloudflare-fronted
    deployment practical: reach for it when the server is public-facing or accessed
    from a browser origin, not when you want only the local HTTP API.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/session/create` | Build + authenticate a service instance; returns a session ID immediately |
| `GET` | `/api/session/{id}/titles` | Fetch and cache the title list |
| `POST` | `/api/session/{id}/tracks` | Resolve tracks, manifests, chapters, headers/cookies for a title |
| `POST` | `/api/session/{id}/segments` | Resolve per-segment/track download descriptors |
| `POST` | `/api/session/{id}/segment_filter` | Unwanted HLS segment URIs for one track (ads, bumpers) |
| `POST` | `/api/session/{id}/license` | DRM licensing (proxy or server CDM) |
| `GET` | `/api/session/{id}/logs` | Drain the service's server-side log output |
| `GET` | `/api/session/{id}/prompt` | Poll interactive auth status / pending prompt |
| `POST` | `/api/session/{id}/prompt` | Submit an answer to a pending prompt |
| `GET` | `/api/session/{id}` | Session info (validity, TTL, counts) |
| `DELETE` | `/api/session/{id}` | Close the remote session, return updated cache, clean up |

### Create: request and response

`POST /api/session/create` requires `service` and `title_id`. It also accepts
`credentials`, `cookies` (base64 of zlib-compressed Netscape cookie file), `proxy`,
`no_proxy`, `profile`, `cache` (a map of `filename → base64(zlib(bytes))`),
`client_region`, `cdm_type`, and the track-selection hints `range_`, `vcodec`,
`quality`, `best_available`, plus arbitrary service CLI options
(`additionalProperties: true`).

The response returns **before** authentication finishes:

```json
{ "session_id": "...uuid4...", "service": "EXAMPLE", "status": "authenticating" }
```

Authentication runs on a background thread (`asyncio.to_thread(authenticate, ...)`).
The remote session starts in `AUTHENTICATING`. The client must poll the prompt
endpoint until it reaches `authenticated` (or `failed`).

### Interactive auth: the `InputBridge`

When the service calls its input function during `authenticate()` on the server
thread, an `InputBridge` pauses that thread and exposes the prompt to the HTTP
layer.

- `AuthStatus` values: `authenticating`, `pending_input`, `authenticated`, `failed`.
- `InputBridge.request_input(prompt, timeout=600)` blocks the sync auth thread on a
  `threading.Event` until `submit_response()` or `cancel()` fires.
- A timeout raises `TimeoutError` and marks the remote session `FAILED`.
- `AUTH_INPUT_TIMEOUT = 600.0` seconds. This is also the TTL granted to
  `AUTHENTICATING` / `PENDING_INPUT` sessions in the store.

`GET /api/session/{id}/prompt` returns one of:

```json
{ "status": "authenticated" }
{ "status": "authenticating" }
{ "status": "pending_input", "prompt": "Enter code: " }
{ "status": "failed", "error": "...message..." }
```

A missing remote session returns `404 SESSION_NOT_FOUND`. An IP mismatch returns
`403 FORBIDDEN`. `POST /api/session/{id}/prompt` takes `{ "response": "..." }` and
returns `{ "status": "accepted" }`. A post with no pending prompt is an
`INVALID_INPUT` error.

### `SessionStore` internals

Source: `unshackle/core/api/session_store.py`. A singleton you get from
`get_session_store()`.

- **Config-driven limits.** `serve.session_ttl` (default `300`) and
  `serve.max_sessions` (default `100`) are read as properties, so config changes
  take effect without recreating the store.
- **`create()`** evicts the least-recently-accessed remote session when at capacity,
  then stores a new `SessionEntry`. It defaults `auth_status` to `AUTHENTICATED`, and
  the `create` handler overrides it to `AUTHENTICATING`.
- **`get()`** refreshes `last_accessed` through `touch()`. It returns `None` (and
  deletes the entry) if an authenticated remote session has been idle longer than the
  TTL. Remote sessions in `AUTHENTICATING` / `PENDING_INPUT` are exempt from TTL expiry.
- **`cleanup_expired()`** runs every 60s: authenticated remote sessions expire at
  `ttl`, in-flight-auth remote sessions expire at `AUTH_INPUT_TIMEOUT`. The removal of
  a remote session cancels its `InputBridge` and deletes its cache directory (pruning
  empty parent dirs up to, but not including, the cache root).
- The server calls **`cancel_all_bridges()`** on shutdown, to unblock any waiting
  auth threads.

`SessionEntry` fields:

| Field | Description |
|---|---|
| `session_id` | UUID4 string |
| `service_tag` | Normalized service tag |
| `service_instance` | The authenticated service object kept alive between calls |
| `titles` / `title_map` | Result of `get_titles()` and a `title_id → Title` map |
| `tracks` / `tracks_by_title` / `chapters_by_title` | Cached resolved tracks and chapters |
| `creator_ip` | IP recorded at create time for IP-binding checks |
| `cache_tag` | Per-session cache directory tag |
| `input_bridge` | `InputBridge` for interactive auth, if any |
| `auth_status` / `auth_error` | Current `AuthStatus` and last error message |
| `created_at` / `last_accessed` | Timestamps; `last_accessed` drives TTL and LRU eviction |

### Per-session cache namespacing

The `create` handler builds a `Cacher` namespaced as:

```text
_sessions/<pbkdf2_hmac(sha256, X-Secret-Key, "unshackle-session-ns", 100000)[:12]>/<session_id>/<service>
```

The handler writes forwarded `cache` files into that directory before authentication.
On `DELETE`, the handler harvests updated cache files (compressing each with zlib
and base64-encoding, **excluding** `titles_*` files) and returns them under a
`cache` field, so the client can keep refreshed tokens:

```json
{ "status": "ok", "cache": { "tokens": "...base64(zlib(bytes))..." } }
```

### Remote session info response

`GET /api/session/{id}`:

```json
{
  "session_id": "...",
  "service": "EXAMPLE",
  "valid": true,
  "expires_in": 300,
  "track_count": 12,
  "title_count": 40
}
```

!!! warning "`expires_in` is the configured TTL, not time remaining"
    The `expires_in` value reports the store's configured `session_ttl`, not the
    seconds left before this specific remote session expires.

### Reference client: `RemoteService`

Source: `unshackle/core/remote_service.py`. This is the canonical consumer of the
remote session API and a good template for any client.

- **`RemoteClient.request`** sets `X-Secret-Key` and `User-Agent: unshackle/<version>`,
  uses a 120s timeout for `POST` and 30s for `GET`/`DELETE`, and treats any
  `status_code >= 400` as fatal: it logs `Server error [<error_code>]: <message>`
  and raises `SystemExit(1)`.
- **Retries.** The download-side HTTP session mounts an adapter with
  `Retry(total=5, backoff_factor=0.2, status_forcelist=[429, 500, 502, 503, 504])`.
- **Flow.** `authenticate()` → `create` (+ poll `prompt` every 2s up to a 600s
  deadline, answering `pending_input` prompts. With stdin closed the client logs an
  error and raises `SystemExit(1)` instead of posting an empty answer) → `get_titles()` → `get_tracks()`
  (merging returned `session_headers`/`session_cookies`, re-parsing `manifests`) →
  get the licence in proxy mode or `server_cdm` mode → `close()` (`DELETE`, saving any returned
  `cache`).
- **Server resolution.** `resolve_server()` reads `config.remote_services.<name>`
  into `{url, api_key, services, server_cdm}`, injecting the `server_cdm` flag into
  the services map as `_server_cdm`.

---

## Troubleshooting

!!! example "\"Could not connect to remote server ... Is it running?\""
    The client could not reach the URL. Make sure that `unshackle serve` operates on
    the server, the `url`/port in `remote_services` are correct (default port `8786`),
    and any firewall or reverse proxy allows the connection.

!!! example "`403 FORBIDDEN` mid-download"
    The request came from a different IP than the one that made the remote session.
    Remote sessions are IP-bound. Do not move between networks (or NAT egress IPs) during a
    remote download.

!!! example "Auth times out or the prompt never resolves"
    Interactive auth allows up to 600s. If you miss that window, the server marks the
    remote session `failed` and cleans it up. Operate the download again. If the server
    never prompts you, make sure that the credentials and cookies the client forwarded
    are valid for the service.

!!! example "\"Multiple remote services configured. Use --server ...\""
    You have more than one entry under `remote_services`. Pass `--server <name>`
    to pick one.
