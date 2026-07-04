# Key Vaults

A **key vault** is a store of DRM **content keys** that unshackle remembers between downloads. Once a title's keys have been recovered, unshackle saves them to your configured vaults. The next time you download something that reuses those same keys, unshackle finds them in a vault instead of performing another license exchange. The download starts faster and puts less load on the service.

Vaults are entirely optional, but if you download regularly they are one of the biggest quality-of-life improvements you can configure.

!!! tip "Vaults save license calls even on the very first download"
    The benefit is not limited to repeat downloads. Within a single run, a service often issues the *same* Key ID and content key for both the video and audio tracks, and for several resolutions or bitrates of the same title. Once unshackle has recovered a key it writes it to your vaults immediately, so the moment the next track presents a KID it has already seen, the key is served from the vault instead of triggering another license exchange. A single fetched key can therefore satisfy many tracks in one run.

## What a vault stores

Every key is stored as a **KID → KEY** pair, grouped by **service**:

- **KID** (Key ID): a 16-byte identifier for a specific encrypted stream, stored as 32 lowercase hex characters with no dashes.
- **KEY**: the 16-byte content key that decrypts that stream, also 32 hex characters.
- **Service**: the canonical service tag (for example `DSNP`, not `DisneyPlus`) that namespaces the key. Each service gets its own logical table.

!!! note "Why keys are matched by KID"
    Vaults match keys by KID, never by PSSH/CENC header. A KID does not change unless the underlying video file itself changes, whereas the PSSH box can vary between requests for the very same content. That makes the KID a far more reliable match key.

!!! info "Null keys are ignored everywhere"
    A key made entirely of zeroes (`00000000000000000000000000000000`) is treated as "no key". Vault lookups skip it, and every backend raises an error if you try to add one. This prevents a placeholder key from masking a real one.

## How lookups and caching work

When unshackle needs a key during a download it asks your vaults **in the order they are listed in your config**. It returns the first non-null key it finds and stops searching. If a vault raises a permission error or is unreachable, that vault is skipped and the search continues with the next one.

When new keys are recovered from a license, unshackle pushes them to **all** configured vaults, so every vault gradually converges on the same set of keys. Two rules apply to pushing:

- Keys that already exist in a vault are skipped (existing data is never overwritten or deleted).
- A vault marked `no_push: true` receives lookups but is never written to. This is useful for read-only or shared upstream vaults you do not own.

!!! tip "A good two-vault setup"
    A common arrangement is a fast local **SQLite** vault plus a shared remote vault (**MySQL**, **HTTP**, or **API**). The local vault answers instantly and works offline; the remote vault lets you share keys across machines or with a group.

## Configuring vaults

Vaults are declared under the `key_vaults` list in your unshackle config file. Each entry is a mapping with two required fields, `type` (which backend to use) and `name` (a label you choose), plus whatever settings that backend needs.

```yaml title="unshackle.yaml"
key_vaults:
  - type: SQLite
    name: local
    path: ~/.local/share/unshackle/vaults/local.db
  - type: MySQL
    name: shared
    host: vault.example.com
    database: unshackle
    username: reader
    password: "s3cret"
```

The `type` value is matched to a vault backend by name. The four backends that ship with unshackle are `SQLite`, `MySQL`, `HTTP`, and `API`, described below.

!!! warning "SQLite vaults are loaded critically"
    During a download, a **SQLite** vault that fails to load will **abort the run**. The assumption is that a local database you configured should always be available. `MySQL`, `HTTP`, and `API` vaults are loaded leniently: if one fails to load it is logged and skipped, and the download continues with the remaining vaults.

### `vault_timeout`

Network-backed vaults (`HTTP` and `API`) accept an optional per-vault `timeout` in seconds. If you omit it, unshackle injects the global default:

```yaml title="unshackle.yaml"
vault_timeout: 10.0   # default, in seconds
```

A `timeout` set inline on an individual vault always wins over this global value. The `SQLite` and `MySQL` backends do not take a `timeout` field.

---

## SQLite backend

A locally-accessed SQLite database file. This is the simplest and fastest option and needs no server.

```yaml title="unshackle.yaml"
key_vaults:
  - type: SQLite
    name: local
    path: ~/.local/share/unshackle/vaults/local.db
```

| Field | Required | Description |
|-------|----------|-------------|
| `type` | yes | Must be `SQLite`. |
| `name` | yes | A label for this vault. |
| `path` | yes | Path to the database file. `~` is expanded. The file (and its tables) are created automatically on first use. |
| `no_push` | no | If `true`, keys are read but never written. Defaults to `false`. |

Each service gets its own table (named after the service tag), created on demand with `kid` and `key_` columns. Connections use WAL journaling with a 30-second busy timeout, so the same database file can be used safely from multiple threads.

!!! warning "Moving the .db file silently forks your vault"
    Because the file is created on demand, if you relocate an existing `.db` without updating `path` to match, unshackle will simply create a fresh empty database at the old location, leaving you with two divergent vaults and no error to warn you. Always update the config path whenever you move the file.

