# Config Documentation

This page documents configuration values and what they do. You begin with an empty configuration file.  
You may alter your configuration with `unshackle cfg --help`, or find the direct location with `unshackle env info`.  
Configuration values are listed in alphabetical order.

Avoid putting comments in the config file as they may be removed. Comments are currently kept only thanks
to the usage of `ruamel.yaml` to parse and write YAML files. In the future `yaml` may be used instead,
which does not keep comments.

## aria2c (dict)

- `max_concurrent_downloads`
  Maximum number of parallel downloads. Default: `min(32,(cpu_count+4))`  
  Note: Overrides the `max_workers` parameter of the aria2(c) downloader function.
- `max_connection_per_server`
  Maximum number of connections to one server for each download. Default: `1`
- `split`
  Split a file into N chunks and download each chunk on its own connection. Default: `5`
- `file_allocation`
  Specify file allocation method. Default: `"prealloc"`

  - `"none"` doesn't pre-allocate file space.
  - `"prealloc"` pre-allocates file space before download begins. This may take some time depending on the size of the
    file.
  - `"falloc"` is your best choice if you are using newer file systems such as ext4 (with extents support), btrfs, xfs
    or NTFS (MinGW build only). It allocates large(few GiB) files almost instantly. Don't use falloc with legacy file
    systems such as ext3 and FAT32 because it takes almost same time as prealloc, and it blocks aria2 entirely until
    allocation finishes. falloc may not be available if your system doesn't have posix_fallocate(3) function.
  - `"trunc"` uses ftruncate(2) system call or platform-specific counterpart to truncate a file to a specified length.

## cdm (dict)

Pre-define which Widevine or PlayReady device to use for each Service by Service Tag as Key (case-sensitive).
The value should be a WVD or PRD filename without the file extension, or a remote CDM name defined in `remote_cdm`.
When loading a local device, unshackle will look in both the `WVDs` and `PRDs` directories for a matching file.

### Basic CDM Selection

For example,

```yaml
AMZN: chromecdm_903_l3
NF: nexus_6_l1
```

### Profile-Based CDM Selection

You may also specify this device based on the profile used.

For example,

```yaml
AMZN: chromecdm_903_l3
NF: nexus_6_l1
DSNP:
  john_sd: chromecdm_903_l3
  jane_uhd: nexus_5_l1
```

### Default Fallback

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

### Quality-Based CDM Selection

**NEW:** You can now select different CDMs based on video resolution (quality). This allows you to use local CDMs
for lower qualities and automatically switch to remote CDMs for higher qualities that require L1/L2 security levels.

unshackle automatically detects the highest quality video track and selects the appropriate CDM before downloading.

#### Supported Quality Operators

- **Exact match**: `"480"`, `"720"`, `"1080"`, `"2160"` - Matches exact resolution
- **Greater than or equal**: `">=1080"` - Matches 1080p and above (1440p, 2160p, etc.)
- **Greater than**: `">720"` - Matches above 720p (1080p, 1440p, 2160p, etc.)
- **Less than or equal**: `"<=1080"` - Matches 1080p and below
- **Less than**: `"<1080"` - Matches below 1080p

**Note**: Quality keys must be quoted strings to preserve operators in YAML.

#### Example: Local for SD/HD, Remote for 4K

```yaml
cdm:
  NETFLIX:
    "<=1080": local_l3              # Use local CDM for 1080p and below
    ">=1440": remote_l1             # Use remote L1 CDM for 1440p and above
    default: local_l3               # Fallback if no quality match

  DISNEY:
    "480": local_l3_mobile          # Use mobile L3 for 480p
    "720": local_l3                 # Use local L3 for 720p
    "1080": local_l3_hd             # Use local L3 for 1080p
    ">1080": remote_l1              # Use remote L1 for above 1080p
```

#### Example: Mixed Profile and Quality Selection

```yaml
cdm:
  AMAZON:
    # Profile-based selection
    john_account: johns_local_l3
    jane_account: janes_remote_l1

    # Quality-based selection (for default profile)
    "<=720": local_l3
    ">=1080": remote_l1

    default: local_l3
```

#### Example: Switching Between Widevine and PlayReady Based on Quality

Some services may use different DRM systems for different quality levels. For example, AMAZON might use
Widevine (ChromeCDM) for SD/HD content but require PlayReady (SL3) for UHD content:

```yaml
cdm:
  AMAZON:
    # Use local/remote Widevine ChromeCDM for 1080p and below
    "<=1080": local_chromecdm

    # Use remote PlayReady SL3 for above 1080p (1440p, 2160p)
    ">1080": remote_sl3

    default: local_chromecdm

# If using local CDMs, place chromecdm.wvd in your WVDs directory
# If using remote CDMs, configure them below:
remote_cdm:
  - name: remote_chromecdm
    type: decrypt_labs                # Or custom_api
    device_name: ChromeCDM
    host: https://your-cdm-api.com
    secret: YOUR_API_KEY

  - name: remote_sl3
    type: decrypt_labs                # Or custom_api
    device_name: SL3
    device_type: PLAYREADY
    host: https://your-cdm-api.com
    secret: YOUR_API_KEY
```

