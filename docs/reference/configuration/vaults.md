# Key vaults { #key-vaults }

The full guide is at [Vaults](../../guide/vaults.md). Two keys configure them.

## `key_vaults`

- **Type:** `list[dict]` &nbsp;·&nbsp; **Default:** `[]`

An ordered list of key-vault backends. unshackle queries them in order and reuses the
content keys instead of licensing them again. Each entry needs a `type` (the backend module
name) and a `name`, plus backend-specific keys.

| `type` | Purpose | Required keys | Notes |
|--------|---------|--------------|-------|
| `SQLite` | Local SQLite database | `name`, `path` | Loaded **critically**; a failure aborts the run. |
| `MySQL` | Remote MySQL database | `name`, `host`, `database`, `username` | Extra keys (e.g. `password`, `port`) forwarded to pymysql. `connect_timeout` defaults to `vault_timeout`. |
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
    - `no_push: true` makes a vault read-only (unshackle fetches content keys from it but never writes to it).
    - A vault of `type: API` whose `name` contains `decrypt_labs` auto-fills its `token` from
      [`decrypt_labs_api_key`](misc.md#external-api-keys) when not set inline. Vault `type` values are
      case-sensitive module names.
    - unshackle treats an all-zero content key (32 zeros) as "no key" everywhere and never
      stores it.

!!! note "Bad content keys"
    A content key from a vault is proven by a short FFmpeg decode of the decrypted output.
    A content key that fails the decode is written to the `bad_keys` table of every `SQLite`
    vault with the name of the vault that supplied it, and every vault lookup skips a flagged
    pair from then on. Only the `SQLite` backend stores flags. A `serve` instance with
    `server_cdm` flags a pair the same way when a remote client reports it, so a server whose
    `key_vaults` hold no `SQLite` entry cannot remember a bad content key.

## `vault_timeout`

- **Type:** `float` &nbsp;·&nbsp; **Default:** `10.0`

Timeout in seconds for vault operations. Injected automatically into any backend whose
constructor accepts a `timeout` parameter (a per-vault `timeout` still wins). The `MySQL` backend
uses it as the default `connect_timeout`.
