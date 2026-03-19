# DRM & CDM Configuration

This document covers Digital Rights Management (DRM) and Content Decryption Module (CDM) configuration options.

## cdm (dict)

Pre-define which Widevine or PlayReady device to use for each Service by Service Tag as Key (case-sensitive).
The value should be a WVD or PRD filename without the file extension. When
loading the device, unshackle will look in both the `WVDs` and `PRDs` directories
for a matching file.

For example,

```yaml
EXAMPLE: chromecdm_903_l3
EXAMPLE2: nexus_6_l1
```

You may also specify this device based on the profile used.

For example,

```yaml
EXAMPLE: chromecdm_903_l3
EXAMPLE2: nexus_6_l1
EXAMPLE3:
  john_sd: chromecdm_903_l3
  jane_uhd: nexus_5_l1
```

You can also specify a fallback value to predefine if a match was not made.
This can be done using `default` key. This can help reduce redundancy in your specifications.

For example, the following has the same result as the previous example, as well as all other
services and profiles being pre-defined to use `chromecdm_903_l3`.

```yaml
EXAMPLE2: nexus_6_l1
EXAMPLE3:
  jane_uhd: nexus_5_l1
default: chromecdm_903_l3
```

You can also select CDMs based on video resolution using comparison operators (`>=`, `>`, `<=`, `<`)
or exact match on the resolution height.

For example,

```yaml
EXAMPLE:
  "<=1080": generic_android_l3   # Use L3 for 1080p and below
  ">1080": nexus_5_l1            # Use L1 for above 1080p (1440p, 2160p)
  default: generic_android_l3    # Fallback if no quality match
```

You can mix profiles and quality thresholds in the same service:

```yaml
EXAMPLE:
  john: example_l3_profile       # Profile-based selection
  "<=720": example_mobile_l3     # Quality-based selection
  "1080": example_standard_l3    # Exact match for 1080p
  ">=1440": example_premium_l1   # Quality-based selection
  default: example_standard_l3   # Fallback
```

---

## remote_cdm (list\[dict])

Configure remote CDM (Content Decryption Module) APIs to use for decrypting DRM-protected content.
Remote CDMs allow you to use high-security CDMs (L1/L2 for Widevine, SL2000/SL3000 for PlayReady) without
having the physical device files locally.

unshackle supports multiple types of remote CDM providers:

1. **DecryptLabs CDM** - Official DecryptLabs KeyXtractor API with intelligent caching
2. **Custom API CDM** - Highly configurable adapter for any third-party CDM API
3. **Legacy PyWidevine Serve** - Standard pywidevine serve-compliant APIs

The name of each defined remote CDM can be referenced in the `cdm` configuration as if it was a local device file.

### DecryptLabs Remote CDM

DecryptLabs provides a professional CDM API service with support for multiple device types and intelligent key caching.

**Supported Devices:**
- **Widevine**: `ChromeCDM` (L3), `L1` (Security Level 1), `L2` (Security Level 2)
- **PlayReady**: `SL2` (SL2000), `SL3` (SL3000)

**Configuration:**

```yaml
remote_cdm:
  # Widevine L1 Device
  - name: decrypt_labs_l1
    type: decrypt_labs              # Required: identifies as DecryptLabs CDM
    device_name: L1                 # Required: must match exactly (L1, L2, ChromeCDM, SL2, SL3)
    host: https://keyxtractor.decryptlabs.com
    secret: YOUR_API_KEY            # Your DecryptLabs API key

  # Widevine L2 Device
  - name: decrypt_labs_l2
    type: decrypt_labs
    device_name: L2
    host: https://keyxtractor.decryptlabs.com
    secret: YOUR_API_KEY

  # Chrome CDM (L3)
  - name: decrypt_labs_chrome
    type: decrypt_labs
    device_name: ChromeCDM
    host: https://keyxtractor.decryptlabs.com
    secret: YOUR_API_KEY

  # PlayReady SL2000
  - name: decrypt_labs_playready_sl2
    type: decrypt_labs
    device_name: SL2
    device_type: PLAYREADY          # Required for PlayReady
    host: https://keyxtractor.decryptlabs.com
    secret: YOUR_API_KEY

  # PlayReady SL3000
  - name: decrypt_labs_playready_sl3
    type: decrypt_labs
    device_name: SL3
    device_type: PLAYREADY
    host: https://keyxtractor.decryptlabs.com
    secret: YOUR_API_KEY
```