!!! tip "Keep a local SQLite vault even when you share a MySQL one"
    The SQLite vault is best thought of as an offline-only backup should anything ever happen to a shared `MySQL` vault. Each member of a group should keep their own local SQLite vault alongside the shared database, so no one loses their keys if the shared vault goes down or away.

---

## MySQL backend

A remotely-accessed MySQL/MariaDB database, ideal for sharing keys across several machines. It connects via `pymysql`. MariaDB is recommended over MySQL where you have the choice.

!!! warning "The vault connects directly to the database server"
    A `MySQL` vault (like `SQLite`) speaks the database wire protocol directly to the host or IP you configure. It cannot sit behind a PHP API or any other application layer. A common real-world snag: many hosting providers firewall their MySQL server to their own internal network, so a database on shared hosting is frequently unreachable from the outside world even when your credentials are perfectly correct. If connections time out, confirm the server accepts remote connections from your address before suspecting the config.

```yaml title="unshackle.yaml"
key_vaults:
  - type: MySQL
    name: shared
    host: vault.example.com
    database: unshackle
    username: unshackle
    password: "your-password"
    port: 3306
```

| Field | Required | Description |
|-------|----------|-------------|
| `type` | yes | Must be `MySQL`. |
| `name` | yes | A label for this vault. |
| `host` | yes | Database server hostname or IP. |
| `database` | yes | Database (schema) name. |
| `username` | yes | Login user. |
| `no_push` | no | If `true`, keys are read but never written. |

Any additional fields you provide (such as `password`, `port`, or `ssl`) are passed straight through to `pymysql.connect`, so you can use the full range of its connection options.

!!! warning "Account security is on you, not on unshackle"
    unshackle enforces none of this. It is operational policy for a database that may be shared with a group. Never connect with the `root` account, not even for yourself. Give every account a password, and never let two users share a username/password. Restrict each account's access to only the `unshackle` database, and grant normal users just `SELECT` and `INSERT`. Reserve the `CREATE` grant for a single trusted user who bootstraps the service tables (see [`kv prepare`](#kv-prepare-pre-create-service-tables)); everyone else can insert new keys but should not be creating tables.

!!! note "Grants determine what the vault can do"
    On connection the backend reads its own `SHOW GRANTS`. It requires at least `SELECT` to load at all (a missing `SELECT` grant makes the vault fail to load). Writing keys additionally needs `INSERT`, and creating a new service table needs `CREATE`. A read-only account still works fine as a lookup source. It simply cannot push new keys. Because `MySQL` is loaded leniently during downloads, a permission problem will not abort the run.

---

## HTTP backend

A flexible HTTP vault that speaks one of three wire protocols, selected with `api_mode`. Use this to integrate with a community or self-hosted vault service.

| `api_mode` | Transport | Authentication | Notes |
|------------|-----------|----------------|-------|
| `query` (default) | `GET` with query parameters | `username` + `password` | Full read and write. |
| `json` | `POST` with a JSON payload | `password` used as a token | Read and write per key; cannot bulk-enumerate keys. |
| `decrypt_labs` | `POST` in DecryptLabs format | `api_key` sent as a header | **Read-only**. `no_push` is forced on. |

=== "query mode"

    ```yaml title="unshackle.yaml"
    key_vaults:
      - type: HTTP
        name: community
        host: https://vault.example.com/api
        api_mode: query
        username: myuser
        password: "your-password"
    ```

    `username` is required in query mode. Both the username and password are sent as query parameters on each request.

=== "json mode"

    ```yaml title="unshackle.yaml"
    key_vaults:
      - type: HTTP
        name: jsonvault
        host: https://vault.example.com/rpc
        api_mode: json
        password: "your-api-token"
    ```

    Requests are `POST`ed as `{"method": ..., "params": ..., "token": ...}` using methods like `GetKey`, `InsertKey`, and `GetServices`. This mode cannot list all keys in bulk, so a `kv copy` from a json vault relies on the remote server's own duplicate handling.

=== "decrypt_labs mode"

    ```yaml title="unshackle.yaml"
    key_vaults:
      - type: HTTP
        name: decrypt_labs
        host: https://api.decryptlabs.example/
        api_mode: decrypt_labs
        api_key: "your-decrypt-labs-key"
    ```

    A read-only lookup source. The key is sent in a `decrypt-labs-api-key` header and the vault refuses all writes.

| Field | Required | Description |
|-------|----------|-------------|
| `type` | yes | Must be `HTTP`. |
| `name` | yes | A label for this vault. |
| `host` | yes | Base URL of the vault API. |
| `api_mode` | no | `query` (default), `json`, or `decrypt_labs`. |
| `username` | for `query` | Required in query mode; ignored otherwise. |
| `password` | one of these | Password (query mode) or token (json mode). |
| `api_key` | one of these | Alternative to `password`; used for `decrypt_labs`. If both are given, `api_key` wins. |
| `no_push` | no | Read-only when `true` (always `true` for `decrypt_labs`). |
| `timeout` | no | Request timeout in seconds; defaults to `vault_timeout`. |

