# Configuration Reference

Every key you can set in `unshackle.yaml`, with its name, type, default value, and behaviour,
split into one page per area. A worked config is on the [annotated example](example.md) page.

Where the file lives, how unshackle discovers it, and how to edit it:
[The Configuration File](../../getting-started/configuration-file.md).

!!! info "How defaults work"
    Every key is optional except [`output_template`](output.md), which `dl` requires.
    Without it, `dl` aborts with an error. If you omit any other key, unshackle uses
    the built-in default listed here. There is **no schema validation**: unknown keys are
    silently ignored (they never become settings), and the file is parsed with
    `yaml.safe_load`. An empty or missing file means every other key falls back to its
    default.

!!! warning "Two keys behave specially on load"
    - `curl_impersonate` is a **deprecated** alias of [`network`](network.md) and emits a
      `DeprecationWarning`.
    - `scene_naming` has been **removed**. If it is set to any value, unshackle exits
      immediately with an error telling you to use [`output_template`](output.md)
      instead.

    See [Deprecated & removed keys](misc.md#deprecated-removed-keys).

---

## Quick index

| Area | Keys |
|------|------|
| [Directories](directories.md) | `directories`, `filenames` |
| [Services & authentication](services.md) | `services`, `credentials`, `firefox_cookies`, `remote_services`, `serve` |
| [Download & processing](download.md) | `dl`, `subtitle`, `audio`, `muxing`, `language_tags`, `dual_multi_mode` |
| [Output & naming](output.md) | `output_template`, `tag`, `tag_rules`, `tag_group_name`, `tag_imdb_tmdb`, `chapter_fallback_name`, `unicode_filenames` |
| [DRM & CDM](drm.md) | `cdm`, `remote_cdm`, `decryption` |
| [Network & proxy](network.md) | `network`, `headers`, `proxy_providers` |
| [Key vaults](vaults.md) | `key_vaults`, `vault_timeout` |
| [External API keys](misc.md#external-api-keys) | `omdb_api_key`, `tmdb_api_key`, `tvdb_api_key`, `tvdb_pin`, `tvdb_order`, `simkl_client_id`, `metadata_providers`, `anilist_title_language`, `disable_metadata`, `decrypt_labs_api_key`, `ipinfo_api_key` |
| [Caching & updates](misc.md#caching-updates) | `title_cache_enabled`, `title_cache_time`, `title_cache_max_retention`, `update_checks`, `update_check_interval` |
| [Logging, privacy & debug](misc.md#logging-privacy-debug) | `redact_paths`, `debug`, `debug_keys`, `debug_requests`, `set_terminal_bg` |
| [Deprecated & removed](misc.md#deprecated-removed-keys) | `curl_impersonate`, `downloader`, `scene_naming` |
