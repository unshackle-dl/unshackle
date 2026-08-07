# Other Settings

Smaller configuration areas: external metadata API keys, caching and update checks, logging and debug flags, and deprecated keys.

## External API keys { #external-api-keys }

All are unset by default and enable optional metadata/geolocation features.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `imdb_api_enabled` | bool | `false` | Use the free IMDxAPI (api.tiffara.com) metadata provider, which needs no API key. Off by default since the site has been unreliable. |
| `omdb_api_key` | str | `""` | [OMDb](https://www.omdbapi.com/) API key for IMDb metadata lookups; a more reliable alternative to IMDxAPI. Free keys are available on the OMDb site. |
| `tmdb_api_key` | str | `""` | TMDB API key for metadata enrichment and external-ID tags. |
| `simkl_client_id` | str | `""` | SIMKL client ID for metadata lookups; an alternative/fallback source to TMDB. |
| `tvdb_api_key` | str | `""` | [TheTVDB v4](https://thetvdb.com/api-information) API key; a fallback source to TMDB, strongest on TV series. Free keys are available on the TVDB site. |
| `tvdb_pin` | str | `""` | Subscriber PIN, only needed for a user-supported TVDB key. Leave empty for a normal project key. |
| `tvdb_order` | str | `""` | Default for `--tvdb-order`: renumber episodes to a TVDB season order (`official`, `dvd`, `absolute`, `alternate`, `regional`). Empty keeps the numbering the service gives. |
| `metadata_providers` | list | *(see below)* | Metadata providers to use, in the order they are tried. |
| `decrypt_labs_api_key` | str | `""` | Global Decrypt Labs API key (used by remote CDM / vault). |
| `ipinfo_api_key` | str | `""` | ipinfo.io API key for IP/region lookups. |

!!! note "How `metadata_providers` is applied"
    A lookup goes to each provider in turn and stops at the first good match, so the order
    decides which source wins. `metadata_providers` sets that order:

    ```yaml
    metadata_providers: [tvdb, tmdb, simkl]
    ```

    Names not in the list are never used, so this both orders and filters. Unknown names are
    ignored with a warning. Leave it unset for the default order:

    ```yaml
    metadata_providers: [imdbapi, omdb, simkl, tmdb, tvdb]
    ```

    Whatever the order, a provider is skipped when its API key is missing (or, for
    `imdbapi`, when `imdb_api_enabled` is `false`). Available names are
    `imdbapi`, `omdb`, `simkl`, `tmdb`, and `tvdb`.

!!! note "`ipinfo_api_key` never touches your service sessions"
    The token is only ever sent to `api.ipinfo.io` as a per-request `Authorization` header; it
    is **never** attached to your service session, so it cannot leak to a streaming provider.
    Lookups degrade gracefully through a fallback chain: the authenticated Lite endpoint (higher
    rate limits, and ASN/continent data, but no city or region) → anonymous ipinfo →
    `ip-api.in` as a last resort.

---

## Caching & updates { #caching-updates }

### Title cache

unshackle caches fetched title metadata (region- and account-aware) to avoid repeat API
calls.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `title_cache_enabled` | bool | `true` | Master switch for the title cache. |
| `title_cache_time` | int (seconds) | `1800` (30 min) | Lifetime of fresh cached titles. |
| `title_cache_max_retention` | int (seconds) | `86400` (24 h) | Total retention window for a cached title, counted from when it was cached. Past `title_cache_time` the stale copy is only served as a fallback when a live fetch fails. |

### Update checks

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `update_checks` | bool | `true` | Whether to check for new unshackle releases. |
| `update_check_interval` | int (hours) | `24` | Minimum hours between update checks. |

!!! note
    Update checks query the GitHub releases API with a fixed 5-second timeout and cache the
    result in `directories.cache/update_check.json`.

---

## Logging, privacy & debug { #logging-privacy-debug }

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `redact_paths` | bool | `true` | Mask install-root / venv / home prefixes in logged paths (`<unshackle>`, `<venv>`, `~`). Set `false` to show full paths. |
| `debug` | bool | `false` | Global debug mode. |
| `debug_keys` | bool | `false` | Log decryption keys. |
| `debug_requests` | bool | `false` | Log HTTP requests. |
| `set_terminal_bg` | bool | `false` | Append the theme's background colour to output styling. |

!!! note "Effect of `set_terminal_bg`"
    When on, the theme's background colour is appended to the foreground styles, which makes
    the full colour palette render correctly on terminals whose *default* background differs
    from unshackle's theme. When off, such terminals can render the ASCII-art banner and
    coloured output incorrectly.

!!! danger "Key exposure"
    With `debug_keys` on, decryption content keys are written to the structured debug log at
    INFO level during the DRM handshake. Treat `debug`, `debug_keys`, and the `unshackle_debug_*.jsonl` files as
    sensitive, and keep `redact_paths` enabled when sharing logs.

    `debug_keys` affects **only** content-encryption keys (the `content_key`/`key` fields).
    Passwords, tokens, cookies, and session tokens are **always** redacted regardless of this
    setting, and KIDs, key counts, and other metadata are always logged either way.

---

## Deprecated & removed keys { #deprecated-removed-keys }

| Key | Status | Behaviour |
|-----|--------|-----------|
| `curl_impersonate` | **Deprecated** → use [`network`](network.md) | Emits a `DeprecationWarning`; still honoured only if `network` is absent. |
| `downloader` | **Deprecated** | Any value other than `"requests"` emits a `DeprecationWarning`; the value is otherwise ignored (the unified requests downloader is always used). |
| `scene_naming` | **Removed** | If set to any non-null value, unshackle exits with an error directing you to configure [`output_template`](output.md) instead. |