**Features:**
- Intelligent key caching system (reduces API calls)
- Automatic integration with unshackle's vault system
- Support for both Widevine and PlayReady
- Multiple security levels (L1, L2, L3, SL2000, SL3000)

**Note:** The `device_type` field determines whether the CDM operates in PlayReady or Widevine mode.
Setting `device_type: PLAYREADY` (or using `device_name: SL2` / `SL3`) activates PlayReady mode.
The `security_level` field is auto-computed from `device_name` when not specified (e.g., SL2 defaults
to 2000, SL3 to 3000, and Widevine devices default to 3). You can override these if needed.

### Custom API Remote CDM

A highly configurable CDM adapter that can work with virtually any third-party CDM API through YAML configuration.
This allows you to integrate custom CDM services without writing code.

**Basic Example:**

```yaml
remote_cdm:
  - name: custom_chrome_cdm
    type: custom_api                # Required: identifies as Custom API CDM
    host: https://your-cdm-api.com
    timeout: 30                     # Optional: request timeout in seconds

    device:
      name: ChromeCDM
      type: CHROME                  # CHROME, ANDROID, PLAYREADY
      system_id: 27175
      security_level: 3

    auth:
      type: bearer                  # bearer, header, basic, body
      key: YOUR_API_TOKEN

    endpoints:
      get_request:
        path: /get-challenge
        method: POST
      decrypt_response:
        path: /get-keys
        method: POST

    caching:
      enabled: true                 # Enable key caching
      use_vaults: true              # Integrate with vault system
```

**Advanced Example with Field Mapping:**

```yaml
remote_cdm:
  - name: advanced_custom_api
    type: custom_api
    host: https://api.example.com
    device:
      name: L1
      type: ANDROID
      security_level: 1

    # Authentication configuration
    auth:
      type: header
      header_name: X-API-Key
      key: YOUR_SECRET_KEY
      custom_headers:
        User-Agent: Unshackle/3.1.0
        X-Client-Version: "1.0"

    # Endpoint configuration
    endpoints:
      get_request:
        path: /v2/challenge
        method: POST
        timeout: 30
      decrypt_response:
        path: /v2/decrypt
        method: POST
        timeout: 30

    # Request parameter mapping
    request_mapping:
      get_request:
        param_names:
          init_data: pssh           # Rename 'init_data' to 'pssh'
          scheme: device_type       # Rename 'scheme' to 'device_type'
        static_params:
          api_version: "2.0"        # Add static parameter
      decrypt_response:
        param_names:
          license_request: challenge
          license_response: license

    # Response field mapping
    response_mapping:
      get_request:
        fields:
          challenge: data.challenge # Deep field access
          session_id: session.id
        success_conditions:
          - status == 'ok'          # Validate response
      decrypt_response:
        fields:
          keys: data.keys
        key_fields:
          kid: key_id               # Map 'kid' field
          key: content_key          # Map 'key' field

    caching:
      enabled: true
      use_vaults: true
      check_cached_first: true      # Check cache before API calls
```

**Supported Authentication Types:**
- `bearer` - Bearer token authentication
- `header` - Custom header authentication
- `basic` - HTTP Basic authentication
- `body` - Credentials in request body
- `query` - Authentication added to query string parameters

### Legacy PyWidevine Serve Format

