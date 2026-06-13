# Advanced & System Configuration

This document covers advanced features, debugging, and system-level configuration options.

## serve (dict)

Configuration for the integrated server that provides CDM endpoints (Widevine/PlayReady) and a REST API for remote downloading.

Start the server with:

```bash
unshackle serve                          # Default: localhost:8786
unshackle serve -h 0.0.0.0 -p 8888      # Listen on all interfaces
unshackle serve --no-key                 # Disable authentication
unshackle serve --api-only               # REST API only, no CDM endpoints
unshackle serve --remote-only            # Only expose remote service session endpoints
```

### CLI Options

| Option | Default | Description |
| --- | --- | --- |
| `-h, --host` | `127.0.0.1` | Host to serve from |
| `-p, --port` | `8786` | Port to serve from |
| `--caddy` | `false` | Also serve with Caddy reverse-proxy for HTTPS |
| `--api-only` | `false` | Serve only the REST API, disable CDM endpoints |
| `--no-widevine` | `false` | Disable Widevine CDM endpoints |
| `--no-playready` | `false` | Disable PlayReady CDM endpoints |
| `--no-key` | `false` | Disable API key authentication (allows all requests) |
| `--debug-api` | `false` | Include tracebacks and stderr in API error responses |
| `--debug` | `false` | Enable debug logging for API operations |
| `--remote-only` | `false` | Only expose remote service session endpoints (health, services, search, session) |

### Configuration

- `api_secret` - Secret key for REST API authentication. Required unless `--no-key` is used. All API requests must include this key via the `X-Secret-Key` header.
- `compression_level` - Compression level for API payloads (manifests, cache, cookies). `0`=off, `1`=fast, `6`=balanced, `9`=max. Default: `1`.
- `session_ttl` - Session inactivity timeout in seconds. Each request resets the timer. Default: `300`.
- `max_sessions` - Maximum concurrent sessions before the oldest is evicted. Default: `100`.
- `services` - Optional global service allowlist. Only these service tags are exposed. If omitted, all services are available.
- `devices` - List of Widevine device files (.wvd). If not specified, auto-populated from the WVDs directory.
- `playready_devices` - List of PlayReady device files (.prd). If not specified, auto-populated from the PRDs directory.
- `users` - Dictionary mapping user secret keys to their access configuration:
  - `devices` - List of Widevine devices this user can access
  - `playready_devices` - List of PlayReady devices this user can access
  - `username` - Internal logging name for the user (not visible to users)
  - `services` - Optional per-user service allowlist. Effective access is the intersection of global and per-user allowlists.

#### Server-side `dl` defaults

Any key accepted by `/api/download` (see `docs/API.md`) can also be declared directly under `serve:` and the REST API will treat it as a default. Per-request bodies still win. Use this to raise concurrency, force `best_available`, etc. without each client repeating the values:

```yaml
serve:
  api_secret: "..."
  users: { ... }
  downloads: 4              # parallel tracks per job
  workers: 16               # threads per track
  best_available: true
  no_proxy_download: false
```

Layering: built-in defaults < `serve.*` overrides < service-specific defaults < request body.

For example,

```yaml
serve:
  api_secret: "your-secret-key-here"
  compression_level: 1
  session_ttl: 300
  max_sessions: 100
  # services:           # global allowlist (optional)
  #   - EXAMPLE1
  #   - EXAMPLE2
  users:
    secret_key_for_jane: # 32bit hex recommended, case-sensitive
      devices: # list of allowed Widevine devices for this user
        - generic_nexus_4464_l3
      playready_devices: # list of allowed PlayReady devices for this user
        - my_playready_device
      username: jane # only for internal logging, users will not see this name
      # services:        # per-user allowlist (optional)
      #   - EXAMPLE1
    secret_key_for_james:
      devices:
        - generic_nexus_4464_l3
      username: james
  # devices can be manually specified by path if you don't want to add it to
  # unshackle's WVDs directory for whatever reason
  # devices:
  #   - 'C:\Users\john\Devices\test_devices_001.wvd'
  # playready_devices:
  #   - '/path/to/device.prd'
```

### REST API

When the server is running, interactive API documentation is available at:

- **Swagger UI**: `http://localhost:8786/api/docs/`

See [API.md](API.md) for full REST API documentation with endpoints, parameters, and examples.

---

## max_concurrent_downloads (int)

Maximum number of `/api/download` jobs the serve queue manager will execute in parallel. Each job runs the full `dl` pipeline (authenticate, fetch tracks, decrypt, mux) in its own worker subprocess. This is independent of `serve.downloads`, which controls parallel tracks **inside** a single job. Default: `2`.

```yaml
max_concurrent_downloads: 4
```

---

## download_job_retention_hours (int)

How long completed, failed, or cancelled download jobs remain queryable via `/api/download/jobs/{job_id}` before the periodic cleanup loop drops them. Default: `24`.

```yaml
download_job_retention_hours: 48
```

---

## debug (bool)

Enables comprehensive debug logging. Default: `false`

When enabled (either via config or the `-d`/`--debug` CLI flag):
- Sets console log level to DEBUG for verbose output
- Creates JSON Lines (`.jsonl`) debug log files with structured logging
- Logs detailed information about sessions, service configuration, DRM operations, and errors with full stack traces

For example,

```yaml
debug: true
```

---

## debug_keys (bool)

Controls whether actual decryption keys (CEKs) are included in debug logs. Default: `false`

When enabled:
- Content encryption keys are logged in debug output
- Only affects `content_key` and `key` fields (the actual CEKs)
- Key metadata (`kid`, `keys_count`, `key_id`) is always logged regardless of this setting
- Passwords, tokens, cookies, and session tokens remain redacted even when enabled

For example,

```yaml
debug_keys: true
```

---

## set_terminal_bg (bool)

Controls whether unshackle should set the terminal background color. Default: `false`

For example,

```yaml
set_terminal_bg: true
```

---

## update_checks (bool)

Check for updates from the GitHub repository on startup. Default: `true`.

---

## update_check_interval (int)

How often to check for updates, in hours. Default: `24`.

---

## title_cache_enabled (bool)

Enable or disable title metadata caching globally. Default: `true`.

---

## title_cache_time (int)

Title cache duration in seconds. Default: `1800` (30 minutes).

---

## title_cache_max_retention (int)

Maximum cache retention in seconds, used as fallback when the upstream API fails. Default: `86400` (24 hours).

---

## unicode_filenames (bool)

When `false`, replaces non-ASCII characters in output filenames with ASCII equivalents. Default: `false`.

---

## ipinfo_api_key (str)

Optional ipinfo.io token. When set, unshackle uses the ipinfo.io Lite endpoint for IP/geolocation lookups instead of the unauthenticated fallback.

---

## tmdb_api_key (str)

Optional TMDB API key, used for metadata enrichment and IMDb/TMDb tagging.

---

## simkl_client_id (str)

Optional Simkl client ID for metadata lookups.

---

## decrypt_labs_api_key (str)

Optional Decrypt Labs API key, used by services that integrate with the service.

---
