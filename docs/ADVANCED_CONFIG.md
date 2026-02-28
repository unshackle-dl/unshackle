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

### Configuration

- `api_secret` - Secret key for REST API authentication. Required unless `--no-key` is used. All API requests must include this key via the `X-API-Key` header or `api_key` query parameter.
- `devices` - List of Widevine device files (.wvd). If not specified, auto-populated from the WVDs directory.
- `playready_devices` - List of PlayReady device files (.prd). If not specified, auto-populated from the PRDs directory.
- `users` - Dictionary mapping user secret keys to their access configuration:
  - `devices` - List of Widevine devices this user can access
  - `playready_devices` - List of PlayReady devices this user can access
  - `username` - Internal logging name for the user (not visible to users)

For example,

```yaml
serve:
  api_secret: "your-secret-key-here"
  users:
    secret_key_for_jane: # 32bit hex recommended, case-sensitive
      devices: # list of allowed Widevine devices for this user
        - generic_nexus_4464_l3
      playready_devices: # list of allowed PlayReady devices for this user
        - my_playready_device
      username: jane # only for internal logging, users will not see this name
    secret_key_for_james:
      devices:
        - generic_nexus_4464_l3
      username: james
    secret_key_for_john:
      devices:
        - generic_nexus_4464_l3
      username: john
  # devices can be manually specified by path if you don't want to add it to
  # unshackle's WVDs directory for whatever reason
  # devices:
  #   - 'C:\Users\john\Devices\test_devices_001.wvd'
```

### REST API

When the server is running, interactive API documentation is available at:

- **Swagger UI**: `http://localhost:8786/api/docs/`

See [API.md](API.md) for full REST API documentation with endpoints, parameters, and examples.

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