Standard [pywidevine] serve-compliant remote CDM configuration (backwards compatibility).

```yaml
remote_cdm:
  - name: legacy_chrome_cdm
    device_name: chrome
    device_type: CHROME
    system_id: 27175
    security_level: 3
    host: https://domain.com/api
    secret: secret_key
```

**Note:** If the `type` field is not specified, the entry is treated as a legacy pywidevine serve CDM.

[pywidevine]: https://github.com/rlaphoenix/pywidevine

---

## decrypt_labs_api_key (str)

API key for DecryptLabs CDM service integration.

When set, enables the use of DecryptLabs remote CDM services in your `remote_cdm` configuration.
This is used specifically for `type: "decrypt_labs"` entries in the remote CDM list.

For example,

```yaml
decrypt_labs_api_key: "your_api_key_here"
```

**Note**: This is different from the per-CDM `secret` field in `remote_cdm` entries. This provides a global
API key that can be referenced across multiple DecryptLabs CDM configurations. If a `remote_cdm` entry with
`type: "decrypt_labs"` does not have a `secret` field specified, the global `decrypt_labs_api_key` will be
used as a fallback.

---

## decryption (str|dict)

Configure which decryption tool to use for DRM-protected content. Default: `shaka`.

Supported values:
- `shaka` - Shaka Packager (default)
- `mp4decrypt` - Bento4 mp4decrypt

You can specify a single decrypter for all services:

```yaml
decryption: shaka
```

Or configure per-service with a `DEFAULT` fallback:

```yaml
decryption:
  DEFAULT: shaka
  EXAMPLE: mp4decrypt
  EXAMPLE2: shaka
```

Service keys are case-insensitive (normalized to uppercase internally).

---

## MonaLisa DRM

MonaLisa is a WASM-based DRM system that uses local key extraction and two-stage segment decryption.
Unlike Widevine and PlayReady, MonaLisa does not use a challenge/response flow with a license server.
Instead, the PSSH value (ticket) is provided directly by the service API, and keys are extracted
locally via a WASM module.

### Requirements

- **ML-Worker binary**: Must be available on your system `PATH` (discovered via `binaries.ML_Worker`).
  This is the binary that performs stage-1 decryption.

### Decryption stages

1. **ML-Worker binary**: Removes MonaLisa encryption layer (bbts -> ents). The key is passed via command-line argument.
2. **AES-ECB decryption**: Final decryption with service-provided key.

MonaLisa uses per-segment decryption during download (not post-download like Widevine/PlayReady),
so segments are decrypted as they are downloaded.

**Note:** MonaLisa is configured per-service rather than through global config options. Services
that use MonaLisa handle ticket/key retrieval and CDM initialization internally.

---

## key_vaults (list\[dict])

Key Vaults store your obtained Content Encryption Keys (CEKs) and Key IDs per-service.

This can help reduce unnecessary License calls even during the first download. This is because a Service may
provide the same Key ID and CEK for both Video and Audio, as well as for multiple resolutions or bitrates.

You can have as many Key Vaults as you would like. It's nice to share Key Vaults or use a unified Vault on
Teams as sharing CEKs immediately can help reduce License calls drastically.

Four types of Vaults are in the Core codebase: API, SQLite, MySQL, and HTTP. API and HTTP make HTTP requests to a RESTful API,
whereas SQLite and MySQL directly connect to an SQLite or MySQL Database.

Note: SQLite and MySQL vaults have to connect directly to the Host/IP. It cannot be in front of a PHP API or such.
Beware that some Hosting Providers do not let you access the MySQL server outside their intranet and may not be
accessible outside their hosting platform.

Additional behavior:

- `no_push` (bool): Optional per-vault flag. When `true`, the vault will not receive pushed keys (writes) but
  will still be queried and can provide keys for lookups. Useful for read-only/backup vaults.

### Using an API Vault

