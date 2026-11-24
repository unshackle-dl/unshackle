# Other Settings

Smaller configuration areas: external metadata API keys, caching and update checks, logging and debug flags, and deprecated keys.

## External API keys { #external-api-keys }

All are unset by default and enable optional metadata/geolocation features. For which fields
each metadata provider can answer with, see
[What each metadata provider supplies](../../guide/downloading.md#what-each-metadata-provider-supplies).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `omdb_api_key` | str | `""` | [OMDb](https://www.omdbapi.com/) API key for IMDb metadata lookups; a fallback for when the keyless `imdb` provider has no answer. Free keys are available on the OMDb site. |
| `tmdb_api_key` | str | `""` | TMDB API key for metadata enrichment and external-ID tags. |
| `simkl_client_id` | str | `""` | SIMKL client ID for metadata lookups; an alternative/fallback source to TMDB. |
| `tvdb_api_key` | str | `""` | [TheTVDB v4](https://thetvdb.com/api-information) API key; a fallback source to TMDB, strongest on TV series. Also the source `--enrich --tvdb` reads. Free keys are available on the TVDB site. |
| `tvdb_pin` | str | `""` | Subscriber PIN, only needed for a user-supported TVDB key. Leave empty for a normal project key. |
| `tvdb_order` | str | `""` | Default for `--tvdb-order`: renumber episodes to a TVDB season order (`official`, `dvd`, `absolute`, `alternate`, `regional`). Empty keeps the numbering the service gives. |
| `metadata_providers` | list or map | *(see below)* | Metadata providers to use, in the order they are tried. |
| `anilist_title_language` | str | `english` | Which AniList title variant to use: `english`, `romaji`, or `native`. |
| `disable_metadata` | bool | `false` | Set to `true` to stop all automatic metadata lookups. An ID you give yourself is still looked up. |
| `decrypt_labs_api_key` | str | `""` | Global Decrypt Labs API key (used by remote CDM / vault). |
| `ipinfo_api_key` | str | `""` | ipinfo.io API key for IP/region lookups. |

!!! note "How `metadata_providers` is applied"
    A lookup goes to each metadata provider in turn and stops at the first good match, so the order
    decides which source wins. `metadata_providers` sets that order:

    ```yaml
    metadata_providers: [tvdb, tmdb, simkl]
    ```

    Names not in the list are never used, so this both orders and filters. Unknown names are
    ignored with a warning. Leave it unset for the default order:

    ```yaml
    metadata_providers: [imdb, omdb, simkl, tmdb, tvdb, anilist]
    ```

    Whatever the order, unshackle skips a metadata provider that has no API key. `imdb` and
    `anilist` need no API key, so they are always available. Available names are `imdb`, `omdb`,
    `simkl`, `tmdb`, `tvdb`, and `anilist`. The old name `imdbapi` still works and is read as
    `imdb`.

    `anilist` is last in the default order. It only answers for anime and returns nothing for
    anything else, so it is harmless there. Move it earlier if you mostly download anime.

    The order applies to a title search and to a direct lookup of an ID you give with
    `--tmdb`, `--imdb`, `--tvdb`, or `--anilist`. A supplied ID always goes to the providers
    that read that kind of ID, and it never falls back to a title search.

!!! note "Which AniList title `anilist_title_language` picks"
    AniList holds up to three titles for a work: the English one, the romaji transliteration,
    and the native one. This option says which of them unshackle uses:

    ```yaml
    anilist_title_language: romaji
    ```

    A work does not always have all three. When the one you asked for is unset on AniList's
    side, unshackle falls back in the order english → romaji → native. It takes the first one
    that is set, so you always get a title.

!!! note "What `disable_metadata` turns off"
    With `disable_metadata: true`, unshackle never contacts a metadata provider on its own.
    The title search is dead, and that includes the keyless `imdb` metadata provider. Titles, years and
    languages then come only from the service.

    ```yaml
    disable_metadata: true
    ```

    An ID you give with `--tmdb`, `--imdb`, `--tvdb`, or `--anilist` is your permission to use
    that ID. That lookup still works. unshackle looks the ID up directly, cross references the other
    IDs from it, and `--enrich` still reads that source. unshackle stops only the lookups you
    did not ask for.

!!! note "The `imdb` provider needs no key"
    It replaces the earlier `imdbapi` metadata provider, which read a third-party mirror
    (api.tiffara.com) and needed the `imdb_api_enabled` switch. That option is gone: delete it
    from your config, where it is now ignored.

!!! tip "A different order per title kind"
    Give a map instead of one list when the best source differs by kind. TVDB is strongest on
    series, TMDB on films. There are two kinds, `tv` and `movie`, and this example sets both
    of them and names every metadata provider, so nothing is left to the default:

    ```yaml
    metadata_providers:
      tv: [tvdb, tmdb, simkl, imdb, omdb]
      movie: [tmdb, imdb, omdb, simkl, tvdb]
    ```

    Each list is independent: it both orders and filters that kind on its own, so unshackle
    still uses for `movie` a metadata provider you leave out of `tv`. Here unshackle tries TVDB
    first for a series and last for a film.

    You do not need to give both keys, or a full list in each. A kind you leave out uses the
    default order:

    ```yaml
    metadata_providers:
      tv: [tvdb, tmdb]     # movies keep the default order
    ```

    `tv` covers every episode and `movie` covers every film. unshackle only looks up
    metadata for those two kinds, so unshackle ignores any other config key, such as `anime` or `music`.

!!! note "`ipinfo_api_key` never touches your service sessions"
    unshackle sends the token only to `api.ipinfo.io`, as a per-request `Authorization` header.
    It **never** goes on your service HTTP session, so it cannot leak to a streaming service.
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

## Appearance { #appearance }

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `theme` | str | `catppuccin-mocha` | Named colour theme for all CLI output: banner, logs, progress bars, and help screens. One of `catppuccin-mocha` (aliases `default`, `catppuccin`, `mocha`), `dracula`, `nord`, `gruvbox`, `one-dark` (alias `onedark`), `tokyo-night` (alias `tokyonight`), `monokai`, `flexoki`, `solarized-dark` (alias `solarized`). Case-insensitive; underscores work in place of hyphens. Unknown names fall back to the default with a warning. Preview them all with `unshackle env theme`. |

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
    When on, unshackle appends the theme's background colour to the foreground styles, which makes
    the full colour palette render correctly on terminals whose *default* background differs
    from unshackle's theme. When off, such terminals can render the ASCII-art banner and
    coloured output incorrectly.

!!! danger "Key exposure"
    With `debug_keys` on, unshackle writes the content keys to the structured debug log at
    INFO level during the DRM handshake. Treat `debug`, `debug_keys`, and the `unshackle_debug_*.jsonl` files as
    sensitive, and keep `redact_paths` enabled when sharing logs.

    `debug_keys` affects **only** content-encryption keys (the `content_key`/`key` fields).
    Passwords, tokens, cookies, and session tokens are **always** redacted regardless of this
    setting, and KIDs, content key counts, and other metadata are always logged either way.

---

## Deprecated & removed keys { #deprecated-removed-keys }

| Key | Status | Behaviour |
|-----|--------|-----------|
| `curl_impersonate` | **Deprecated** → use [`network`](network.md) | Emits a `DeprecationWarning`; still honoured only if `network` is absent. |
| `downloader` | **Deprecated** | Any value other than `"requests"` emits a `DeprecationWarning`; the value is otherwise ignored (the unified requests downloader is always used). |
| `scene_naming` | **Removed** | If set to any non-null value, unshackle exits with an error directing you to configure [`output_template`](output.md) instead. |
| `imdb_api_enabled` | **Removed** | Ignored. It gated the old `imdbapi` provider, which the keyless [`imdb`](#external-api-keys) provider replaces. Safe to delete from your config. |