**How it works:**
- When downloading 720p or 1080p content → Uses `local_chromecdm` (local Widevine L3)
- When downloading 1440p or 2160p content → Uses `remote_sl3` (remote PlayReady SL3)
- unshackle automatically detects the video quality and selects the appropriate CDM
- The DRM type is verified against the content's actual DRM system

**Note:** This configuration assumes the service uses different DRM systems for different qualities.
Most services use a single DRM system across all qualities, but some (like AMAZON) may vary by region or quality tier.

### DRM-Specific CDM Selection (Widevine/PlayReady)

For services that support multiple DRM systems, you can specify different CDMs based on the DRM type.
unshackle automatically detects the DRM system used by content and switches to the appropriate CDM.

#### Example: Separate Widevine and PlayReady CDMs

```yaml
cdm:
  DISNEY:
    widevine:
      default: local_wv               # Local Widevine CDM
      ">=2160": remote_l1             # Remote L1 for 4K Widevine

    playready:
      default: local_pr               # Local PlayReady CDM
      ">=1080": remote_sl2            # Remote SL2 for HD+ PlayReady
```

#### Example: AMAZON - Quality-Based with DRM Type Override

For AMAZON, you might want to use ChromeCDM (Widevine) for SD/HD content and PlayReady SL3 for UHD content.
Here's a more explicit configuration using DRM-specific overrides:

```yaml
cdm:
  AMAZON:
    # DRM-specific configuration with quality-based selection
    widevine:
      "<=1080": local_chromecdm       # Local ChromeCDM for 1080p and below
      default: local_chromecdm

    playready:
      ">1080": remote_sl3             # Remote PlayReady SL3 for above 1080p
      "<=1080": local_pr              # Optional: Local PlayReady for lower quality
      default: remote_sl3

    # Fallback for unknown DRM types
    default: local_chromecdm

# Define remote CDMs (if using remote for high quality)
remote_cdm:
  - name: remote_sl3
    type: decrypt_labs                # Or custom_api
    device_name: SL3
    device_type: PLAYREADY
    host: https://your-cdm-api.com
    secret: YOUR_API_KEY

  - name: remote_chromecdm
    type: decrypt_labs                # Or custom_api
    device_name: ChromeCDM
    host: https://your-cdm-api.com
    secret: YOUR_API_KEY
```

**How it works:**
- If content uses **Widevine** → Uses `local_chromecdm` for all qualities up to 1080p
- If content uses **PlayReady** and quality > 1080p → Uses `remote_sl3` (remote SL3)
- If content uses **PlayReady** and quality ≤ 1080p → Uses `local_pr` (local, optional)
- Fallback for unknown DRM → Uses `local_chromecdm`

**Alternative: Simple quality-based approach** (when DRM type varies by quality):

```yaml
cdm:
  AMAZON:
    "<=1080": local_chromecdm         # Local Widevine for SD/HD
    ">1080": remote_sl3               # Remote PlayReady for UHD
    default: local_chromecdm
```

This simpler approach works when the service consistently uses Widevine for SD/HD and PlayReady for UHD.

### How Automatic DRM Switching Works

When downloading content, unshackle:

1. **Detects video quality** - Analyzes all video tracks and determines the highest resolution
2. **Applies quality rules** - Matches resolution against your quality-based CDM configuration
3. **Detects DRM type** - Identifies whether content uses Widevine or PlayReady
4. **Switches CDM automatically** - Loads the appropriate CDM based on DRM type and quality
5. **Falls back if needed** - Uses local CDM if remote CDM is unavailable

For example, if you download 4K content that uses Widevine:
- System detects 2160p resolution
- Matches `">=2160": remote_l1` rule
- Detects Widevine DRM
- Automatically loads `remote_l1` remote CDM
- If remote CDM fails, falls back to local CDM (if available)

### Local to Remote CDM Fallback

When you configure both local and remote CDMs, unshackle follows this priority order:

1. **Remote CDM** (if defined in `remote_cdm` and matched by quality/DRM rules)
2. **Local PlayReady** (.prd files in `PRDs` directory)
3. **Local Widevine** (.wvd files in `WVDs` directory)

This ensures that if a remote CDM API is unavailable, unshackle can still use local devices as fallback.

#### Example: Complete Configuration with Fallback

```yaml
cdm:
  NETFLIX:
    # Use local for low quality, remote for high quality
    "<=720": local_l3_sd            # Local WVD file
    "1080": local_l3_hd             # Local WVD file
    ">=1440": remote_l1             # Remote L1 API
    default: local_l3_sd

# Define remote CDMs
remote_cdm:
  - name: remote_l1
    type: decrypt_labs              # Or custom_api
    device_name: L1
    host: https://your-cdm-api.com
    secret: YOUR_API_KEY

  - name: remote_sl2
    type: decrypt_labs              # Or custom_api
    device_name: SL2                # PlayReady SL2000
    device_type: PLAYREADY
    host: https://your-cdm-api.com
    secret: YOUR_API_KEY
```

