# Configuration Documentation Index

This page documents all unshackle configuration options, organized by category. For a complete list of all configuration values, see the reference sections below.

You begin with an empty configuration file that you can alter with `unshackle cfg --help`, or find the location with `unshackle env info`.

Avoid comments in config files as they may be removed when the config is rewritten.

---

## Configuration Categories

### [Download & Processing](docs/DOWNLOAD_CONFIG.md)
Configure how media content is downloaded and processed.

- **aria2c** - Multi-threaded downloader settings
- **curl_impersonate** - Browser fingerprinting for anti-bot protection
- **downloader** - Choose downloader per service (requests, aria2c, curl_impersonate, n_m3u8dl_re)
- **n_m3u8dl_re** - HLS/DASH/ISM manifest downloader configuration
- **dl** - Download command defaults (quality, language, workers, etc.)
- **decryption** - Decryption method selection (shaka vs mp4decrypt)

[View Download Configuration →](docs/DOWNLOAD_CONFIG.md)

---

### [DRM & CDM Configuration](docs/DRM_CONFIG.md)
Configure Digital Rights Management and Content Decryption Module settings.

- **cdm** - CDM device mapping and automatic quality-based/profile-based selection
- **remote_cdm** - Remote CDM APIs (DecryptLabs, Custom API, Legacy pywidevine)
- **decrypt_labs_api_key** - DecryptLabs global API key
- **key_vaults** - Key storage backends (SQLite, MySQL, API, HTTP)

[View DRM Configuration →](docs/DRM_CONFIG.md)

---

### [Network & Proxy Configuration](docs/NETWORK_CONFIG.md)
Configure network settings for bypassing geofencing and managing connections.

- **proxy_providers** - VPN/proxy services (NordVPN, Surfshark, Hola, basic, Windscribe)
- **headers** - Global HTTP headers

[View Network Configuration →](docs/NETWORK_CONFIG.md)

---

### [Output & Naming Configuration](docs/OUTPUT_CONFIG.md)
Configure output file organization, naming patterns, and metadata tagging.

- **filenames** - Filename pattern templates
- **scene_naming** - P2P-style vs human-readable naming
- **series_year** - Year inclusion in series names
- **tag** - Group/username tagging in filenames
- **tag_group_name** - Group name tagging toggle
- **tag_imdb_tmdb** - Metadata ID tagging (IMDb, TMDB, TVDB)
- **muxing** - Container title and muxing options
- **chapter_fallback_name** - Chapter naming fallback strategy
- **directories** - Directory path overrides

[View Output Configuration →](docs/OUTPUT_CONFIG.md)

---

### [Subtitle Processing Configuration](docs/SUBTITLE_CONFIG.md)
Configure subtitle conversion, SDH stripping, and formatting preservation.

- **subtitle** - Subtitle processing settings (conversion methods, SDH stripping, formatting)

[View Subtitle Configuration →](docs/SUBTITLE_CONFIG.md)

---

### [Service Integration & Authentication](docs/SERVICE_CONFIG.md)
Configure service-specific authentication and metadata integration.

- **services** - Service-specific configuration overrides
- **credentials** - Authentication credentials per service/profile
- **tmdb_api_key** - TMDB API integration for metadata tagging
- **simkl_client_id** - SIMKL API integration for tracking
- **title_cache_enabled/time/max_retention** - Title metadata caching

[View Service Configuration →](docs/SERVICE_CONFIG.md)

---

### [Advanced & System Configuration](docs/ADVANCED_CONFIG.md)
Configure advanced features, debugging, and system-level settings.

- **serve** - CDM server configuration (pywidevine serve)
- **debug** - Debug logging configuration
- **debug_keys** - Key logging control
- **set_terminal_bg** - Terminal background customization
- **update_checks/interval** - Update checking configuration

[View Advanced Configuration →](docs/ADVANCED_CONFIG.md)

---

## Quick Start

For common setup tasks, here are some typical configurations:

### Basic Setup

```yaml
# Essential credentials for your service
credentials:
  SERVICE_A: "username@email.com:password"

# Download preferences
dl:
  lang: en
  workers: 8
  downloads: 4

# Simple CDM setup
cdm:
  SERVICE_A: local_l3
```

### With Remote CDM

```yaml
remote_cdm:
  - name: decrypt_labs_l1
    type: decrypt_labs
    device_name: L1
    host: https://keyxtractor.decryptlabs.com
    secret: YOUR_API_KEY

cdm:
  SERVICE_A:
    ">=1440": decrypt_labs_l1  # L1 for 4K
    "<=1080": local_l3          # L3 for HD/SD
```

### With VPN/Proxy

```yaml
proxy_providers:
  nordvpn:
    username: your_service_username
    password: your_service_password
    server_map:
      us: 12
      gb: 2366
```

---

## Configuration File Location

Find your configuration file with:

```bash
unshackle env info
```

Edit configuration with:

```bash
unshackle cfg set key value
unshackle cfg get key
unshackle cfg list
```

---

## Security Notes

- **Keep credentials secure** - Do not share your config file
- **API keys are sensitive** - Store TMDB, SIMKL, and DecryptLabs keys securely
- **Database passwords** - Never use root accounts with database vaults
- **Debug logs** - Passwords and tokens are always redacted automatically, but keys are only logged when `debug_keys: true`

---

## See Also

- [Full Configuration Reference (deprecated - see category links above)](CONFIG_REFERENCE.md) - Complete alphabetical listing
- [Service Documentation](../services/) - Service-specific setup guides
- [Architecture & Concepts](../README.md) - System overview
