# DRM & CDM Configuration

This document covers Digital Rights Management (DRM) and Content Decryption Module (CDM) configuration options.

## cdm (dict)

Pre-define which Widevine or PlayReady device to use for each Service by Service Tag as Key (case-sensitive).
The value should be a WVD or PRD filename without the file extension. When
loading the device, unshackle will look in both the `WVDs` and `PRDs` directories
for a matching file.

For example,

```yaml
AMZN: chromecdm_903_l3
NF: nexus_6_l1
```

You may also specify this device based on the profile used.

For example,

```yaml
AMZN: chromecdm_903_l3
NF: nexus_6_l1
DSNP:
  john_sd: chromecdm_903_l3
  jane_uhd: nexus_5_l1
```

You can also specify a fallback value to predefine if a match was not made.
This can be done using `default` key. This can help reduce redundancy in your specifications.

For example, the following has the same result as the previous example, as well as all other
services and profiles being pre-defined to use `chromecdm_903_l3`.

```yaml
NF: nexus_6_l1
DSNP:
  jane_uhd: nexus_5_l1
default: chromecdm_903_l3
```

---

## remote_cdm (list\[dict])

Use [pywidevine] Serve-compliant Remote CDMs in unshackle as if it was a local widevine device file.
The name of each defined device maps as if it was a local device and should be used like a local device.

For example,

```yaml
- name: chromecdm_903_l3 # name must be unique for each remote CDM
  # the device type, system id and security level must match the values of the device on the API
  # if any of the information is wrong, it will raise an error, if you do not know it ask the API owner
  device_type: CHROME
  system_id: 1234
  security_level: 3
  host: "http://xxxxxxxxxxxxxxxx/the_cdm_endpoint"
  secret: "secret/api key"
  device_name: "remote device to use" # the device name from the API, usually a wvd filename
```

[pywidevine]: https://github.com/rlaphoenix/pywidevine

---

## decrypt_labs_api_key

**Description:** DecryptLabs global API key configuration

**Note:** This configuration option was not found in the original CONFIG.md documentation. If this feature exists, please refer to the service documentation or contact the maintainers for configuration details.

---

## key_vaults (list\[dict])

Key Vaults store your obtained Content Encryption Keys (CEKs) and Key IDs per-service.

This can help reduce unnecessary License calls even during the first download. This is because a Service may
provide the same Key ID and CEK for both Video and Audio, as well as for multiple resolutions or bitrates.

You can have as many Key Vaults as you would like. It's nice to share Key Vaults or use a unified Vault on
Teams as sharing CEKs immediately can help reduce License calls drastically.

Three types of Vaults are in the Core codebase, API, SQLite and MySQL. API makes HTTP requests to a RESTful API,
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

---