You must supply either `password` or `api_key`, or the vault will fail to load.

---

## API backend

A simple RESTful HTTP vault with a fixed, well-defined protocol. Authentication is a bearer token.

```yaml title="unshackle.yaml"
key_vaults:
  - type: API
    name: myapi
    uri: https://vault.example.com/api
    token: "your-bearer-token"
```

| Field | Required | Description |
|-------|----------|-------------|
| `type` | yes | Must be `API`. |
| `name` | yes | A label for this vault. |
| `uri` | yes | Base URI of the vault API (a trailing slash is trimmed). |
| `token` | yes | Bearer token, sent as `Authorization: Bearer <token>`. |
| `no_push` | no | If `true`, keys are read but never written. |
| `timeout` | no | Request timeout in seconds; defaults to `vault_timeout`. |

The backend talks to endpoints under `uri` (for example `GET {uri}/{service}/{kid}` to look up a single key) and interprets a numeric `code` field in each JSON response to detect errors such as an invalid token, rate limiting, or an invalid service tag.

!!! warning "This is unshackle's own protocol, not a generic vault client"
    The `API` backend expects unshackle's specific HTTP request and response shape. An API or HTTP key-vault endpoint built for a different project or service will generally *not* be compatible. You cannot simply point this at an arbitrary third-party vault API and expect it to work. To bridge another service, put an adapter in front of it that speaks this format, or use the `HTTP` backend if one of its wire protocols matches. When pushing many keys at once it batches requests and automatically shrinks the batch size if the server rejects an over-large request.

!!! tip "DecryptLabs shortcut"
    An `API` vault whose `name` contains `decrypt_labs` will have its `token` filled in automatically from a global `decrypt_labs_api_key` in your config if you leave `token` unset. This lets you keep the key in one place.

---

## Managing vaults with `unshackle kv`

The `kv` command group manages the contents of your configured vaults. Every subcommand refers to vaults **by the `name` you gave them** in `key_vaults`; an unknown name is an error.

```console
$ unshackle kv --help
```

### `kv add`: import keys from a file

Add `KID:KEY` pairs to one or more vaults for a given service. The file must contain one pair per line as `HEX:HEX` (32 hex characters each); lines that do not match are ignored.

```console
$ unshackle kv add keys.txt DSNP local shared
```

```text title="keys.txt"
9a04f07998404286ab92e65be0885f95:b1c2d3e4f5a6978012345678deadbeef
0e7cc1a4e2f9411db0f24a3fbb8f8b21:00112233445566778899aabbccddeeff
```

The service argument (`DSNP` above) is normalized to its canonical tag automatically. Keys that already exist in a vault are reported as skipped.

### `kv search`: find a key by KID

Search your vaults for a KID and print any matching key.

```console
$ unshackle kv search 9a04f07998404286ab92e65be0885f95
```

The KID must be 32 hex characters (dashes are stripped for you). By default every configured vault and every service table is scanned; narrow the search with options:

| Option | Description |
|--------|-------------|
| `-s`, `--service` | Limit the search to a single service tag. |
| `-v`, `--vault` | Limit the search to one configured vault by name. |

!!! note
    Remote vaults that cannot enumerate their service tables (for example an `HTTP` vault in `json` or `decrypt_labs` mode) can only be searched when you also pass `--service`. Without it, they are skipped.

### `kv copy`: copy keys between vaults

Copy every key from one or more source vaults into a single destination vault. Existing rows are never altered; only genuinely new KIDs are added, and null keys are skipped.

```console
$ unshackle kv copy shared local
```

The first argument is the destination; the rest are sources. You can supply several sources at once:

```console
$ unshackle kv copy local shared community
```

| Option | Description |
|--------|-------------|
| `-s`, `--service` | Only copy the given service. |
| `-l`, `--local-only` | Only copy services you have installed locally, skipping tables for unknown services. |

`--service` and `--local-only` are mutually exclusive.

### `kv sync`: mirror vaults both ways

Ensure two or more vaults each end up with every key the others have. This is effectively a two-way `copy` between each pair, so afterwards all listed vaults hold the same set of keys.

```console
$ unshackle kv sync local shared
```

It accepts the same `--service` and `--local-only` options as `copy`, and requires at least two vaults.

### `kv prepare`: pre-create service tables

Create the per-service tables on table-based vaults (`SQLite`, `MySQL`) ahead of time, for every service you have installed. Vaults that do not use tables (the network-backed `HTTP` and `API` backends) are skipped.

```console
$ unshackle kv prepare shared
```

This is mainly useful with a `MySQL` vault where you want the tables created once by an account that has the `CREATE` grant.

---

## See also

- [Downloading](downloading.md): how keys are recovered and used during a download.
