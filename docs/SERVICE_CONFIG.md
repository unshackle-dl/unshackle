# Service Integration & Authentication Configuration

This document covers service-specific configuration, authentication, and metadata integration options.

## services (dict)

Configuration data for each Service. The Service will have the data within this section merged into the per-service
`config.yaml` (located in the service's directory) before being provided to the Service class.

Think of this config to be used for more sensitive configuration data, like user or device-specific API keys, IDs,
device attributes, and so on. A per-service `config.yaml` file is typically shared and not meant to be modified,
so use this for any sensitive configuration data.

The Key is the Service Tag, but can take any arbitrary form for its value. It's expected to begin as either a list or
a dictionary.

For example,

```yaml
EXAMPLE:
  client:
    auth_scheme: MESSO
    # ... more sensitive data
```

### Per-Service Configuration Overrides

You can override many global configuration options on a per-service basis by nesting them under the
service tag in the `services` section. Supported override keys include: `dl`, `subtitle`, `muxing`,
`headers`, `proxy_map`, `title_map`, and more.

Overrides are merged with global config (not replaced) -- only specified keys are overridden, others
use global defaults.

Any `dl` command option can be overridden per service. Use the option name with dashes as underscores
(`--v-lang` -> `v_lang`). `range` and `list` work as-is; their internal Python names (`range_`,
`list_`, suffixed to avoid the builtins) are also accepted.

Precedence (highest first):

1. Explicit CLI arguments / environment variables (e.g. `--v-lang en`)
2. Per-service config (`services.<TAG>.dl`)
3. Global `dl:` config
4. Built-in option defaults

For example,

```yaml
services:
  RATE_LIMITED_SERVICE:
    dl:
      downloads: 2       # Limit concurrent track downloads
      workers: 4         # Reduce workers to avoid rate limits
    headers:
      User-Agent: "..."  # Service-specific UA override
```

Note: unshackle uses a single unified `requests`-based downloader. The legacy `aria2c`,
`n_m3u8dl_re`, and `curl_impersonate` override sections have been removed.

### title_map (dict)

Rewrites service-provided titles before naming and output. Some services name a title differently
from how you want it stored, which can break library matching (e.g. a regional variant reusing the
international name). Keys are the exact title string the service returns; values are the desired
output title.

```yaml
services:
  EXAMPLE:
    title_map:
      Service Title: Desired Title
```

Episodes are matched on their show title, Movies and Songs on their name. The remap is applied
after the title cache (so edits take effect without a cache reset) and before any `--enrich`
override (so an explicit enrich still wins).

It applies on the local `dl` path, the `import` command, and the remote client (`dl --remote`).
For remote services the **client's** `title_map` is applied to the titles returned by the server,
so you can rename titles for services you don't have installed locally. The server sends raw
titles and does not remap, leaving the final name fully under the client's control.

### Service Class Conventions

Each service directory under `unshackle/services/` exports a class extending
`unshackle.core.service.Service`. The class name must match the directory name (the service tag).

Key class variables (defined on `Service` or by service-level idiom):

- `ALIASES: tuple[str, ...]` — alternative tags accepted on the CLI. Empty by default.
- `GEOFENCE: tuple[str, ...]` — ISO country codes the service is available in. Empty == no geofence.
- `TITLE_RE: str` — regex (with named groups, e.g. `(?P<id>...)`, `(?P<type>...)`) used by the
  service to parse the CLI title argument. Service-level idiom, not declared on the base class.
- `NO_SUBTITLES: bool` — service-level idiom indicating the service has no subtitle tracks.

`self.*` helpers available after `super().__init__(ctx)`:

- `self.session` — pre-configured HTTP session (`requests.Session`, or `RnetSession` when TLS
  impersonation is active). Cookies, headers, proxies pre-applied.
- `self.config` — merged service config (per-service `config.yaml` plus the `services.<TAG>` block
  from `unshackle.yaml`).
- `self.log` — `logging.Logger` named for the service class.
- `self.cache` — generic `Cacher` for arbitrary key/value persistence.
- `self.title_cache` — specialized `TitleCacher` for title metadata.
- `self.track_request` — `TrackRequest` built from CLI flags. Fields: `codecs: list[Video.Codec]`,
  `ranges: list[Video.Range]` (defaults to `[SDR]`), `best_available: bool`. Services may
  read or rewrite these (e.g. force HEVC for HDR ranges).
- `self.credential` — set during `authenticate()`; `None` if cookies-only.
- `self.current_region` — lowercase ISO country code from proxy/geolocation, or `None`.
- `self.request_input(prompt: str) -> str` — interactive prompt. Falls through to `input()`
  locally; under `serve`, the attached `InputBridge` relays the prompt to the remote client.

Driving CLI flags (parsed into `self.track_request`):

- `-v` / `--vcodec` — comma-separated `Video.Codec` list (e.g. `H264,H265`).
- `-a` / `--acodec` — comma-separated audio codec list.
- `-r` / `--range` — comma-separated `Video.Range` list (`SDR`, `HDR10`, `HDR10+`, `DV`,
  `HYBRID`). Defaults to `[SDR]`.
- `-q` / `--quality` — resolution list.
- `--vbitrate-range` / `--abitrate-range` — `MIN-MAX` kbps windows.

---

## credentials (dict[str, str|list|dict])

Specify login credentials to use for each Service, and optionally per-profile.

For example,

```yaml
EXAMPLE: jane@example.tld:LoremIpsum100 # directly
EXAMPLE2: # or per-profile, optionally with a default
  default: jane@example.tld:LoremIpsum99 # <-- used by default if -p/--profile is not used
  james: james@example.tld:TheFriend97
  john: john@example.tld:LoremIpsum98
EXAMPLE3: # the `default` key is not necessary, but no credential will be used by default
  john: john@example.tld:SecretPassword123
```

The value should be in string form, i.e. `john@example.tld:password123` or `john:password123`.
Any arbitrary values can be used on the left (username/password/phone) and right (password/secret).
You can also specify these in list form, i.e., `["john@example.tld", ":PasswordWithAColon"]`.

If you specify multiple credentials with keys like the `EXAMPLE2` and `EXAMPLE3` example above, then you should
use a `default` key or no credential will be loaded automatically unless you use `-p/--profile`. You
do not have to use a `default` key at all.

Please be aware that this information is sensitive and to keep it safe. Do not share your config.

---

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

**Note**: Keep your API key secure and do not share it publicly. This key is used by the `core/providers/tmdb.py` metadata provider to fetch metadata from TMDB for proper file tagging and ID enrichment.

---

## simkl_client_id (str)

Client ID for SIMKL API integration. SIMKL is used as a metadata source for improved title matching and tagging,
especially when a TMDB API key is not configured.

To obtain a SIMKL Client ID:

1. Create an account at <https://simkl.com/>
2. Go to <https://simkl.com/settings/developer/>
3. Register a new application to receive your Client ID

For example,

```yaml
simkl_client_id: "your_client_id_here"
```

**Note**: While optional, having a SIMKL Client ID improves metadata lookup reliability. SIMKL serves as an alternative or fallback metadata source to TMDB. This is used by the `core/providers/simkl.py` metadata provider.

---

## ipinfo_api_key (str)

Optional API token for [ipinfo.io](https://ipinfo.io). When set, unshackle uses the free authenticated **Lite** endpoint (`https://api.ipinfo.io/lite/me`), which has substantially higher rate limits than the anonymous endpoint and returns richer fields (ASN, organization name, continent). Leave empty to use the anonymous ipinfo.io endpoint, with [ip-api.in](https://ip-api.in) as a final fallback.

To obtain an ipinfo.io token:

1. Sign up for a free account at <https://ipinfo.io/signup>
2. Copy the token from your dashboard

For example,

```yaml
ipinfo_api_key: "12a3b45cd678ef" # Not a real key
```

**Note**: The token is only ever sent to `api.ipinfo.io` as a per-request `Authorization` header — it is never attached to your session for service requests. Used by `core/utils/ip_info.py` for region detection and proxy verification.

---

## title_cache_enabled (bool)

Enable/disable caching of title metadata to reduce redundant API calls. Default: `true`.

---

## title_cache_time (int)

Cache duration in seconds for title metadata. Default: `1800` (30 minutes).

---

## title_cache_max_retention (int)

Maximum retention time in seconds for serving slightly stale cached title metadata when API calls fail.
Default: `86400` (24 hours). Effective retention is `min(title_cache_time + grace, title_cache_max_retention)`.

---

## debug (bool)

Enable structured JSON debug logging for troubleshooting and service development. Default: `false`.

When enabled (via config or the `--debug` CLI flag):
- Creates JSON Lines (`.jsonl`) log files with complete debugging context
- Logs: session info, CLI params, service config, CDM details, authentication, titles, tracks metadata,
  DRM operations, vault queries, errors with stack traces
- File location: `logs/unshackle_debug_{service}_{timestamp}.jsonl`

---

## debug_keys (bool)

Log decryption keys in debug logs. Default: `false`.

When `true`, actual content encryption keys (CEKs) are included in debug log output. Useful for
debugging key retrieval and decryption issues.

**Security note:** Passwords, tokens, cookies, and session tokens are always redacted regardless
of this setting. Only content keys (`content_key`, `key` fields) are affected. Key IDs (`kid`),
key counts, and other metadata are always logged.

---
