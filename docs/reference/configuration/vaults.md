# Key vaults { #key-vaults }

The full guide is at [Vaults](../../guide/vaults.md). Two keys configure them.

## `key_vaults`

- **Type:** `list[dict]` &nbsp;·&nbsp; **Default:** `[]`

An ordered list of key-vault backends. unshackle queries them in order and reuses content
keys instead of re-licensing. Each entry needs a `type` (the backend module name) and a
`name`, plus backend-specific keys.

| `type` | Purpose | Required keys | Notes |
|--------|---------|--------------|-------|
| `SQLite` | Local SQLite database | `name`, `path` | Loaded **critically**; a failure aborts the run. |
| `MySQL` | Remote MySQL database | `name`, `host`, `database`, `username` | Extra keys (e.g. `password`, `port`) forwarded to pymysql. |
| `API` | RESTful JSON API | `name`, `uri`, `token` | Optional `headers` map is sent with every request. Honours `vault_timeout`. |
| `HTTP` | HTTP API with modes | `name`, `host`, one of `password`/`api_key`, and `username` in query mode | `api_mode`: `query` (default), `json`, `decrypt_labs`. Honours `vault_timeout`. |

```yaml
key_vaults:
  - type: SQLite
    name: Local
    path: ~/.unshackle/keys.db
  - type: MySQL
    name: Team
    host: db.example.com
    database: keys
    username: unshackle
    password: hunter2
    no_push: false
```

!!! note "Per-entry options"
    - `no_push: true` makes a vault read-only (keys are fetched but never written to it).
    - A vault of `type: API` whose `name` contains `decrypt_labs` auto-fills its `token` from
      [`decrypt_labs_api_key`](misc.md#external-api-keys) when not set inline. Vault `type` values are
      case-sensitive module names.
    - An all-zero content key (32 zeros) is treated as "no key" everywhere and is never
      stored.

## `vault_timeout`

- **Type:** `float` &nbsp;·&nbsp; **Default:** `10.0`

Timeout in seconds for vault operations. Injected automatically into any backend whose
constructor accepts a `timeout` parameter (a per-vault `timeout` still wins).
