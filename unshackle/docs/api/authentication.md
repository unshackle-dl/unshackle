# Authentication

When `api_secret` is set in `unshackle.yaml`, all API requests require the **`X-Secret-Key`** header. There is no query-parameter fallback. `/api/health` is always reachable without authentication. `--no-key` disables auth entirely (not recommended for public-facing servers).

```yaml
# unshackle.yaml
serve:
  api_secret: "your-master-secret"          # falls back to global users map below
  remote_only: false                         # also toggleable via --remote-only
  services: ["EXAMPLE1", "EXAMPLE2"]         # optional global service allowlist
  users:
    user-secret-1:
      username: alice
      devices: ["my_widevine_l3"]            # Widevine WVD names this user may use
      playready_devices: ["my_pr_sl2000"]    # PlayReady PRD names; defaults to [] (no access)
      services: ["EXAMPLE1"]                  # optional per-user allowlist (intersected with global)
    user-secret-2:
      username: bob
      devices: []
      playready_devices: []
```

## Service allowlists

`config.serve.services` is the global allowlist; `users.<key>.services` further narrows it per key. The effective set is the intersection. Endpoints affected: `/api/services`, `/api/search`, `/api/list-titles`, `/api/list-tracks`, `/api/download`, and all `/api/session/*` routes.

## CDM access (server-side decryption)

There is no separate "tier" flag. Whether the server can return KID:KEY for a session-mode download depends solely on the device lists configured for the calling user key:

- Empty `devices` and `playready_devices` -> server can only proxy CDM challenges; the client must run its own CDM and parse the license.
- Populated lists -> the client may set `mode: "server_cdm"` on `/api/session/{id}/license` and receive `{ "keys": { "<track_id>": { "<KID>": "<KEY>" } } }` instead of raw license bytes.

Per-service CDM type can be pinned via `config.cdm` (`widevine`/`playready`) or per-service `cdm_type`; otherwise the server picks the type the user has devices for.

## Server-side `dl` defaults

Any flag accepted by `/api/download` (see the table below) can be declared under `serve:` in `unshackle.yaml` and the API will apply it as a default. Request-body values still win. Useful for raising concurrency without changing every client call:

```yaml
serve:
  api_secret: "..."
  users: { ... }
  downloads: 4        # parallel tracks per download job
  workers: 16         # threads per track segment fetch
  best_available: true
  no_proxy_download: false
```

Layering order: built-in defaults < `serve.*` overrides < service-specific click defaults < request body.

## Per-request override gates

Two security gates under `serve:` guard capabilities that `/api/download` clients can request. Both default OFF; an unset or `false` value causes the server to reject the request with `403 FORBIDDEN`.

**`cdm_overrides`** — a `/api/download` job body may include a `cdm` field to select a specific server-side device. When this gate is off, every such request is rejected. Set to `true` to allow any device the server has, or to a list of device names to restrict to an explicit subset:

```yaml
serve:
  # Allow any server-side device:
  cdm_overrides: true

  # Or restrict to specific devices:
  cdm_overrides:
    - generic_nexus_4464_l3
```

**`allow_job_credentials`** — a `/api/download` job body may include a `credential` (single) or `credentials` (map) field to authenticate the job with client-supplied secrets instead of the server's configured credentials. When this gate is off, any job containing either field is rejected. Each distinct credential gets its own isolated token cache on the server. Set to `true` to allow:

```yaml
serve:
  allow_job_credentials: true
```

Both gates are independent and can be combined. A typical locked-down deployment leaves both unset; a trusted single-client deployment might enable both.
