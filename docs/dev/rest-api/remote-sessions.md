# Remote Service Sessions

A **remote service session** lets one machine do the hard, service-specific work
(authenticating to a streaming service, listing titles, resolving tracks and
manifests, and, optionally, DRM licensing) while another machine runs the actual
download, decryption, and muxing locally.

This is the mechanism behind unshackle's client/server "remote download" workflow.
A thin local client (`RemoteService`) talks to an unshackle `serve` instance over
HTTP. The server keeps an authenticated service instance alive between requests so
the client can authenticate once and then make many follow-up calls against the
same session.

!!! note "Why you'd use this"
    - The server has service accounts, cookies, or a CDM you don't want to copy to
      every machine.
    - You want to run downloads on a fast/local machine but keep credentials and
      licensing centralized.
    - A service is region-locked to where the server lives, and the client isn't.

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
    functionality now rides on `dl` via `RemoteService`.

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
    your server actually binds. Some older example snippets show `8080`, which is
    not the default.

### Selecting a server

If exactly one server is configured, unshackle uses it implicitly:

```bash
unshackle dl EXAMPLE 0ABCDEF
```

If **more than one** server is configured, you must pick one with `--server`:

```bash
unshackle dl --server my_server EXAMPLE 0ABCDEF
```

With no `remote_services` configured at all, unshackle raises a clear error telling
you to add the block shown above.

---

## What happens during a download

From your point of view the command looks like an ordinary download. Under the
hood the client walks a session through its lifecycle.

=== "1. Create + authenticate"

    The client calls `POST /api/session/create`, forwarding whatever it can so the
    server doesn't have to prompt:

    - Local **credentials** for the service/profile (`{username, password, extra?}`)
    - Local **cookies**, compressed and base64-encoded
    - A resolved **proxy**, or, if you didn't set one, your detected
      **client region** so the server can auto-proxy to match
    - Track-selection hints (`range_`, `vcodec`, `quality`, `best_available`) so
      the server fetches the right manifests
    - Your local per-service **cache files** (e.g. refreshed tokens)

    The server responds immediately with a session ID and a status. Authentication
    runs in the background on the server.

=== "2. Interactive prompts (if needed)"

    Some services need a one-time code, PIN, or device confirmation. When the
    server's auth flow asks for input, the session enters `pending_input`. The
    client polls, displays the prompt to you locally, collects your answer, and
    sends it back. The server's auth thread resumes with your response.

    ```text
    Enter the 6-digit code sent to your email: 483920
    ```

=== "3. Titles + tracks"

    Once authenticated, the client fetches the title list, then the tracks for the
    chosen title. The tracks come back with playback URLs, and DASH/ISM manifests
    are shipped as compressed XML so the client can re-parse them locally for
    downloading. Any `session_headers` / `session_cookies` the server used are
    merged into the client's local HTTP session.

