# Advanced & System Configuration

This document covers advanced features, debugging, and system-level configuration options.

## serve (dict)

Configuration data for pywidevine's serve functionality run through unshackle.
This effectively allows you to run `unshackle serve` to start serving pywidevine Serve-compliant CDMs right from your
local widevine device files.

- `api_secret` - Secret key for REST API authentication. When set, enables the REST API server alongside the CDM serve functionality. This key is required for authenticating API requests.

For example,

```yaml
api_secret: "your-secret-key-here"
users:
  secret_key_for_jane: # 32bit hex recommended, case-sensitive
    devices: # list of allowed devices for this user
      - generic_nexus_4464_l3
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

## debug

**Description:** Debug logging configuration

**Note:** This configuration option was not found in the original CONFIG.md documentation. If this feature exists, please refer to the service documentation or contact the maintainers for configuration details.

---

## debug_keys

**Description:** Key logging control and debug key output

**Note:** This configuration option was not found in the original CONFIG.md documentation. If this feature exists, please refer to the service documentation or contact the maintainers for configuration details.

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
