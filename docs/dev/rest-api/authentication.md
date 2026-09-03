# REST API Authentication

The unshackle REST API protects every endpoint (with one exception) behind a single API key. Clients present that API key in a request header, and the server examines it against the API keys in your configuration. This page explains how that check works, how to configure API keys, and how clients send them. It also explains the extra controls on top of the API key: per-key service allowlists, per-key CDM device access, and remote session IP binding.

You start the API with the [`unshackle serve`](../../guide/cli-reference.md) command. Authentication is on by default: if you start the server without an API key configured, it refuses to launch.

!!! note "Which server am I running?"
    `unshackle serve` can operate in two shapes, and each uses a slightly different piece of code to examine your API key:

    - The **integrated server** (the default) exposes the REST API *and* the pywidevine / pyplayready CDM HTTP endpoints.
    - The **API-only server** (`--api-only`, also implied by `--remote-only`) exposes only the REST API.

    The header, the config, and the behaviour you see as a client are the same in both. The differences are internal, and the developer notes below give them.

## The short version

1. Add an `api_secret` to the `serve` section of your `unshackle.yaml`.
2. Start the server with `unshackle serve`.
3. Send that secret in the `X-Secret-Key` header on every request except `GET /api/health`.

```bash title="A minimal authenticated request"
curl -H "X-Secret-Key: your-secret-key-here" \
     http://127.0.0.1:8786/api/services
```

## Configuring authentication

All authentication settings live under the `serve` section of your unshackle config file (`unshackle.yaml`).

### A single API key

The simplest setup defines one shared secret with `api_secret`:

```yaml title="unshackle.yaml"
serve:
  api_secret: "a-long-random-secret-string"
```

This value becomes the one and only API key that the server accepts. The server allows requests that present it in the `X-Secret-Key` header. The server rejects everything else.

!!! warning "The server will not start without a key"
    If you start `unshackle serve` with no `api_secret` configured, and you have *not* passed `--no-key`, the command fails immediately with:

    ```
    API secret key is not configured. Please add 'api_secret' to the 'serve' section in your config.
    ```

    Choose a long, unpredictable value. The API key is the only thing standing between the internet and your CDM and download endpoints.

### Multiple keys with `users`

For finer control you can define named users, each with their own API key. A user entry can also grant CDM device access and restrict which services that API key may touch:

```yaml title="unshackle.yaml"
serve:
  api_secret: "the-primary-admin-key"
  users:
    a-secret-key-for-alice:
      username: alice
      devices: ["my_widevine_device"]         # Widevine (.wvd) devices this key may use
      playready_devices: ["my_playready_prd"]  # PlayReady (.prd) devices this key may use
      services: ["EXAMPLE1", "EXAMPLE2"]       # optional per-key service allowlist
    a-secret-key-for-bob:
      username: bob
      devices: ["my_widevine_device"]
```

The config key of each entry under `users` (for example `a-secret-key-for-alice`) *is* the secret that client sends in `X-Secret-Key`. The `username` is a human-readable label used in logs and in job/history metadata.

!!! info "How `api_secret` and `users` combine (integrated server)"
    On the integrated server, the server automatically adds the configured `api_secret` as an extra accepted API key alongside everyone in `users`. It gives that API key access to all loaded Widevine and PlayReady devices, and labels it `api_user`. So `api_secret` behaves as an admin-level API key in addition to any named users you define.

    On the API-only server, the server accepts only `api_secret`: the single-key model. (See the developer notes for the exact difference.)

### CDM device access per API key

Under the integrated server, an API key can do DRM licensing only through a CDM device that its user entry lists:

| Config key | Grants access to |
|---|---|
| `devices` | Widevine `.wvd` devices, by name |
| `playready_devices` | PlayReady `.prd` devices, by name |

!!! warning "PlayReady access is opt-in per user"
    If a user entry omits `playready_devices`, the server gives that API key **no** PlayReady access, and logs a warning at startup. You must name the PlayReady devices explicitly for any API key that needs them. Widevine and PlayReady device files themselves are auto-loaded from your WVDs and PRDs directories.