**Result:**
- **480p/720p content** → Uses `local_l3_sd` (local .wvd file)
- **1080p content** → Uses `local_l3_hd` (local .wvd file)
- **1440p/2160p content** → Uses `remote_l1` (remote API)
- **If remote API fails** → Falls back to local .wvd files if available

### Advanced: Service Certificate Configuration

Some services require L1/L2 security levels for high-quality content. When using remote L1/L2 CDMs,
you may need to configure the service certificate in the `services` section. See the [services](#services-dict)
section for certificate configuration details.

### Configuration Priority Order

When multiple configuration types are defined, unshackle follows this selection hierarchy:

1. **Profile-specific** (if `-p/--profile` specified on command line)
2. **DRM-specific** (widevine/playready keys)
3. **Quality-based** (resolution with operators: >=, >, <=, <, exact)
4. **Service-level default** (default key under service)
5. **Global default** (top-level default key)

### Summary

- **Basic**: Simple service → CDM mapping
- **Profile**: Different CDMs per user profile
- **Quality**: Automatic CDM selection based on video resolution
- **DRM Type**: Separate CDMs for Widevine vs PlayReady
- **Fallback**: Local CDM fallback if remote CDM unavailable
- **Automatic**: Zero manual intervention - unshackle handles all switching

## chapter_fallback_name (str)

The Chapter Name to use when exporting a Chapter without a Name.
The default is no fallback name at all and no Chapter name will be set.

The fallback name can use the following variables in f-string style:

- `{i}`: The Chapter number starting at 1.
  E.g., `"Chapter {i}"`: "Chapter 1", "Intro", "Chapter 3".
- `{j}`: A number starting at 1 that increments any time a Chapter has no title.
  E.g., `"Chapter {j}"`: "Chapter 1", "Intro", "Chapter 2".

These are formatted with f-strings, directives are supported.
For example, `"Chapter {i:02}"` will result in `"Chapter 01"`.

## credentials (dict[str, str|list|dict])

Specify login credentials to use for each Service, and optionally per-profile.

For example,

```yaml
ALL4: jane@gmail.com:LoremIpsum100 # directly
AMZN: # or per-profile, optionally with a default
  default: jane@example.tld:LoremIpsum99 # <-- used by default if -p/--profile is not used
  james: james@gmail.com:TheFriend97
  john: john@example.tld:LoremIpsum98
NF: # the `default` key is not necessary, but no credential will be used by default
  john: john@gmail.com:TheGuyWhoPaysForTheNetflix69420
```

The value should be in string form, i.e. `john@gmail.com:password123` or `john:password123`.  
Any arbitrary values can be used on the left (username/password/phone) and right (password/secret).  
You can also specify these in list form, i.e., `["john@gmail.com", ":PasswordWithAColon"]`.

If you specify multiple credentials with keys like the `AMZN` and `NF` example above, then you should
use a `default` key or no credential will be loaded automatically unless you use `-p/--profile`. You
do not have to use a `default` key at all.

Please be aware that this information is sensitive and to keep it safe. Do not share your config.

## curl_impersonate (dict)

Configuration for curl_cffi browser impersonation and custom fingerprinting.

- `browser` - The Browser to impersonate as OR a fingerprint preset name. A list of available Browsers and Versions
  are listed here: <https://github.com/yifeikong/curl_cffi#sessions>

  Default: `"chrome124"`

### Available Fingerprint Presets

- `okhttp4` - Android TV OkHttp 4.x fingerprint preset (for better Android TV compatibility)
- `okhttp5` - Android TV OkHttp 5.x fingerprint preset (for better Android TV compatibility)

### Custom Fingerprinting

For advanced users, you can specify custom TLS and HTTP/2 fingerprints:

- `ja3` (str): Custom JA3 TLS fingerprint string (format: "SSLVersion,Ciphers,Extensions,Curves,PointFormats")
- `akamai` (str): Custom Akamai HTTP/2 fingerprint string (format: "SETTINGS|WINDOW_UPDATE|PRIORITY|PSEUDO_HEADERS")
- `extra_fp` (dict): Additional fingerprint parameters for advanced customization

For example, using a browser preset:

```yaml
curl_impersonate:
  browser: "chrome120"
```

Using an Android TV preset:

```yaml
curl_impersonate:
  browser: "okhttp4"
```

Using custom fingerprints:

```yaml
curl_impersonate:
  browser: "chrome120"
  ja3: "custom_ja3_fingerprint_string"
  akamai: "custom_akamai_fingerprint_string"
```

## directories (dict)

Override the default directories used across unshackle.  
The directories are set to common values by default.

The following directories are available and may be overridden,

- `commands` - CLI Command Classes.
- `services` - Service Classes.
- `vaults` - Vault Classes.
- `fonts` - Font files (ttf or otf).
- `downloads` - Downloads.
- `temp` - Temporary files or conversions during download.
- `cache` - Expiring data like Authorization tokens, or other misc data.
- `cookies` - Expiring Cookie data.
- `logs` - Logs.
- `wvds` - Widevine Devices.
- `prds` - PlayReady Devices.
- `dcsl` - Device Certificate Status List.

Notes:

- `services` accepts either a single directory or a list of directories to search for service modules.

For example,

```yaml
downloads: "D:/Downloads/unshackle"
temp: "D:/Temp/unshackle"
```

There are directories not listed that cannot be modified as they are crucial to the operation of unshackle.

## dl (dict)

Pre-define default options and switches of the `dl` command.
The values will be ignored if explicitly set in the CLI call.

The Key must be the same value Python click would resolve it to as an argument.
E.g., `@click.option("-r", "--range", "range_", type=...` actually resolves as `range_` variable.

### Common Options

For example to set the default primary language to download to German,

```yaml
lang: de
```

You can also set multiple preferred languages using a list, e.g.,

```yaml
lang:
  - en
  - fr
```

to set how many tracks to download concurrently to 4 and download threads to 16,

```yaml
downloads: 4
workers: 16
```

to set `--bitrate=CVBR` for the AMZN service,

```yaml
lang: de
AMZN:
  bitrate: CVBR
```

or to change the output subtitle format from the default (original format) to WebVTT,

```yaml
sub_format: vtt
```

### Additional Available Options

The following additional flags can be pre-configured as defaults:

- `latest_episode` (bool): Download only the most recent episode (corresponds to `--latest-episode` / `-le` flag)
- `no_video` (bool): Skip downloading video tracks (corresponds to `--no-video` / `-nv` flag)
- `audio_description` (bool): Download audio description tracks (corresponds to `--audio-description` / `-ad` flag)
- `forced_subs` (bool): Include forced subtitle tracks (corresponds to `--forced-subs` / `-fs` flag)
- `no_cache` (bool): Bypass title cache (corresponds to `--no-cache` flag)
- `reset_cache` (bool): Clear title cache before fetching (corresponds to `--reset-cache` flag)
- `best_available` (bool): Continue with best quality if requested unavailable (corresponds to `--best-available` flag)

For example,

```yaml
dl:
  latest_episode: true      # Always download only the latest episode
  audio_description: true   # Include audio description tracks by default
  best_available: true      # Use best available quality as fallback
```

**Note**: These options can also be set per-service by nesting them under a service tag.

## downloader (str | dict)

Choose what software to use to download data throughout unshackle where needed.
You may provide a single downloader globally or a mapping of service tags to
downloaders.

Options:

- `requests` (default) - <https://github.com/psf/requests>
- `aria2c` - <https://github.com/aria2/aria2>
- `curl_impersonate` - <https://github.com/yifeikong/curl-impersonate> (via <https://github.com/yifeikong/curl_cffi>)
- `n_m3u8dl_re` - <https://github.com/nilaoda/N_m3u8DL-RE>

Note that aria2c can reach the highest speeds as it utilizes threading and more connections than the other downloaders. However, aria2c can also be one of the more unstable downloaders. It will work one day, then not another day. It also does not support HTTP(S) proxies while the other downloaders do.

Example mapping:

```yaml
downloader:
  NF: requests
  AMZN: n_m3u8dl_re
  DSNP: n_m3u8dl_re
  default: requests
```

The `default` entry is optional. If omitted, `requests` will be used for services not listed.

## debug (bool)

Enable comprehensive JSON-based debug logging for troubleshooting and service development.
When enabled, creates JSON Lines (`.jsonl`) log files with complete debugging context.

Default: `false`

When enabled (via `--debug` flag or `debug: true` in config):

- Creates structured JSON Lines log files: `logs/unshackle_debug_{service}_{timestamp}.jsonl`
- Logs session info, CLI parameters, service configuration, CDM details, authentication status
- Logs title/track metadata, DRM operations, vault queries
- Logs errors with full stack traces
- Also creates text log: `logs/unshackle_root_{timestamp}.log`

For example,

```yaml
debug: true
```

**Security Note**: Passwords, tokens, cookies, and session tokens are ALWAYS redacted regardless of this setting.

## debug_keys (bool)

Control whether actual decryption keys (CEKs) are logged in debug logs.

Default: `false`

When set to `true`, includes actual content encryption keys in debug logs. This is useful for debugging
key retrieval and decryption issues.

For example,

```yaml
debug_keys: true
```

**Security Notes**:

- Only affects content_key and key fields (the actual CEKs)
- Key metadata (kid, keys_count, key_id) is always logged regardless of this setting
- Passwords, tokens, cookies, and session tokens remain redacted even when this is enabled
- Use with caution and ensure debug logs are stored securely

## decryption (str | dict)

Choose what software to use to decrypt DRM-protected content throughout unshackle where needed.
You may provide a single decryption method globally or a mapping of service tags to
decryption methods.

Options:

- `shaka` (default) - Shaka Packager - <https://github.com/shaka-project/shaka-packager>
- `mp4decrypt` - mp4decrypt from Bento4 - <https://github.com/axiomatic-systems/Bento4>

Note that Shaka Packager is the traditional method and works with most services. mp4decrypt
is an alternative that may work better with certain services that have specific encryption formats.

Example mapping:

```yaml
decryption:
  ATVP: mp4decrypt
  AMZN: shaka
  default: shaka
```

The `default` entry is optional. If omitted, `shaka` will be used for services not listed.

Simple configuration (single method for all services):

```yaml
decryption: mp4decrypt
```

## decrypt_labs_api_key (str)

API key for DecryptLabs CDM service integration.

When set, enables the use of DecryptLabs remote CDM services in your `remote_cdm` configuration.
This is used specifically for `type: "decrypt_labs"` entries in the remote CDM list.

For example,

```yaml
decrypt_labs_api_key: "your_api_key_here"
```

**Note**: This is different from the per-CDM `secret` field in `remote_cdm` entries. This provides a global
API key that can be referenced across multiple DecryptLabs CDM configurations.

## filenames (dict)

Override the default filenames used across unshackle.
The filenames use various variables that are replaced during runtime.

The following filenames are available and may be overridden:

- `log` - Log filenames. Uses `{name}` and `{time}` variables.
- `debug_log` - Debug log filenames in JSON Lines format. Uses `{service}` and `{time}` variables.
- `config` - Service configuration filenames.
- `root_config` - Root configuration filename.
- `chapters` - Chapter export filenames. Uses `{title}` and `{random}` variables.
- `subtitle` - Subtitle export filenames. Uses `{id}` and `{language}` variables.

For example,

```yaml
filenames:
  log: "unshackle_{name}_{time}.log"
  debug_log: "unshackle_debug_{service}_{time}.jsonl"
  config: "config.yaml"
  root_config: "unshackle.yaml"
  chapters: "Chapters_{title}_{random}.txt"
  subtitle: "Subtitle_{id}_{language}.srt"
```

## headers (dict)

Case-Insensitive dictionary of headers that all Services begin their Request Session state with.  
All requests will use these unless changed explicitly or implicitly via a Server response.  
These should be sane defaults and anything that would only be useful for some Services should not
be put here.

Avoid headers like 'Accept-Encoding' as that would be a compatibility header that Python-requests will
set for you.

I recommend using,

```yaml
Accept-Language: "en-US,en;q=0.8"
User-Agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/77.0.3865.75 Safari/537.36"
```

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

## muxing (dict)

- `set_title`
  Set the container title to `Show SXXEXX Episode Name` or `Movie (Year)`. Default: `true`

## n_m3u8dl_re (dict)

Configuration for N_m3u8DL-RE downloader. This downloader is particularly useful for HLS streams.

- `thread_count`
  Number of threads to use for downloading. Default: Uses the same value as max_workers from the command.
- `ad_keyword`
  Keyword to identify and potentially skip advertisement segments. Default: `None`
- `use_proxy`
  Whether to use proxy when downloading. Default: `true`

For example,

```yaml
n_m3u8dl_re:
  thread_count: 16
  ad_keyword: "advertisement"
  use_proxy: true
```

## proxy_providers (dict)

Enable external proxy provider services. These proxies will be used automatically where needed as defined by the
Service's GEOFENCE class property, but can also be explicitly used with `--proxy`. You can specify which provider
to use by prefixing it with the provider key name, e.g., `--proxy basic:de` or `--proxy nordvpn:de`. Some providers
support specific query formats for selecting a country/server.

### basic (dict[str, str|list])

Define a mapping of country to proxy to use where required.  
The keys are region Alpha 2 Country Codes. Alpha 2 Country Codes are `[a-z]{2}` codes, e.g., `us`, `gb`, and `jp`.  
Don't get this mixed up with language codes like `en` vs. `gb`, or `ja` vs. `jp`.

Do note that each key's value can be a list of strings, or a string. For example,

```yaml
us:
  - "http://john%40email.tld:password123@proxy-us.domain.tld:8080"
  - "http://jane%40email.tld:password456@proxy-us.domain2.tld:8080"
de: "https://127.0.0.1:8080"
```

Note that if multiple proxies are defined for a region, then by default one will be randomly chosen.
You can choose a specific one by specifying it's number, e.g., `--proxy basic:us2` will choose the
second proxy of the US list.

### nordvpn (dict)

Set your NordVPN Service credentials with `username` and `password` keys to automate the use of NordVPN as a Proxy
system where required.

You can also specify specific servers to use per-region with the `server_map` key.  
Sometimes a specific server works best for a service than others, so hard-coding one for a day or two helps.

For example,

```yaml
username: zxqsR7C5CyGwmGb6KSvk8qsZ # example of the login format
password: wXVHmht22hhRKUEQ32PQVjCZ
server_map:
  us: 12 # force US server #12 for US proxies
```

The username and password should NOT be your normal NordVPN Account Credentials.  
They should be the `Service credentials` which can be found on your Nord Account Dashboard.

Once set, you can also specifically opt in to use a NordVPN proxy by specifying `--proxy=gb` or such.
You can even set a specific server number this way, e.g., `--proxy=gb2366`.

Note that `gb` is used instead of `uk` to be more consistent across regional systems.

### surfsharkvpn (dict)

Enable Surfshark VPN proxy service using Surfshark Service credentials (not your login password).
You may pin specific server IDs per region using `server_map`.

```yaml
username: your_surfshark_service_username # https://my.surfshark.com/vpn/manual-setup/main/openvpn
password: your_surfshark_service_password # service credentials, not account password
server_map:
  us: 3844 # force US server #3844
  gb: 2697 # force GB server #2697
  au: 4621 # force AU server #4621
```

### windscribevpn (dict)

Enable Windscribe VPN proxy service using Windscribe Service credentials (not your login password).
You may pin specific server hostnames per region using `server_map`.

```yaml
username: your_windscribe_username  # From https://windscribe.com/getconfig/openvpn
password: your_windscribe_password  # Service credentials (not your login password)
server_map:
  us: "us-central-096.totallyacdn.com"  # Force specific US server
  gb: "uk-london-055.totallyacdn.com"   # Force specific GB server
  de: "de-frankfurt-001.totallyacdn.com"  # Force specific DE server
```

**Note**: The username and password should be your Windscribe OpenVPN credentials, which can be obtained from
the Windscribe configuration generator. The `server_map` uses full server hostnames (not just numbers like NordVPN).

You can use Windscribe proxies by specifying `--proxy=windscribevpn:us` or such. Server selection works similar
to other providers - use `--proxy=windscribevpn:us` for automatic server or specify the full hostname if needed.

### hola (dict)

Enable Hola VPN proxy service for datacenter and residential proxies.

This provider uses the open-source `hola-proxy` tool and requires no account credentials.
Simply include an empty configuration to enable it.

For example,

```yaml
proxy_providers:
  hola: {}
```

**Requirements**: The `hola-proxy` binary must be installed and available in your system PATH or in the
unshackle binaries directory.

**Note**: Hola uses a peer-to-peer VPN network. Consider the privacy implications before using this provider.

### gluetun (dict)

Enable Gluetun VPN proxy service, which creates Docker containers running Gluetun to bridge VPN connections
to HTTP proxies. This supports 50+ VPN providers through a single, unified interface.

**Requirements**: Docker must be installed and running. Check with `unshackle env check`.

```yaml
gluetun:
  base_port: 8888              # Starting port for HTTP proxies
  auto_cleanup: true           # Remove containers when done
  container_prefix: "unshackle-gluetun"  # Docker container name prefix
  verify_ip: true              # Verify VPN IP matches expected region

  providers:
    windscribe:
      vpn_type: wireguard
      credentials:
        private_key: "YOUR_WIREGUARD_PRIVATE_KEY"
        addresses: "YOUR_WIREGUARD_ADDRESS"  # e.g., "10.x.x.x/32"
      server_countries:
        us: US
        uk: GB
        ca: CA
```

**Usage**: Use the format `--proxy gluetun:<provider>:<region>`, e.g.:
- `--proxy gluetun:windscribe:us` - Connect via Windscribe to US
- `--proxy gluetun:nordvpn:de` - Connect via NordVPN to Germany

**Supported VPN Types**:
- `wireguard` - For providers like Windscribe, NordVPN, Surfshark (recommended)
- `openvpn` - For providers like ExpressVPN, PIA

See the example config file for more provider configurations.

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

**Note:** The `device_type` and `security_level` fields are optional metadata. They don't affect API communication
but are used for internal device identification.

### Custom API Remote CDM

A highly configurable CDM adapter that can work with virtually any third-party CDM API through YAML configuration.
This allows you to integrate custom CDM services without writing code.

**Configuration Philosophy:**
- **90%** of new CDM providers: Only YAML config needed
- **9%** of cases: Add new transform type
- **1%** of cases: Add new auth strategy

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
        User-Agent: Unshackle/2.0.0
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

### Legacy PyWidevine Serve Format

Standard pywidevine serve-compliant remote CDM configuration (backwards compatibility).

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

**Note:** If `type` is not specified, unshackle assumes legacy format. For DecryptLabs or Custom API,
always specify `type: decrypt_labs` or `type: custom_api`.

### Integration with Quality-Based CDM Selection

Remote CDMs can be used in quality-based and DRM-specific CDM configurations:

```yaml
cdm:
  NETFLIX:
    "<=1080": local_l3              # Local for SD/HD
    ">=1440": remote_l1             # Remote for 4K+

    widevine:
      ">=2160": remote_l1           # Remote L1 for 4K Widevine
      default: local_wv

    playready:
      ">=1080": remote_sl2          # Remote SL2 for HD+ PlayReady
      default: local_pr

remote_cdm:
  - name: remote_l1
    type: decrypt_labs              # Or custom_api
    device_name: L1
    host: https://your-cdm-api.com
    secret: YOUR_API_KEY

  - name: remote_sl2
    type: decrypt_labs              # Or custom_api
    device_name: SL2
    device_type: PLAYREADY
    host: https://your-cdm-api.com
    secret: YOUR_API_KEY
```

### Key Features

**Intelligent Caching:**
- Remote CDMs integrate with unshackle's vault system
- Keys are cached locally to reduce API calls
- Cached keys are checked before making license requests
- Multiple vault sources supported (SQLite, MySQL, API)

**Automatic Fallback:**
- If remote CDM fails, unshackle falls back to local devices (if available)
- Priority: Remote CDM → Local PRD → Local WVD

**DRM Type Detection:**
- Automatically switches between Widevine and PlayReady remote CDMs
- Based on content DRM system detection

**Quality-Based Selection:**
- Use different remote CDMs based on video resolution
- Combine with local CDMs for cost-effective downloads

[pywidevine]: https://github.com/rlaphoenix/pywidevine

## scene_naming (bool)

Set scene-style naming for titles. When `true` uses scene naming patterns (e.g., `Prime.Suspect.S07E01...`), when
`false` uses a more human-readable style (e.g., `Prime Suspect S07E01 ...`). Default: `true`.

## series_year (bool)

Whether to include the series year in series names for episodes and folders. Default: `true`.

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

## services (dict)

Configuration data for each Service. The Service will have the data within this section merged into the `config.yaml`
before provided to the Service class.

This configuration serves two purposes:

1. **Service-specific data**: Sensitive configuration like user or device-specific API keys, IDs, device attributes, and
   so on. A `config.yaml` file is typically shared and not meant to be modified, so use this for any sensitive data.

2. **Per-service configuration overrides**: Override any global configuration option on a per-service basis for fine-tuned
   control. This allows you to customize behavior for services with special requirements.

The Key is the Service Tag, and the value can take any form (typically a dictionary or list).

### Basic Service Configuration

For example,

```yaml
services:
  NOW:
    client:
      auth_scheme: MESSO
      # ... more sensitive data
```

### Service-Specific Configuration Overrides

**New in v2.0.0**: You can override ANY global configuration option on a per-service basis. Supported overrides include:

- `dl` - Download command defaults
- `aria2c` - aria2c downloader settings
- `n_m3u8dl_re` - N_m3u8DL-RE downloader settings
- `curl_impersonate` - Browser impersonation settings
- `subtitle` - Subtitle processing options
- `muxing` - Muxing behavior
- `headers` - HTTP headers
- And more...

### Comprehensive Example

```yaml
services:
  EXAMPLE:
    # Standard service configuration
    api_key: "service_api_key"

    # Service certificate for Widevine L1/L2 (base64 encoded)
    certificate: |
      CAUSwwUKvQIIAxIQ5US6QAvBDzfTtjb4tU/7QxiH8c+TBSKOAjCCAQoCggEBAObzvlu2hZRs...
      # (full base64 certificate)

    # Profile-specific configurations
    profiles:
      john_sd:
        device:
          app_name: "AIV"
          device_model: "SHIELD Android TV"
      jane_uhd:
        device:
          app_name: "AIV"
          device_model: "Fire TV Stick 4K"

    # Override dl command defaults for this service
    dl:
      downloads: 4              # Limit concurrent track downloads
      workers: 8                # Reduce workers per track
      lang: ["en", "es-419"]    # Different language priority
      sub_format: srt           # Force SRT subtitle format

    # Override n_m3u8dl_re downloader settings
    n_m3u8dl_re:
      thread_count: 8           # Lower thread count for rate-limited service
      use_proxy: true           # Force proxy usage
      retry_count: 10           # More retries for unstable connections

    # Override aria2c downloader settings
    aria2c:
      max_concurrent_downloads: 2
      max_connection_per_server: 1
      split: 3

    # Override subtitle processing
    subtitle:
      conversion_method: pycaption
      sdh_method: auto

    # Service-specific headers
    headers:
      User-Agent: "Service-specific user agent string"
      Accept-Language: "en-US,en;q=0.9"

    # Override muxing options
    muxing:
      set_title: true

  # Example: Rate-limited service requiring conservative settings
  RATE_LIMITED_SERVICE:
    dl:
      downloads: 2
      workers: 4
    n_m3u8dl_re:
      thread_count: 4
      retry_count: 20
    aria2c:
      max_concurrent_downloads: 1
      max_connection_per_server: 1
```

### Important Notes

- Overrides are merged with global config, not replaced
- Only specified keys are overridden; others use global defaults
- Reserved keys (`profiles`, `api_key`, `certificate`, etc.) are NOT treated as overrides
- Any dict-type config option can be overridden
- CLI arguments always take priority over service-specific config
- This feature enables fine-tuned control without modifying global settings

## set_terminal_bg (bool)

Controls whether unshackle should set the terminal background color. Default: `false`

For example,

```yaml
set_terminal_bg: true
```

## simkl_client_id (str)

Client ID for SIMKL API integration. SIMKL is used as a metadata source for improved title matching and tagging,
especially when a TMDB API key is not configured.

To obtain a SIMKL Client ID:

1. Create an account at <https://simkl.com>
2. Go to <https://simkl.com/settings/developer/>
3. Register a new application to receive your Client ID

For example,

```yaml
simkl_client_id: "your_client_id_here"
```

**Note**: While optional, having a SIMKL Client ID improves metadata lookup reliability and reduces the chance
of rate limiting. SIMKL serves as an alternative or fallback metadata source to TMDB.

## tag (str)

Group or Username to postfix to the end of all download filenames following a dash.  
For example, `tag: "J0HN"` will have `-J0HN` at the end of all download filenames.

## tag_group_name (bool)

Enable/disable tagging downloads with your group name when `tag` is set. Default: `true`.

## tag_imdb_tmdb (bool)

Enable/disable tagging downloaded files with IMDB/TMDB/TVDB identifiers (when available). Default: `true`.

## title_cache_enabled (bool)

Enable/disable caching of title metadata to reduce redundant API calls. Default: `true`.

## title_cache_time (int)

Cache duration in seconds for title metadata. Default: `1800` (30 minutes).

## title_cache_max_retention (int)

Maximum retention time in seconds for serving slightly stale cached title metadata when API calls fail.  
Default: `86400` (24 hours). Effective retention is `min(title_cache_time + grace, title_cache_max_retention)`.

## tmdb_api_key (str)

API key for The Movie Database (TMDB). This is used for tagging downloaded files with TMDB,
IMDB and TVDB identifiers. Leave empty to disable automatic lookups.

To obtain a TMDB API key:

1. Create an account at <https://www.themoviedb.org/>
2. Go to <https://www.themoviedb.org/settings/api> to register for API access
3. Fill out the API application form with your project details
4. Once approved, you'll receive your API key

For example,

```yaml
tmdb_api_key: cf66bf18956kca5311ada3bebb84eb9a # Not a real key
```

**Note**: Keep your API key secure and do not share it publicly. This key is used by the core/utils/tags.py module to fetch metadata from TMDB for proper file tagging.

## subtitle (dict)

Control subtitle conversion, SDH (hearing-impaired) stripping behavior, and formatting preservation.

### Conversion and Processing Options

- `conversion_method`: How to convert subtitles between formats. Default: `auto`.

  - `auto`: Smart routing - use subby for WebVTT/SAMI, pycaption for others.
  - `subby`: Always use subby with CommonIssuesFixer for advanced processing.
  - `subtitleedit`: Prefer SubtitleEdit when available; otherwise fallback to standard conversion.
  - `pycaption`: Use only the pycaption library (no SubtitleEdit, no subby).
  - `pysubs2`: Use pysubs2 library (supports SRT, SSA, ASS, WebVTT, TTML, SAMI, MicroDVD, MPL2, TMP formats).

- `sdh_method`: How to strip SDH cues. Default: `auto`.

  - `auto`: Try subby for SRT first, then SubtitleEdit, then subtitle-filter.
  - `subby`: Use subby's SDHStripper (SRT only).
  - `subtitleedit`: Use SubtitleEdit's RemoveTextForHI when available.
  - `filter-subs`: Use the subtitle-filter library directly.

- `strip_sdh`: Automatically create stripped (non-SDH) versions of SDH subtitles. Default: `true`.

  Set to `false` to disable automatic SDH stripping entirely. When `true`, unshackle will automatically
  detect SDH subtitles and create clean versions alongside the originals.

- `convert_before_strip`: Auto-convert VTT/other formats to SRT before using subtitle-filter. Default: `true`.

  This ensures compatibility when subtitle-filter is used as the fallback SDH stripping method, as
  subtitle-filter works best with SRT format.

- `preserve_formatting`: Preserve original subtitle formatting (tags, positioning, styling). Default: `true`.

  When `true`, skips pycaption processing for WebVTT files to keep tags like `<i>`, `<b>`, positioning,
  and other formatting intact. Combined with no `sub_format` setting, ensures subtitles remain in their
  original format.

### Example Configuration

```yaml
subtitle:
  conversion_method: auto
  sdh_method: auto
  strip_sdh: true
  convert_before_strip: true
  preserve_formatting: true
```

### Minimal Configuration (Disable Processing)

```yaml
subtitle:
  strip_sdh: false            # Don't strip SDH
  preserve_formatting: true   # Keep all formatting intact
```

## update_checks (bool)

Check for updates from the GitHub repository on startup. Default: `true`.

## update_check_interval (int)

How often to check for updates, in hours. Default: `24`.
