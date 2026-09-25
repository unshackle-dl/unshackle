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
      the client region itself. For `--proxy controld:ca` the client points its
      Control D profile at the region and sends only the resolver, as
      `controld://<resolver>@dns.controld.com`. The server counts it as a full proxy
      URI and runs its own forwarder for it. A bare region skips Control D, because its
      proxy is a forwarder on the client. See
      [Control D](../../guide/proxies-and-vpn.md#control-d)
    - Track-selection hints (`range_`, `vcodec`, `quality`, `best_available`) so
      the server fetches the right manifests
    - Your language and audio codec selection (`-l`, `-vl`, `-al`, `-a`, `-fs`)
      in a `dl_params` object. Some services fetch one manifest
      for each audio language, and pick the languages from these values
    - Your local per-service **cache files** (e.g. refreshed tokens), including
      files in subdirectories of the service cache directory, but only the files
      for the active profile. The client withholds a file whose path, in any
      directory or file name, embeds a hash of another credential or the name of
      another profile. One profile's tokens
      therefore never reach the server while you use a different profile. At
      worst, a withheld file makes the server authenticate again

    The server uses its own accounts only when the operator lists the service in
    `serve.server_accounts` and gives your API key `server_accounts` in `serve.users`.
    For any other service, a client that sends no credentials or cookies authenticates
    with nothing. For a listed service the client sends no credentials, cookies, or
    cache at all: the server picks one of its own accounts that works in your region and
    rotates through them. `--proxy ca` picks a `ca` account.

    `GET /api/services` advertises the regions those accounts cover. When your own
    region is not one of them and you set no `--proxy`, the client resolves a proxy for
    the first advertised region itself, so both sides sit in a region the account
    works in. This is a bare region query, so it skips Control D. A server-account
    service does not accept a `controld://` resolver from the client.

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
    downloading. A service can build an AdaptationSet of its own that the served
    manifest does not contain. Each such track gets a one-AdaptationSet MPD in
    `track_manifests`, and the client re-parses that instead. Any
    `session_headers` / `session_cookies` the server used are merged into the
    client's local HTTP session.

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

    The client downloads, decrypts, and muxes locally. While it works, it sends a
    keep-alive request to the remote session at an interval of one third of
    `session_ttl`, so a long download does not let the remote session expire. On
    completion, and also when `dl` exits early (`--list`, Ctrl+C, an error), it
    deletes the remote session. If the login belongs to the client, the `DELETE`
    request returns the updated cache files (for example a refreshed token) and the
    client saves them locally, so the **next** remote session can skip interactive
    authentication. The login belongs to the client when the client sent
    cookies, credentials, or cache files, or answered a login prompt (a device code
    or an OTP) that led to a successful login. A server-account login never does,
    and neither does a remote session on the server's device (see below).
    The client saves only the returned files that pass the same profile check it
    applies before it sends them, so a file for another profile cannot overwrite
    that profile's local file.

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
    - The service on the server sees your device, not the server's: its DRM system,
      security level, system ID and device type. A CDM call that the service makes
      (for example a session-key exchange at login) goes to your client as a `cdm_call`,
      and your local CDM answers it. See [Your own device identity](#your-own-device-identity).

=== "Server-side CDM (server_cdm: true)"

    The **server's CDM** does everything. The client sends track IDs (or a PSSH),
    the server checks its key vaults, loads the device configured for your API key,
    runs the CDM flow, and returns `KID:KEY` pairs directly. The client needs no
    CDM.

    - Enable with `server_cdm: true` in the server entry.
    - The server tells the client which DRM type it used
      (`widevine` or `playready`).
    - A content key the server took from its own vault is unproven. The response
      lists its KID in `vault_keys`, the client decrypts and checks that the output
      decodes, and a wrong pair goes back to `POST /api/session/{id}/keys/bad`. The
      server flags the pair in every local vault it holds, reports it to the vault
      that served it, and the next licence for that KID skips the pair and reaches
      the CDM. A server with no local SQLite vault cannot store the flag. The
      server accepts a report only for a pair it served to that remote session. The
      client never learns the server vault names.
    - A content key in the client's own vaults goes first, even when the server
      already answered. The client proves it, and a server key waits as the next
      candidate.

!!! note "A height limit on the server CDM"
    An API key with `server_cdm_max_height` gets the server CDM only up to that height. The
    session create response tells the client who licenses the session in `server_cdm` and gives
    the limit in `server_cdm_max_height`. In a server CDM session, the batch licence response
    lists each track the server refused to license live under `capped_tracks`. A single-track
    server CDM licence answers `403 SERVER_CDM_CAPPED` for such a track. In both cases the client
    licenses those tracks in proxy mode with its own local CDM. See
    [`server_cdm_max_height`](../../reference/configuration/services.md).

### Your own device identity

A service can need more than a licence challenge from your device: an ESN, model keys, or a
session-key exchange that runs on the CDM. The device never leaves your machine. Only these cross
the wire:

- **Device facts.** When it creates the remote session, the client sends its DRM system, security level,
  system ID and device type. The service sees a stand-in with these facts.
- **CDM calls.** When the service calls the CDM, the server sends a `cdm_call` to the client
  and waits. The client makes the challenge, or parses the licence, on its own CDM. When
  the service closes a CDM session, the server sends a `close` call, because a CDM holds
  only a few sessions. The client polls for calls during login and while one of its own
  requests runs service code, so an idle remote session sends nothing extra.
- **Session keys only.** A `keys` call returns Widevine `OPERATOR_SESSION` and `SIGNING`
  keys. It never returns a content key, and the client refuses a PlayReady `keys` call.
  So a server cannot use your device to license titles for itself.
- **Identity config.** A service names the config keys a client may send (for example
  `esn`, `Kpe`, `Kph`, `esn_map`). The server lists them in the service listing as
  `client_config`. The client sends only those keys, from its own `services: <TAG>:`
  block in `unshackle.yaml`. When the client sends an identity, the server drops its own
  value for each of those keys, so the two identities never mix. A mapping (such as
  `esn_map`) merges the client's entries over the server's. A client entry replaces a
  server entry with the same key, also when the server's key is a number. The server keeps
  these values in the remote session's memory only.

A service can also offer an option that builds the remote session on the server's own
device and identity, for the quality that device reaches. The session then makes no live
licence at all: each content key comes from the server vault or the client's own vaults,
and a content key that no vault holds stops the download with an error. The create
response sets `server_device` to `true` for such a remote session. The service then uses
the server's own config: the client's identity values do not apply. The client reads the
server vault only when the API key has the `server_vault` grant (or `server_cdm`); without
it, the client reads only its own vaults. The login tokens of such a remote session hold the
server's device identity, so the server keeps them: `DELETE` returns no cache files, and the
HTTP session headers and `track.data` lose their secret-looking keys, as for a server login.

!!! note "The server vault in a session your device licenses"
    With the `server_vault` grant (or `server_cdm`) on the API key, a remote session that your own
    device licenses still takes content keys from the server vault. The server tries the
    vault first. A track that no server vault holds goes back to your device. See
    [`server_vault`](../../reference/configuration/services.md).

---

## Remote session lifecycle and expiry

Remote sessions live in an **in-memory store on the server** and expire on a timer. You
generally never touch this directly, but it explains behaviour you might observe.

| Behavior | Value | Notes |
|---|---|---|
| Idle session TTL | **300s** (5 min) default | Refreshed on every request to the session and when a `titles` or `tracks` request ends. A remote session does not expire while a request works on it |
| Max concurrent sessions | **100** default | Oldest (least recently used) is evicted when full. A remote session that a request works on goes last |
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
| `POST` | `/api/session/{id}/keys/bad` | Report a server-vault content key that did not decrypt |
| `GET` | `/api/session/{id}/logs` | Drain the service's server-side log output |
| `GET` | `/api/session/{id}/prompt` | Poll interactive auth status / pending prompt |
| `POST` | `/api/session/{id}/prompt` | Submit an answer to a pending prompt |
| `GET` | `/api/session/{id}` | Session info (validity, TTL, counts) |
| `DELETE` | `/api/session/{id}` | Close the remote session, return updated cache, clean up |

### Create: request and response

`POST /api/session/create` requires `service` and `title_id`. It also accepts
`credentials`, `cookies` (base64 of zlib-compressed Netscape cookie file), `proxy`,
`no_proxy`, `profile`, `cache` (a map of `filename → base64(zlib(bytes))`),
`client_region`, `cdm_type`, `cdm_security_level`, `cdm_relay` (`true` when the client
answers CDM calls), `cdm_system_id` and `cdm_device_type` (Widevine only), `service_config`
(the identity config keys the service lists in `client_config`), `client` (a freeform identity
object the dashboard shows as sent), and the track-selection hints `range_`, `vcodec`,
`quality`, `best_available`, plus arbitrary service CLI options
(`additionalProperties: true`).

`dl_params` holds the `dl` track selection: `lang`, `v_lang` and `a_lang`
(arrays of language strings), `acodec` (an array of audio codec names, such as
`EC3`), and `forced_subs` (boolean). The server gives these values to the
service in `ctx.parent.params`, never as service options, so a service option
with the same name, such as an own `--lang`, does not get them. The server uses
the `dl` default for each absent or malformed value: `lang` is `["orig"]`,
`v_lang`, `a_lang` and `acodec` are `[]`, and `forced_subs` is `false`. The
server drops an unknown codec name. An empty array is a valid value and stays
empty.

The server does not forward the `dl` mode flags: `video_only`, `audio_only`,
`subs_only`, `chapters_only`, `list_`, `skip_dl`, `sub_format` and
`no_attachments`. A service that reads one of them to skip work fetches more on
the server, but the result is the same.

In a client-login session (not a server account), the server gives the service
the client's `profile` in `ctx.parent.params`, the same as a local run with that
profile. The profile name keys the service's token cache, so the cache files
the client sends and gets back belong to that profile. A service can also use
the name to select per-profile entries in the server's service config, such as
a per-profile ESN.

The response returns **before** authentication finishes:

```json
{ "session_id": "...uuid4...", "service": "EXAMPLE", "status": "authenticating",
  "server_cdm": false, "server_cdm_max_height": null, "server_vault": true, "server_device": false }
```

`server_vault` tells the client that the server vault serves a remote session its own device
licenses. `server_device` tells it that the service runs on the server's device, so every
content key comes from a vault.

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
- A prompt after authentication (in `get_titles()`, `get_tracks()` or licensing) raises
  `RuntimeError` at once. The client polls for prompts only during authentication, so
  no answer can come.
- A CDM call (`InputBridge.request_cdm`) works during and after authentication, because the
  client also polls while its own requests run. A call that no client fetches within 10 seconds
  fails then, because no client is listening. A fetched call waits 60 seconds for its answer.
  The call leaves the auth status as it was. An answer never marks the remote session as client-authenticated.
- `AUTH_INPUT_TIMEOUT = 600.0` seconds. This is also the TTL granted to
  `AUTHENTICATING` / `PENDING_INPUT` sessions in the store.

`GET /api/session/{id}/prompt` returns one of:

```json
{ "status": "authenticated" }
{ "status": "authenticating" }
{ "status": "pending_input", "prompt": "Enter code: " }
{ "status": "pending_input", "prompt": "...", "cdm_call": { "op": "challenge", "drm": "widevine", "init_data": "...", "license_type": "OFFLINE", "privacy_mode": true, "service_certificate": "..." } }
{ "status": "failed", "error": "...message..." }
```

A `cdm_call` comes first, in any auth state. The client answers it with a JSON object in
`response`: `{"session": "...", "challenge": "<base64>"}` for `challenge`,
`{"keys": [{"kid", "type", "key", "permissions"}]}` for `keys`, `{}` for `close`, or
`{"error": "..."}` when its CDM refuses. An older client refuses `close`, and the server
ignores that refusal. An answered call stops being pending at once, so a client that polls again
straight away does not see it twice.

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
On `DELETE`, when the login belongs to the client (`client_auth`) and the remote
session does not run on the server's device, the handler harvests updated cache files from the whole directory tree (compressing each
with zlib and base64-encoding, **excluding** `titles_*` files) and returns them
under a `cache` field, so the client can keep refreshed tokens. Each cache key
is the file path relative to that directory, with `/` separators and no `.json`
suffix, which is the `Cacher` cache key the service reads it with:

```json
{
  "status": "ok",
  "cache": {
    "tokens": "...base64(zlib(bytes))...",
    "session_web/<sha1>": "...base64(zlib(bytes))..."
  }
}
```

Both sides reject a cache key with a drive letter, a root, a `..` segment, an empty
segment, or more than 8 segments before it becomes a path, so a peer cannot write
outside the cache directory. Both sides also refuse a `cache` map with more than
64 entries. The server skips a cache key it cannot write, such as one that names an
existing file as a directory, and logs a warning.

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
  uses the server's `timeout` from `remote_services` (default 120s) as the read timeout,
  and treats any `status_code >= 400` as fatal: it logs `Server error [<error_code>]: <message>`
  and raises `SystemExit(1)`. A `200` body with `"status": "error"` is also fatal (see
  the heartbeat below).
- **Heartbeat.** The `titles`, `tracks` and `license` routes send a newline every 30s while
  the service works. The read timeout starts again at each byte, so a slow request stays
  open for as long as the server works on it, and a reverse proxy does not close it as
  idle. A route that answers within 30s sends the usual response. After 30s the status
  is already `200`, so a failure after that time arrives as the usual error body with
  status `200`. Your client must check `status` in the body. JSON parsers ignore the
  leading newlines. The server does not gzip-compress a heartbeat response, because gzip
  output cannot start before the body is ready.
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
