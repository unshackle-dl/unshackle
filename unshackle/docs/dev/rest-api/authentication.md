# REST API Authentication

The unshackle REST API protects every endpoint (with one exception) behind a single API key. Clients present that key in a request header, and the server checks it against the keys defined in your configuration. This page explains how that check works, how to configure keys, how clients send them, and the extra controls that build on top of the key: per-key service allowlists, per-key CDM device access, and session IP binding.

The API is started with the [`unshackle serve`](../../guide/cli-reference.md) command. Authentication is on by default: if you start the server without an API key configured, it refuses to launch.

!!! note "Which server am I running?"
    `unshackle serve` can run in two shapes, and each uses a slightly different piece of code to check your key:

    - The **integrated server** (the default) exposes the REST API *and* the pywidevine / pyplayready CDM HTTP endpoints.
    - The **API-only server** (`--api-only`, also implied by `--remote-only`) exposes only the REST API.

    The header, the config, and the behaviour you see as a client are the same in both. The differences are internal and are called out in the developer notes below.

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

This value becomes the one and only key that the server accepts. Requests that present it in the `X-Secret-Key` header are allowed; everything else is rejected.

!!! warning "The server will not start without a key"
    If you start `unshackle serve` with no `api_secret` configured, and you have *not* passed `--no-key`, the command fails immediately with:

    ```
    API secret key is not configured. Please add 'api_secret' to the 'serve' section in your config.
    ```

    Choose a long, unpredictable value. The key is the only thing standing between the internet and your CDM and download endpoints.

### Multiple keys with `users`

For finer control you can define named users, each with their own key. A user entry can also grant CDM device access and restrict which services that key may touch:

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

The key of each entry under `users` (for example `a-secret-key-for-alice`) *is* the secret that client sends in `X-Secret-Key`. The `username` is a human-readable label used in logs and in job/history metadata.

!!! info "How `api_secret` and `users` combine (integrated server)"
    On the integrated server, the configured `api_secret` is automatically added as an extra accepted key alongside everyone in `users`. It is granted access to all loaded Widevine and PlayReady devices and is labelled `api_user`. So `api_secret` behaves as an admin-level key in addition to any named users you define.

    On the API-only server, only `api_secret` is accepted: the single-key model. (See the developer notes for the exact difference.)

### CDM device access per key

Under the integrated server, a key can only run DRM licensing through a CDM device that its user entry lists:

| Config key | Grants access to |
|---|---|
| `devices` | Widevine `.wvd` devices, by name |
| `playready_devices` | PlayReady `.prd` devices, by name |

!!! warning "PlayReady access is opt-in per user"
    If a user entry omits `playready_devices`, that key is given **no** PlayReady access, and the server logs a warning at startup. You must list PlayReady devices explicitly for any key that needs them. Widevine and PlayReady device files themselves are auto-loaded from your WVDs and PRDs directories.

!!! note "Device lists *are* the server-side-decryption switch"
    There is no separate tier, capability flag, or permission toggle for whether the server will hand back keys (`KID:KEY`) for a session download. The presence of devices on the calling key decides it. With **empty** `devices` and `playready_devices`, the server can only proxy CDM challenges, so the client must run its own CDM. Once those lists are **populated**, the client may request `mode:server_cdm` and receive keys back. Don't go looking for a tier setting; configuring devices is what enables server-side decryption.

### Disabling authentication

The `--no-key` flag turns authentication off entirely. Every request is allowed, `X-Secret-Key` is ignored, and no key needs to be configured.

```bash
unshackle serve --no-key
```

!!! danger "Only use `--no-key` on a trusted, isolated network"
    With `--no-key` there is nothing stopping any client that can reach the port from listing your services, starting downloads, or using your CDM devices. Never expose a `--no-key` server to an untrusted network.

## How clients present credentials

Clients authenticate by sending the secret in a single HTTP request header on every call:

```
X-Secret-Key: <your-secret>
```

The value must exactly match one of the configured keys (`api_secret`, or one of the keys under `users`). There is no login step, no session cookie, and no token exchange for API access. The same static key is sent on each request.

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
    unshackle's own [remote download](../../guide/downloading.md) client (`RemoteService`) sends `X-Secret-Key` automatically, reading the key from your `remote_services` config. It also sends a `User-Agent` of `unshackle/<version>`. You only deal with the header directly when writing your own client.

### The health check is always open

One endpoint is exempt from authentication: `GET /api/health`. It never requires a key, so you can use it for liveness and version probes without embedding a secret in your monitoring:

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

Every other endpoint (including the Swagger UI at `/api/docs/` on the API-only server) requires a valid key.

## Authentication failures

When a request is rejected for authentication reasons, the response has an HTTP `401 Unauthorized` status and a small JSON body.

=== "Missing header"

    Sending a request with no `X-Secret-Key` header:

    ```json
    {
      "status": 401,
      "message": "Secret Key is Empty."
    }
    ```

=== "Wrong key"

    Sending a key that is not in your config:

    ```json
    {
      "status": 401,
      "message": "Secret Key is Invalid."
    }
    ```