API vaults use a specific HTTP request format, therefore API or HTTP Key Vault APIs from other projects or services may
not work in unshackle. The API format can be seen in the [API Vault Code](unshackle/vaults/API.py).

```yaml
- type: API
  name: "John#0001's Vault" # arbitrary vault name
  uri: "https://key-vault.example.com" # api base uri (can also be an IP or IP:Port)
  # uri: "127.0.0.1:80/key-vault"
  # uri: "https://api.example.com/key-vault"
  token: "random secret key" # authorization token
  # no_push: true            # optional; make this API vault read-only (lookups only)
```

### Using a MySQL Vault

MySQL vaults can be either MySQL or MariaDB servers. I recommend MariaDB.
A MySQL Vault can be on a local or remote network, but I recommend SQLite for local Vaults.

```yaml
- type: MySQL
  name: "John#0001's Vault" # arbitrary vault name
  host: "127.0.0.1" # host/ip
  # port: 3306               # port (defaults to 3306)
  database: vault # database used for unshackle
  username: jane11
  password: Doe123
  # no_push: false           # optional; defaults to false
```

I recommend giving only a trustable user (or yourself) CREATE permission and then use unshackle to cache at least one CEK
per Service to have it create the tables. If you don't give any user permissions to create tables, you will need to
make tables yourself.

- Use a password on all user accounts.
- Never use the root account with unshackle (even if it's you).
- Do not give multiple users the same username and/or password.
- Only give users access to the database used for unshackle.
- You may give trusted users CREATE permission so unshackle can create tables if needed.
- Other uses should only be given SELECT and INSERT permissions.

### Using an SQLite Vault

SQLite Vaults are usually only used for locally stored vaults. This vault may be stored on a mounted Cloud storage
drive, but I recommend using SQLite exclusively as an offline-only vault. Effectively this is your backup vault in
case something happens to your MySQL Vault.

```yaml
- type: SQLite
  name: "My Local Vault" # arbitrary vault name
  path: "C:/Users/Jane11/Documents/unshackle/data/key_vault.db"
  # no_push: true           # optional; commonly true for local backup vaults
```

**Note**: You do not need to create the file at the specified path.
SQLite will create a new SQLite database at that path if one does not exist.
Try not to accidentally move the `db` file once created without reflecting the change in the config, or you will end
up with multiple databases.

If you work on a Team I recommend every team member having their own SQLite Vault even if you all use a MySQL vault
together.

### Using an HTTP Vault

HTTP Vaults provide flexible HTTP-based key storage with support for multiple API modes. This vault type
is useful for integrating with various third-party key vault APIs.

```yaml
- type: HTTP
  name: "My HTTP Vault"
  host: "https://vault-api.example.com"
  api_key: "your_api_key"       # or use 'password' field
  api_mode: "json"              # query, json, or decrypt_labs
  # username: "user"            # required for query mode only
  # no_push: false              # optional; defaults to false
```

**Supported API Modes:**

- `query` - Uses GET requests with query parameters. Requires `username` field.
- `json` - Uses POST requests with JSON payloads. Token-based authentication.
- `decrypt_labs` - DecryptLabs API format. Read-only mode (`no_push` is forced to `true`).

**Example configurations:**

```yaml
# Query mode (requires username)
- type: HTTP
  name: "Query Vault"
  host: "https://api.example.com/keys"
  username: "myuser"
  password: "mypassword"
  api_mode: "query"

# JSON mode
- type: HTTP
  name: "JSON Vault"
  host: "https://api.example.com/vault"
  api_key: "secret_token"
  api_mode: "json"

# DecryptLabs mode (read-only)
- type: HTTP
  name: "DecryptLabs Cache"
  host: "https://keyxtractor.decryptlabs.com/cache"
  api_key: "your_decrypt_labs_api_key"
  api_mode: "decrypt_labs"
```

**Note**: The `decrypt_labs` mode is always read-only and cannot receive pushed keys.

---