=== "4. Licensing"

    For DRM content the client either **proxies** its CDM challenge through the
    server, or, if `server_cdm: true`, asks the server to run the full CDM flow
    and return the content keys directly. See
    [Server-side vs. proxied CDM](#server-side-vs-proxied-cdm) below.

=== "5. Download + close"

    The client downloads, decrypts, and muxes locally. On completion it deletes
    the session. If the server has updated cache files (e.g. a refreshed token),
    they're returned on delete and saved locally so the **next** session can skip
    interactive auth.

!!! tip "Renaming remote titles locally"
    You can rename titles for a remote service you don't have installed locally by
    adding a `title_map` under that service in your client `remote_services.<name>.services`
    config. The server sends raw titles; your local map wins.

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

    - Keeps your CDM local; the server just relays the license request.
    - Used when `server_cdm` is `false` (the default).

=== "Server-side CDM (server_cdm: true)"

    The **server's CDM** does everything. The client sends track IDs (or a PSSH),
    the server checks its key vaults, loads the device configured for your API key,
    runs the CDM flow, and returns `KID:KEY` pairs directly. No CDM is needed on the
    client.

    - Enable with `server_cdm: true` in the server entry.
    - The server tells the client which DRM type it actually used
      (`widevine` or `playready`).

---

## Session lifecycle and expiry

Sessions live in an **in-memory store on the server** and expire on a timer. You
generally never touch this directly, but it explains behavior you might observe.

| Behavior | Value | Notes |
|---|---|---|
| Idle session TTL | **300s** (5 min) default | Refreshed on every request to the session |
| Max concurrent sessions | **100** default | Oldest (least recently used) is evicted when full |
| Auth/input timeout | **600s** (10 min) | Sessions still authenticating or awaiting a prompt use this longer window instead of the TTL |
| Cleanup sweep | every 60s | Expired sessions are removed and their input prompts cancelled |

!!! note "Auth is never rushed"
    A session that is still `authenticating` or waiting on `pending_input` is **not**
    subject to the short 300s TTL; it gets the full 600s auth window. This gives you
    time to enter an interactive code without the session vanishing mid-prompt.

The TTL and max-session limits are read from server config:

```yaml title="unshackle.yaml (server)"
serve:
  session_ttl: 300     # seconds a session may sit idle
  max_sessions: 100    # cap on concurrent sessions
```

---

## Security properties worth knowing

- **API key required.** Every session request carries `X-Secret-Key`; requests
  without a valid key are rejected (health check aside). See
  [Authentication](authentication.md).
- **IP binding.** A session records the creator's IP at create time. If a later
  request for the same session comes from a different IP, the server returns
  `403 FORBIDDEN`. Sessions are not portable between hosts.
- **Namespaced, isolated cache.** Each session gets its own cache directory,
  namespaced by a hash of the API key and the session ID, so sessions can't read
  each other's cached tokens. The directory is deleted when the session ends.
- **Secrets are scrubbed from logs.** Session IDs, service tags, and other user
  values are sanitized before logging.

---

## Developer reference

!!! info "This section is for developers"
    The material below documents the server-side implementation and the HTTP
    contract. End users configuring a client can stop at the sections above.

### Session endpoints

All routes are exposed even in `--remote-only` server mode. Paths use the
`session_id` returned by create.

!!! note "Why `--remote-only` exists"
    `--remote-only` narrows the server to just the health, services, search, and
    session subset (and emits CORS headers) precisely so it is safe to sit behind
    Cloudflare or serve cross-origin browser clients. That trimmed surface, rather
    than the full `--api-only` mode, is what makes a CORS/Cloudflare-fronted
    deployment practical: reach for it when the server is public-facing or accessed
    from a browser origin, not when you simply want the local HTTP API.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/session/create` | Build + authenticate a service instance; returns a session ID immediately |
| `GET` | `/api/session/{id}/titles` | Fetch and cache the title list |
| `POST` | `/api/session/{id}/tracks` | Resolve tracks, manifests, chapters, headers/cookies for a title |
| `POST` | `/api/session/{id}/segments` | Resolve per-segment/track download descriptors |
| `POST` | `/api/session/{id}/license` | DRM licensing (proxy or server CDM) |
| `GET` | `/api/session/{id}/prompt` | Poll interactive auth status / pending prompt |
| `POST` | `/api/session/{id}/prompt` | Submit an answer to a pending prompt |
| `GET` | `/api/session/{id}` | Session info (validity, TTL, counts) |
| `DELETE` | `/api/session/{id}` | Close the session, return updated cache, clean up |

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
The session starts in `AUTHENTICATING`; the client must poll the prompt endpoint
until it reaches `authenticated` (or `failed`).

### Interactive auth: the `InputBridge`

When the service calls its input function during `authenticate()` on the server
thread, an `InputBridge` pauses that thread and exposes the prompt to the HTTP
layer.

- `AuthStatus` values: `authenticating`, `pending_input`, `authenticated`, `failed`.
- `InputBridge.request_input(prompt, timeout=600)` blocks the sync auth thread on a
  `threading.Event` until `submit_response()` or `cancel()` fires.
- A timeout raises `TimeoutError` and marks the session `FAILED`.
- `AUTH_INPUT_TIMEOUT = 600.0` seconds. This is also the TTL granted to
  `AUTHENTICATING` / `PENDING_INPUT` sessions in the store.

`GET /api/session/{id}/prompt` returns one of:

```json
{ "status": "authenticated" }
{ "status": "authenticating" }
{ "status": "pending_input", "prompt": "Enter code: " }
{ "status": "failed", "error": "...message..." }
```

A missing session returns `404 SESSION_NOT_FOUND`; an IP mismatch returns
`403 FORBIDDEN`. `POST /api/session/{id}/prompt` takes `{ "response": "..." }` and
returns `{ "status": "accepted" }`; posting with no pending prompt is an
`INVALID_INPUT` error.

### `SessionStore` internals

Source: `unshackle/core/api/session_store.py`. A singleton obtained via
`get_session_store()`.

- **Config-driven limits.** `serve.session_ttl` (default `300`) and
  `serve.max_sessions` (default `100`) are read as properties, so config changes
  take effect without recreating the store.
- **`create()`** evicts the least-recently-accessed session when at capacity,
  then stores a new `SessionEntry` (defaulting `auth_status` to `AUTHENTICATED`;
  the create handler overrides it to `AUTHENTICATING`).
- **`get()`** refreshes `last_accessed` via `touch()`. It returns `None` (and
  deletes the entry) if an authenticated session has been idle longer than the TTL.
  Sessions in `AUTHENTICATING` / `PENDING_INPUT` are exempt from TTL expiry.
- **`cleanup_expired()`** runs every 60s: authenticated sessions expire at `ttl`,
  in-flight-auth sessions expire at `AUTH_INPUT_TIMEOUT`. Removing a session
  cancels its `InputBridge` and deletes its cache directory (pruning empty parent
  dirs up to, but not including, the cache root).
- **`cancel_all_bridges()`** is called on server shutdown to unblock any waiting
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

The create handler builds a `Cacher` namespaced as:

```text
_sessions/<pbkdf2_hmac(sha256, X-Secret-Key, "unshackle-session-ns", 100000)[:12]>/<session_id>/<service>
```

Forwarded `cache` files are written into that directory before authentication.
On `DELETE`, the handler harvests updated cache files (compressing each with zlib
and base64-encoding, **excluding** `titles_*` files) and returns them under a
`cache` key so the client can persist refreshed tokens:

```json
{ "status": "ok", "cache": { "tokens": "...base64(zlib(bytes))..." } }
```

### Session info response

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
    seconds left before this specific session expires.

### Reference client: `RemoteService`

Source: `unshackle/core/remote_service.py`. This is the canonical consumer of the
session API and a good template for any client.

- **`RemoteClient.request`** sets `X-Secret-Key` and `User-Agent: unshackle/<version>`,
  uses a 120s timeout for `POST` and 30s for `GET`/`DELETE`, and treats any
  `status_code >= 400` as fatal: it logs `Server error [<error_code>]: <message>`
  and raises `SystemExit(1)`.
- **Retries.** The download-side HTTP session mounts an adapter with
  `Retry(total=5, backoff_factor=0.2, status_forcelist=[429, 500, 502, 503, 504])`.
- **Flow.** `authenticate()` → `create` (+ poll `prompt` every 2s up to a 600s
  deadline, answering `pending_input` prompts; with stdin closed the client logs an
  error and raises `SystemExit(1)` instead of posting an empty answer) → `get_titles()` → `get_tracks()`
  (merging returned `session_headers`/`session_cookies`, re-parsing `manifests`) →
  license via proxy or `server_cdm` → `close()` (`DELETE`, saving any returned
  `cache`).
- **Server resolution.** `resolve_server()` reads `config.remote_services.<name>`
  into `{url, api_key, services, server_cdm}`, injecting the `server_cdm` flag into
  the services map as `_server_cdm`.

---

## Troubleshooting

!!! example "\"Could not connect to remote server ... Is it running?\""
    The client couldn't reach the URL. Confirm `unshackle serve` is running on the
    server, the `url`/port in `remote_services` are correct (default port `8786`),
    and any firewall or reverse proxy allows the connection.

!!! example "`403 FORBIDDEN` mid-download"
    The request came from a different IP than the one that created the session.
    Sessions are IP-bound. Don't move between networks (or NAT egress IPs) during a
    remote download.

!!! example "Auth times out or the prompt never resolves"
    Interactive auth allows up to 600s. If you miss that window the session is
    marked `failed` and cleaned up; just re-run the download. If the server never
    prompts you, check that credentials/cookies forwarded from the client are valid
    for the service.

!!! example "\"Multiple remote services configured. Use --server ...\""
    You have more than one entry under `remote_services`. Pass `--server <name>`
    to pick one.
