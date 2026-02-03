# Advanced & System Configuration

This document covers advanced features, debugging, and system-level configuration options.

## serve (dict)

Configuration data for pywidevine's serve functionality run through unshackle.
This effectively allows you to run `unshackle serve` to start serving pywidevine Serve-compliant CDMs right from your
local widevine device files.

- `api_secret` - Secret key for REST API authentication. When set, enables the REST API server alongside the CDM serve functionality. This key is required for authenticating API requests.
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