!!! note "Device lists *are* the server-side-decryption switch"
    There is no separate tier, capability flag, or permission toggle for whether the server will hand back content keys (`KID:KEY`) for a remote session download. The presence of devices on the calling API key decides it. With **empty** `devices` and `playready_devices`, the server can only proxy CDM challenges, so the client must operate its own CDM. Once those lists are **populated**, the client may request `mode:server_cdm` and receive content keys back. Do not look for a tier setting. The devices you configure are what enables server-side decryption.

### Disabling authentication

The `--no-key` flag turns authentication off entirely. The server allows every request, ignores `X-Secret-Key`, and needs no API key in your configuration.

```bash
unshackle serve --no-key
```

!!! danger "Only use `--no-key` on a trusted, isolated network"
    With `--no-key`, any client that can reach the port can show your services, start downloads, and use your CDM devices. Never put a `--no-key` server on an untrusted network.

## How clients present credentials

Clients authenticate by sending the secret in a single HTTP request header on every call:

```
X-Secret-Key: <your-secret>
```

The value must match one of the configured API keys exactly (`api_secret`, or one of the keys under `users`). There is no login step, no HTTP session cookie, and no token exchange for API access. The client sends the same static API key on each request.

!!! info "One route also accepts the key as a query parameter"
    The job event stream, `GET /api/download/jobs/{job_id}/events`, also accepts the API key in a `secret_key` query parameter. This is for browsers: the `EventSource` API cannot set request headers. The exception applies to that one route only, and it works in both server modes.

    ```javascript
    new EventSource(`http://127.0.0.1:8786/api/download/jobs/${jobId}/events?secret_key=${key}`);
    ```

    The header always wins. If the request has an `X-Secret-Key` header, the server examines that header and ignores the query parameter. The server rejects a wrong header even when the query parameter holds a valid API key.

    An API key in a URL is easier to leak than an API key in a header, because proxies and browser history can record it. Use the header wherever your client can set one.

=== "curl"

    ```bash
    curl -H "X-Secret-Key: your-secret-key-here" \
         http://127.0.0.1:8786/api/services
    ```

=== "Python (requests)"

    ```python
    import requests

    BASE = "http://127.0.0.1:8786"
    HEADERS = {"X-Secret-Key": "your-secret-key-here"}

    resp = requests.get(f"{BASE}/api/services", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    print(resp.json())
    ```

=== "JavaScript (fetch)"

    ```javascript
    const resp = await fetch("http://127.0.0.1:8786/api/services", {
      headers: { "X-Secret-Key": "your-secret-key-here" },
    });
    if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
    console.log(await resp.json());
    ```

!!! tip "The reference client sets it for you"
    unshackle's own [remote download](../../guide/downloading.md) client (`RemoteService`) sends `X-Secret-Key` automatically, reading the API key from your `remote_services` config. It also sends a `User-Agent` of `unshackle/<version>`. You only deal with the header directly when writing your own client.

### The health check is always open

One endpoint is exempt from authentication: `GET /api/health`. It never requires an API key, so you can use it for liveness and version probes without embedding a secret in your monitoring:

```bash title="No key needed"
curl http://127.0.0.1:8786/api/health
```

```json title="Response"
{
  "status": "ok",
  "version": "5.3.0",
  "update_check": {
    "update_available": null,
    "current_version": "5.3.0",
    "latest_version": null
  }
}
```

Every other endpoint (including the Swagger UI at `/api/docs/` on the API-only server) requires a valid API key.

## Authentication failures

When the server rejects a request for authentication reasons, the response has an HTTP `401 Unauthorized` status and a small JSON body.

=== "Missing header"

    Sending a request with no `X-Secret-Key` header:

    ```json
    {
      "status": 401,
      "message": "Secret Key is Empty."
    }
    ```

=== "Wrong key"

    Sending an API key that is not in your config:

    ```json
    {
      "status": 401,
      "message": "Secret Key is Invalid."
    }
    ```

!!! warning "Auth errors use a different JSON shape than other errors (developer note)"
    The rest of the API reports errors with a structured body containing `error_code`, `message`, `timestamp`, and other fields (see [Errors](errors.md)). The authentication middleware is different: it returns `{"status": 401, "message": "..."}`, where `status` is an **integer**, and there is no `error_code` or `timestamp`.

    In your client, treat any `401` as an authentication problem and parse *both* shapes, because the standard error body is not always present.

## Rate limit rejections

An API key that carries a rate limit gets `429 Too Many Requests` once it goes over, from the
same middleware and in the same shape as the `401` bodies above, with a `Retry-After` header in
seconds:

```json
{
  "status": 429,
  "message": "Rate limit exceeded."
}
```

A limit comes from `serve.tiers.<name>.rate_limit` or from the API key's own
`serve.users.<key>.rate_limit`, in requests per hour. An API key with neither has no limit, and
`GET /api/health` is never limited. See
[dashboard rate limits](dashboard.md#rate-limits) for the config and the counters, and
[services configuration](../../reference/configuration/services.md) for the keys themselves.

!!! warning "`429` is not the `RATE_LIMITED` error body"
    `RATE_LIMITED` in [Errors](errors.md) is a *service* rate limit that a handler raises, and it
    arrives in the standard envelope with an `error_code`. The `429` here comes from the
    middleware before any handler runs, and has the integer-`status` shape shown above. A client
    that reads `error_code` to classify a `429` must cope with it being absent.

## Access controls on top of the API key

After the server authenticates a request, several additional controls decide what that specific API key can do.

### Per-key service allowlists

You can restrict which services an API key may use. Two layers combine:

- **Global allowlist**: `serve.services`, applied to every API key.
- **Per-key allowlist**: `services` inside that key's `users` entry.

The effective allowlist for a request is:

| Global set? | Per-key set? | Result |
|---|---|---|
| Yes | Yes | The **intersection** of the two |
| Yes | No | The global list |
| No | Yes | The per-key list |
| No | No | No restriction, all services allowed |

```yaml title="Restricting services"
serve:
  api_secret: "admin-key"
  services: ["EXAMPLE1", "EXAMPLE2", "EXAMPLE3"]   # global ceiling for all keys
  users:
    limited-key:
      username: limited
      services: ["EXAMPLE1"]          # this key ends up allowed only EXAMPLE1
```

Endpoints that name a service (listing services, searching, listing titles/tracks, downloading, profiles, history, and remote sessions) all filter against this allowlist. The server rejects a request for a service outside the API key's allowlist as an invalid service. The server matches service tags after normalisation, so casing and aliases give the same result.

### CDM overrides and per-job credentials

The server config gates two download-time capabilities. Unless you enable them, the server refuses them with `403 Forbidden`, even for an authenticated API key:

| Config key | Default | Controls |
|---|---|---|
| `serve.cdm_overrides` | off | Whether a request may pick a specific CDM device via the `cdm` field. Set to `true` to allow any, or to a list/set of device names to allow only those. |
| `serve.allow_job_credentials` | off | Whether a request may pass its own `credential` / `credentials` login details for a download job. |

```yaml title="Opting in to per-request CDM and credentials"
serve:
  api_secret: "admin-key"
  cdm_overrides: ["my_widevine_device"]  # or true to allow any configured device
  allow_job_credentials: true
```

!!! tip "When to enable these gates"
    The two gates are fully independent, and you can combine them. For a locked-down or public-facing deployment, leave **both** unset. They then default off and reject with `403`, so client requests can never steer the server's CDM or feed it their own logins. Enable them only for a trusted single-client deployment, where the caller and the operator are effectively the same party.

!!! note "Client-supplied credentials get isolated token caches"
    When you enable `allow_job_credentials` and a download job supplies its own `credential` / `credentials`, the server gives each distinct credential its **own** isolated token cache. The server never shares or merges client-supplied logins with its configured-credential caches. They cannot cross-contaminate or read the server's own token state, and they do not clobber each other.

See [Downloads](endpoints.md) for how the download endpoint uses these fields.

### Session IP binding (remote sessions)

The remote-download session endpoints (`/api/session/*`) add an extra check on top of the API key. When the server creates a remote session, it records the client's IP address. Any later request for that remote session must come from the **same** IP, or the server rejects it with `403 Forbidden`. Thus the server binds a remote session both to the API key that created it and to the address it came from.

## Transport security (HTTPS)

The API server speaks plain HTTP. Because the client sends your API key on every request, do not use it over an unencrypted connection on an untrusted network. To serve over HTTPS, operate a reverse proxy in front of unshackle. The `serve` command can launch Caddy for you:

```bash
unshackle serve --caddy
```

With `--caddy`, unshackle also starts `caddy run` using the `Caddyfile` located next to your unshackle config, letting Caddy end the TLS connection and reverse-proxy to the API. Configuring Caddy itself (certificates, domains, upstream) is done in that `Caddyfile`.

## Quick reference

| Item | Value |
|---|---|
| Auth header | `X-Secret-Key` |
| Query-parameter alternative | `?secret_key=` on `GET /api/download/jobs/{job_id}/events` only |
| Default bind address | `127.0.0.1:8786` |
| Exempt endpoint | `GET /api/health` |
| Failure status | `401 Unauthorized`, or `429 Too Many Requests` for a key over its rate limit |
| Missing-header body | `{"status": 401, "message": "Secret Key is Empty."}` |
| Invalid-key body | `{"status": 401, "message": "Secret Key is Invalid."}` |
| Over-limit body | `{"status": 429, "message": "Rate limit exceeded."}`, plus a `Retry-After` header |
| Rate limit config | `serve.tiers.<name>.rate_limit`, overridden by `serve.users.<key>.rate_limit` |
| Disable auth | `--no-key` (all requests allowed) |
| Config location | `serve.api_secret` and `serve.users` in `unshackle.yaml` |

---

## Developer notes: how the server does the check

!!! note "This section is for people writing or debugging the server, not for API consumers."

### Two middlewares, one header

The header and config are identical between modes, but the code that enforces them differs:

- **API-only server** (`--api-only`, or `--remote-only` which forces API-only). An `api_key_authentication` middleware runs on every request. It lets `GET /api/health` through unconditionally, then reads `X-Secret-Key`. An empty header returns the *"Secret Key is Empty."* body. A header that is not a configured user returns *"Secret Key is Invalid."*. In this mode the accepted-keys map holds your `api_secret` (labelled `api_user`, with no devices) plus every key under `serve.users`. A per-user key therefore authenticates here exactly as it does on the integrated server, and that user's `services` allowlist applies as usual. With `--no-key`, this middleware is not installed and the users map is empty.

- **Integrated server** (default). A `serve_authentication` middleware wraps pywidevine's authentication (and pyplayready's for `/playready*` paths), and the REST routes live in the same application, guarded by the same check. Here the accepted-keys map is your full `serve` config: `api_secret` (auto-added with access to all loaded devices) plus every entry under `users`, each with its declared `devices` / `playready_devices`.

Because the API-only middleware guards *all* non-health paths, the Swagger UI at `/api/docs/` is also behind `X-Secret-Key` there. In `--remote-only` mode the Swagger UI is not mounted at all.

Both middlewares read the caller's API key through the shared `request_secret_key()` helper in `unshackle/core/api/handlers.py`. That helper returns the `X-Secret-Key` header, and falls back to the `secret_key` query parameter only when the matched route is the job event stream. The integrated server needs one extra branch for this. pywidevine's `authentication` is header-only. So `serve_authentication` compares the API key from the query parameter itself when the header is absent, and delegates to pywidevine in every other case. That is why both modes reject a present-but-wrong header.

### CORS and the `Authorization` header

A `cors_middleware` runs on every response in both modes (and answers `OPTIONS` preflight requests with an empty response). It emits:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, PUT, DELETE, OPTIONS
Access-Control-Allow-Headers: Content-Type, X-Secret-Key, Authorization
Access-Control-Max-Age: 3600
```

The REST API authenticates with `X-Secret-Key`. The middleware advertises `Authorization` because the co-hosted pywidevine / pyplayready CDM endpoints use it. `PUT` appears in the allowed-methods list, but no REST route uses it. Only `GET`, `POST`, and `DELETE` have routes.

### Where device access is read

The map of accepted API keys lives at `request.app["config"]["users"]`. For DRM operations, an API key's permitted Widevine devices come from its `devices` list, and PlayReady devices from `playready_devices`. The server uses the first configured device when a request does not (or may not) override it. A server-wide `config.cdm[<service>]` mapping, if present, takes precedence over the API key's device list when the server chooses which device to use.