!!! warning "Auth errors use a different JSON shape than other errors (developer note)"
    The rest of the API reports errors with a structured body containing `error_code`, `message`, `timestamp`, and other fields (see [Errors](errors.md)). The authentication middleware is different: it returns `{"status": 401, "message": "..."}`, where `status` is an **integer**, and there is no `error_code` or `timestamp`.

    A robust client should treat any `401` as an authentication problem and be prepared to parse *both* shapes rather than assuming the standard error body is always present.

## Building on the key: access controls

Once a request is authenticated, several additional controls decide what that specific key is allowed to do.

### Per-key service allowlists

You can restrict which services a key may use. Two layers combine:

- **Global allowlist**: `serve.services`, applied to every key.
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

Endpoints that name a service (listing services, searching, listing titles/tracks, downloading, profiles, history, and remote sessions) all filter against this allowlist. A request for a service outside the key's allowlist is rejected as an invalid service. Service tags are matched after normalization, so casing and aliases resolve consistently.

### CDM overrides and per-job credentials

Two download-time capabilities are gated behind server config and, unless enabled, are refused with `403 Forbidden` even for an authenticated key:

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
    The two gates are fully independent and can be combined. The intended posture is that a locked-down or public-facing deployment leaves **both** unset (they default off and reject with `403`) so client requests can never steer the server's CDM or feed it their own logins. Enabling them only makes sense for a trusted single-client deployment, where the caller and the operator are effectively the same party.

!!! note "Client-supplied credentials get isolated token caches"
    When `allow_job_credentials` is enabled and a download job supplies its own `credential` / `credentials`, each distinct credential is given its **own** isolated token cache on the server. Client-supplied logins are never shared or merged with the server's configured-credential caches, so they cannot cross-contaminate or read the server's own token state, and they don't clobber each other.

See [Downloads](endpoints.md) for how these fields are used on the download endpoint.

### Session IP binding (remote sessions)

The remote-download session endpoints (`/api/session/*`) add an extra check on top of the key. When a session is created, the server records the client's IP address. Any later request for that session must come from the **same** IP, or it is rejected with `403 Forbidden`. This means a session is bound both to the key that created it and to the address it was created from.

## Transport security (HTTPS)

The API server speaks plain HTTP. Because your key is sent on every request, you should not expose it over an unencrypted connection on an untrusted network. To serve over HTTPS, run a reverse proxy in front of unshackle. The `serve` command can launch Caddy for you:

```bash
unshackle serve --caddy
```

With `--caddy`, unshackle also starts `caddy run` using the `Caddyfile` located next to your unshackle config, letting you terminate TLS and reverse-proxy to the API. Configuring Caddy itself (certificates, domains, upstream) is done in that `Caddyfile`.

## Quick reference

| Item | Value |
|---|---|
| Auth header | `X-Secret-Key` |
| Default bind address | `127.0.0.1:8786` |
| Exempt endpoint | `GET /api/health` |
| Failure status | `401 Unauthorized` |
| Missing-header body | `{"status": 401, "message": "Secret Key is Empty."}` |
| Invalid-key body | `{"status": 401, "message": "Secret Key is Invalid."}` |
| Disable auth | `--no-key` (all requests allowed) |
| Config location | `serve.api_secret` and `serve.users` in `unshackle.yaml` |

---

## Developer notes: how the check is implemented

!!! note "This section is for people writing or debugging the server, not for API consumers."

### Two middlewares, one header

The header and config are identical between modes, but the code that enforces them differs:

- **API-only server** (`--api-only`, or `--remote-only` which forces API-only). An `api_key_authentication` middleware runs on every request. It lets `GET /api/health` through unconditionally, then reads `X-Secret-Key`. An empty header returns the *"Secret Key is Empty."* body; a header that is not a configured user returns *"Secret Key is Invalid."*. In this mode the accepted-keys map contains exactly one entry: your `api_secret` (labelled `api_user`, with no devices). With `--no-key`, this middleware is simply not installed and the users map is empty.

- **Integrated server** (default). A `serve_authentication` middleware wraps pywidevine's authentication (and pyplayready's for `/playready*` paths), and the REST routes live in the same application, guarded by the same check. Here the accepted-keys map is your full `serve` config: `api_secret` (auto-added with access to all loaded devices) plus every entry under `users`, each with its declared `devices` / `playready_devices`.

Because the API-only middleware guards *all* non-health paths, the Swagger UI at `/api/docs/` is also behind `X-Secret-Key` there. In `--remote-only` mode the Swagger UI is not mounted at all.

### CORS and the `Authorization` header

A `cors_middleware` runs on every response in both modes (and answers `OPTIONS` preflight requests with an empty response). It emits:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, PUT, DELETE, OPTIONS
Access-Control-Allow-Headers: Content-Type, X-Secret-Key, Authorization
Access-Control-Max-Age: 3600
```

The REST API authenticates with `X-Secret-Key`; `Authorization` is advertised because the co-hosted pywidevine / pyplayready CDM endpoints use it. `PUT` appears in the allowed methods list but no REST route uses it; only `GET`, `POST`, and `DELETE` are wired up.

### Where device access is read

The map of accepted keys lives at `request.app["config"]["users"]`. For DRM operations, a key's permitted Widevine devices come from its `devices` list and PlayReady devices from `playready_devices`; the first configured device is used when a request does not (or may not) override it. A server-wide `config.cdm[<service>]` mapping, if present, takes precedence over the key's device list when resolving which device to use.
