# Configuration Reference

This page is the exhaustive, authoritative reference for **every** key you can set in
`unshackle.yaml`. For each key you will find its name, expected type, default value, and
what it does. Keys are grouped by area so you can jump straight to the part of unshackle
you are configuring.

If you are new to the config file (where it lives, how unshackle discovers it, and how to
edit it), start with [The Configuration File](../getting-started/configuration-file.md).
This page assumes you already know the basics and want precise details.

!!! info "How defaults work"
    Every key is optional. If you omit a key, unshackle uses the built-in default listed
    here. There is **no schema validation**: unknown keys are silently ignored (they never
    become settings), and the file is parsed with `yaml.safe_load`. An empty or missing
    file means *all* defaults apply.

!!! warning "Two keys behave specially on load"
    - `curl_impersonate` is a **deprecated** alias of [`network`](#network-proxy) and emits a
      `DeprecationWarning`.
    - `scene_naming` has been **removed**. If it is present at all, unshackle exits
      immediately with an error telling you to use [`output_template`](#output-naming)
      instead.

    See [Deprecated & removed keys](#deprecated-removed-keys) at the end of this page.

---

## Quick index

| Area | Keys |
|------|------|
| [Directories](#directories) | `directories`, `filenames` |
| [Services & authentication](#services-auth) | `services`, `credentials`, `firefox_cookies`, `remote_services`, `serve` |
| [Download & processing](#download-processing) | `dl`, `subtitle`, `audio`, `muxing`, `language_tags` |
| [Output & naming](#output-naming) | `output_template`, `tag`, `tag_group_name`, `tag_imdb_tmdb`, `chapter_fallback_name`, `unicode_filenames` |
| [DRM & CDM](#drm-cdm) | `cdm`, `remote_cdm`, `decryption` |
| [Network & proxy](#network-proxy) | `network`, `headers`, `proxy_providers` |
| [Key vaults](#key-vaults) | `key_vaults`, `vault_timeout` |
| [External API keys](#external-api-keys) | `imdb_api_enabled`, `tmdb_api_key`, `simkl_client_id`, `decrypt_labs_api_key`, `ipinfo_api_key` |
| [Caching & updates](#caching-updates) | `title_cache_enabled`, `title_cache_time`, `title_cache_max_retention`, `update_checks`, `update_check_interval` |
| [Logging, privacy & debug](#logging-privacy-debug) | `redact_paths`, `debug`, `debug_keys`, `debug_requests`, `set_terminal_bg` |
| [Deprecated & removed](#deprecated-removed-keys) | `curl_impersonate`, `downloader`, `scene_naming` |

---

## Directories { #directories }

The `directories` key is a **dict** mapping directory names to filesystem paths. Only the
names listed below are honoured; a handful are **protected** and cannot be moved (attempts
to override them are silently skipped). All defaults are computed relative to the installed
package, and every user-settable path is passed through `Path(...).expanduser()`, so `~`
works.

```yaml title="unshackle.yaml"
directories:
  downloads: ~/Media/unshackle
  temp: /mnt/fast-scratch/unshackle-temp
  wvds: ~/.unshackle/WVDs
  prds: ~/.unshackle/PRDs
```

| Key | Type | Default | Overridable | Purpose |
|-----|------|---------|:-----------:|---------|
| `downloads` | path | `<repo>/downloads` | ✅ | Default output directory for finished files. |
| `temp` | path | `<repo>/temp` | ✅ | Temporary working files during download/decrypt/mux. |
| `cache` | path | `<data>/cache` | ✅ | Generic cache, title cache, and the update-check store. |
| `cookies` | path | `<data>/cookies` | ✅ | Per-service cookie files (and VPN cookie/token files). |
| `logs` | path | `<data>/logs` | ✅ | Log files. |
| `exports` | path | `<data>/exports` | ✅ | Export JSON files. |
| `wvds` | path | `<data>/WVDs` | ✅ | Widevine device files (`.wvd`). |
| `prds` | path | `<data>/PRDs` | ✅ | PlayReady device files (`.prd`). |
| `dcsl` | path | `<data>/DCSL` | ✅ | DCSL data. |
| `commands` | path | `unshackle/commands` | ✅ | CLI command modules. |
| `services` | list \| path | `[unshackle/services]` | ✅ | Service search paths and/or remote repo specs (see below). |
| `vaults` | path | `unshackle/vaults` | ✅ | Vault backend modules. |
| `fonts` | path | `unshackle/fonts` | ✅ | Bundled fonts. |
| `user_configs` | path | `unshackle/` | ❌ protected | Where `unshackle.yaml` lives. |
| `data` | path | `unshackle/` | ❌ protected | Base for the data subdirectories above. |
| `core_dir` | path | `unshackle/core` | ❌ protected | Package core. |
| `namespace_dir` | path | `unshackle/` | ❌ protected | Package root. |
| `app_dirs` | - | `AppDirs("unshackle", False)` | ❌ protected | Internal AppDirs instance. |

!!! note "The `services` directory is special"
    `services` may be a **list**, and each entry can be either a local directory or a
    **repository spec**: a git URL (`https://...`, `ssh://...`, `git@...`, or anything ending in
    `.git`) or `owner/repo` shorthand. Repo specs are cloned and updated automatically; plain
    paths are used as-is. **List order is priority**: the first source to define a service
    tag wins, so put your own local service directories **last** to make them fallbacks.

    ```yaml
    directories:
      services:
        - unshackle-dl/community-services   # cloned from GitHub
        - https://example.com/private.git@stable
        - ~/my-services                      # local, lowest priority
    ```

!!! info "How git-backed service repos are handled"
    unshackle's use of git is **read-only on the remote**: it only ever performs a shallow
    `clone`, `fetch`, `pull`, and a local `reset`; **nothing is ever pushed**. Private repos
    are authenticated through your existing git credential helper, and unshackle stores no
    tokens of its own. Clones live under `<first-local-services-dir>/_repos/<repo-name>/` (or
    the bundled `unshackle/services`), and **nothing is written to the cache directory**. After
    the first clone unshackle re-pulls **at most once every 24 h**, so it does not touch the
    network on every run.

!!! warning "The two refresh paths diverge on purpose"
    The **automatic** 24 h-TTL refresh that happens during a normal `dl`/`search` run will
    deliberately **refuse to refresh and exit** (naming the offending clone) if that clone
    has uncommitted changes to tracked files or unpushed local commits, so a background pull can
    never clobber work in progress. Untracked files (new service folders, `__pycache__`) never
    block it. The **manual** `unshackle util refresh-services` command does the opposite: it
    **hard-resets** the clone to upstream, discarding any local edits. Reach for the manual
    command only when you actually want to throw local changes away.

!!! warning "Read-only installs and reinstalls"
    If the services directory lives inside the installed package, a reinstall can delete the
    `_repos` clones. They are simply re-cloned on next use. On **read-only installs** you must
    point `services` at a writable path, or cloning will fail.

### Filenames { #filenames }

The `filenames` key is a **dict** of templated file/name patterns. Each value is used
verbatim (no path processing). Braced fields like `{time}` and `{service}` are filled in at
runtime.

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `log` | str | `"unshackle_{name}_{time}.log"` | Written under `directories.logs`. |
| `debug_log` | str | `"unshackle_debug_{service}_{time}.jsonl"` | Structured debug log, under `directories.logs`. |
| `config` | str | `"config.yaml"` | Per-service config file, under that service's directory. |
| `root_config` | str | `"unshackle.yaml"` | The main config filename itself. |
| `chapters` | str | `"Chapters_{title}_{random}.txt"` | Under `directories.temp`. |
| `subtitle` | str | `"Subtitle_{id}_{language}.srt"` | Under `directories.temp`. |

---

## Services & authentication { #services-auth }

### `services`

- **Type:** `dict` keyed by service tag &nbsp;·&nbsp; **Default:** `{}`

Per-service configuration, keyed by the canonical service tag (for example `EXAMPLE1`, `EXAMPLE2`,
`EXAMPLE3`). unshackle reads several well-known sub-keys out of each service's block:

- **`proxy_map`**: a dict mapping `"provider:query"` or `"query"` to a proxy value, used to
  auto-select a proxy for that service.
- **`title_map`**: an exact-match rename map applied to fetched titles (source name →
  desired name), so a service that names a title differently from your library still matches.

Individual services may read any additional keys they define. The whole block is handed to
the service as `self.config`.

!!! tip "Why put secrets here and not in the service's own `config.yaml`?"
    A service ships a per-service `config.yaml` inside its own folder, and that file is
    typically shared/committed and **not** meant to be edited by you. So anything user- or
    device-specific (API keys, account IDs, device attributes) belongs in this `services`
    block of `unshackle.yaml` instead, where it is **merged over** the service's `config.yaml`
    at load time. That is why the two config locations exist: one for shared service defaults,
    one for your private overrides.

```yaml
services:
  EXAMPLE1:
    proxy_map:
      default: us
    title_map:
      "My Show: The Movie": "My Show Presents The Movie"
```

### `credentials`

- **Type:** `dict` keyed by service tag &nbsp;·&nbsp; **Default:** `{}`

Per-service login credentials. Each value is a `username:password[:extra]` string (a list or
dict form is also accepted where a service supports it). These are parsed into `Credential`
objects; the credential's SHA-1 is also used as an account hash for cache keys.

```yaml
credentials:
  EXAMPLE2: user@example.com:hunter2
  EXAMPLE1:
    - primary@example.com:pw1
    - secondary@example.com:pw2
```

!!! tip "Cookies vs credentials"
    Cookies are stored as files under [`directories.cookies`](#directories), not in this key.
    A service's `authenticate()` accepts cookies, credentials, or both.

### `firefox_cookies`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Settings for extracting cookies directly from a local Firefox profile. A service block is
expected to provide `hosts` (a list of cookie hostnames; entries shorter than 3 characters
are ignored to prevent dumping the whole cookie store) and an optional `local_storage`
boolean to also pull matching local-storage entries. Extraction is read-only.

!!! note "Firefox does not need to be closed"
    The extractor copies **both** `cookies.sqlite` **and** its WAL file into a `0700` temp
    directory; the WAL copy exists to capture writes Firefox has not yet flushed to the main
    DB, so the copy is current even against a running browser. The only real constraint is that
    Firefox must not hold an exclusive write lock at the exact instant of the copy. The live
    profile is otherwise left untouched.

!!! tip "When you actually need `local_storage: true`"
    Enable it only for services that keep their auth tokens in the browser's **localStorage**
    (`webappsstore.sqlite`) rather than in HTTP cookies. If a service authenticates with plain
    cookies, leaving it off is correct.

!!! warning "Extraction fails silently to file cookies"
    If extraction yields no cookies or fails for **any** reason, unshackle silently falls back
    to the normal file-based cookie path (`cookies/<SERVICE>.txt` or
    `cookies/<SERVICE>/<profile>.txt`). This is a safety net, but it also means that if you
    expected Firefox cookies and file cookies are being used instead, extraction quietly failed.
    Check that first when troubleshooting.

### `remote_services`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Definitions of remote unshackle service servers, used by the `--remote` mode. Each entry
carries a `services` sub-dict of remote service tags, which unshackle turns into synthetic
CLI commands that run against the remote server.

### `serve`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Configuration for the `serve` command (the built-in REST API server). The full server guide
is the [REST API](../dev/rest-api/index.md) section; these are the config keys.

| Sub-key | Type | Default | Description |
|---------|------|---------|-------------|
| `api_secret` | str | *(unset)* | Master secret accepted in the `X-Secret-Key` header. |
| `users` | dict | `{}` | Per-user API keys and their allowlists (see below). |
| `services` | list | *(unset)* | Global service allowlist. Omit to allow all. |
| `remote_only` | bool | `false` | Run without a local `output_template` (CORS/Cloudflare-friendly). |
| `session_ttl` | int (s) | `300` | Lifetime of an interactive auth session. |
| `max_sessions` | int | `100` | Maximum concurrent sessions. |
| `history_limit` | int | `100` | How many finished jobs to retain in history. |
| `compression_level` | int | `1` | gzip level for responses. |
| `cdm_overrides` | dict | *(unset)* | Allowed per-request CDM overrides. |
| `allow_job_credentials` | bool | `false` | Permit clients to supply credentials per job. |
| `devices` | list | *(auto)* | Widevine devices offered; auto-filled from `directories.wvds`. |
| `playready_devices` | list | *(auto)* | PlayReady devices; auto-filled from `directories.prds`. |
| `country` / `locations` | str / dict | *(unset)* | Advertised region metadata. |

Each entry under `users` is keyed by that user's API key and may set its own `services`,
`devices`, and `playready_devices` allowlists, narrowing the global ones.

```yaml
serve:
  api_secret: change-me
  remote_only: true
  services: [EXAMPLE1, EXAMPLE2]
  # server-wide download defaults (same keys as the `dl:` block)
  downloads: 3
  best_available: true
  users:
    a1b2c3d4:                 # this user's API key
      services: [EXAMPLE1]        # may only use EXAMPLE1
```

!!! tip "Declaring `dl` defaults under `serve`"
    You can set any [`dl`](#dl) flag key (`downloads`, `workers`, `best_available`, and so on)
    directly inside `serve`. The point is to raise concurrency and behaviour defaults
    **server-wide** (every request the server handles picks them up) without having to change
    each client call individually.

---

## Download & processing { #download-processing }

### `dl`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Persistent defaults for the `dl` command. This block is used as Click's `default_map`, so
**any** `dl` flag can be given a default here and it applies to every download without you
typing it. See [Downloading](../guide/downloading.md) and the
[CLI Reference](../guide/cli-reference.md) for what each flag does.

**Key naming.** A key is the flag's long name with dashes replaced by underscores
(`--best-available` → `best_available`, `--sub-format` → `sub_format`). A few keys are named
after the flag's internal destination rather than its visible name; set these exact keys:

| Flag | Config key |
|------|-----------|
| `-r` / `--range` | `range` |
| `--list` | `list` |
| `--tmdb` | `tmdb_id` |
| `--imdb` | `imdb_id` |
| `--animeapi` | `animeapi_id` |
| `-naa` / `--noatmos` | `no_atmos` |
| `-o` / `--output` | `output_dir` |
| `--cdm-only` / `--vaults-only` | `cdm_only` |

**Precedence** (highest first): an explicit CLI flag or environment variable → a per-service
[`services.<TAG>.dl`](#services-auth) override → this global `dl:` block → the built-in
default. In other words, config only fills in options you did not set on the command line.

Common keys (a useful subset; every `dl` flag works):

| Key | Type | Default | Sets |
|-----|------|---------|------|
| `lang` | list | `["orig"]` | Video/audio language(s); `orig` = original language. |
| `a_lang` / `v_lang` | list | `[]` | Audio- / video-only language override. |
| `s_lang` | list | `["all"]` | Subtitle language(s). |
| `quality` | list | `[]` (best) | Resolution(s), e.g. `[1080]`. |
| `vcodec` | list | `[]` (any) | Video codec(s), e.g. `[H265]`. |
| `acodec` | list | `[]` (any) | Audio codec(s), e.g. `[EC3]`. |
| `range` | list | `["SDR"]` | Colour range(s): `SDR`, `HDR10`, `HDR10+`, `DV`. |
| `channels` | float | *(unset)* | Audio channels, e.g. `6` for 5.1. |
| `sub_format` | str | *(unset)* | Convert subtitles to this format (`srt`, `vtt`, `original`, ...). |
| `forced_subs` | bool | `false` | Include forced subtitle tracks. |
| `no_subs` / `no_audio` / `no_chapters` | bool | `false` | Skip that track type. |
| `downloads` | int | `1` | Tracks downloaded concurrently. |
| `workers` | int | *(downloader default)* | Threads per track. |
| `slow` | str | *(unset)* | Inter-title delay, e.g. `"20-40"`. |
| `best_available` | bool | `false` | Fall back to best quality if the request is unavailable. |
| `proxy` | str | *(unset)* | Default proxy URI or 2-letter country. |
| `no_folder` | bool | `false` | Do not create a per-title folder. |

```yaml title="Global download defaults"
dl:
  lang: [en]
  quality: [1080]
  range: [HDR10]
  vcodec: [H265]
  sub_format: srt
  downloads: 2
```

!!! tip "Per-service overrides"
    Nest a `dl` block under a service in [`services`](#services-auth) to override these defaults
    for just that service. It uses the same keys and wins over the global `dl:` block (but still
    loses to an explicit CLI flag).

    ```yaml
    dl:
      lang: [en]          # global default
    services:
      EXAMPLE1:
        dl:
          lang: [en, ja]  # EXAMPLE1 downloads both; everything else stays English
          range: [DV]
    ```

!!! note "`serve` uses these same keys"
    The [`serve`](#serve) block accepts the same `dl` option keys at its top level to set
    server-wide download defaults for every request it handles.

### `subtitle`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Subtitle handling: SDH stripping, format conversion, and whether subtitles are muxed in or
written as sidecar files. See [Subtitles](../guide/subtitles.md) for the full guide.

| Sub-key | Type | Default | Description |
|---------|------|---------|-------------|
| `strip_sdh` | bool | `true` | Strip SDH/CC cues (hearing-impaired annotations) into a clean track. |
| `sdh_method` | str | `"auto"` | SDH-stripping backend to use. |
| `preserve_formatting` | bool | `true` | Keep styling/positioning when converting or stripping. |
| `convert_before_strip` | bool | `true` | Convert to the working format before stripping SDH. |
| `conversion_method` | str | `"auto"` | Subtitle conversion backend. |
| `preferred_conversion_method` | str | *(unset)* | Preferred converter when several are available. |
| `output_mode` | str | `"mux"` | `mux` embeds subtitles in the MKV; `sidecar` writes separate files. |
| `sidecar_format` | str | `"srt"` | Format for sidecar files when `output_mode: sidecar`. |
| `type_priority` | list | *(unset)* | Ordered ranking of subtitle types (`forced`, `normal`, `sdh`; CC counts as SDH) that sorts the variants within each language. Types you leave out fall to the end. |

```yaml
subtitle:
  strip_sdh: true
  output_mode: sidecar
  sidecar_format: srt
```

!!! note "`type_priority` also picks the default subtitle"
    `type_priority` sorts the tracks within each language, and at mux time the first track
    in the default subtitle language gets the `default` flag. The built-in order is `forced`,
    `normal`, `sdh`, which means a forced track becomes default whenever one exists. If you
    want the full dialogue track as default instead, set:

    ```yaml
    subtitle:
      type_priority: [normal, sdh, forced]
    ```

### `audio`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Audio handling options. The most notable sub-key is **`codec_priority`**, an ordered list of
audio codecs used to break ties in track selection.

```yaml
audio:
  codec_priority:
    - EAC3
    - AC3
```

!!! note "`codec_priority` is a soft priority: nothing is dropped"
    `codec_priority` only breaks ties **among tracks that share the same bitrate and language**.
    Codecs not in the list keep their normal bitrate-based ordering and are simply placed after
    all listed codecs, and none are ever removed. Two higher-level rules still override it: **Atmos
    tracks always take precedence over `codec_priority`**, and **audio-description tracks are
    always forced to the end** regardless of it. When `codec_priority` is unset, tracks sort by
    bitrate alone, with those same Atmos/descriptive rules still applied.

### `muxing`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Matroska (MKV) muxing options.

| Sub-key | Type | Default | Description |
|---------|------|---------|-------------|
| `set_title` | bool | `true` | Write a human-readable title into the MKV container. |
| `default_language` | dict | `{}` | Force which language is flagged *default* per track type, e.g. `{audio: en, subtitle: en}`. |
| `merge_audio` | bool | `true` | Merge all audio into one file. `--split-audio` on the CLI flips this off. |
| `merge_video` | bool | `false` | Merge all video tracks into one file. `--merge-video` on the CLI flips this on. |

```yaml
muxing:
  set_title: true
  default_language:
    audio: en
    subtitle: en
```

### `language_tags`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Language-tag remapping and the rule engine behind the `{lang_tag}` filename variable. When a
`rules` list is present, each rule is evaluated in order and the first match's `tag` is used.
A rule may test:

| Condition | Meaning |
|-----------|---------|
| `audio` | Matches against the audio track languages. |
| `subs_contain` | At least one subtitle language matches. |
| `subs_contain_all` | Every listed language must be present (scalar or list). |

```yaml
language_tags:
  rules:
    - audio: [en]
      subs_contain_all: [en]
      tag: ""            # native English release, no tag
    - audio: [ja]
      subs_contain: [en]
      tag: "SUBBED"
```

---

## Output & naming { #output-naming }

For the full picture of how templates render, see
[Output & naming](../guide/output-and-naming.md). The keys are summarised here.

### `output_template`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Filename templates keyed by media kind, using `{variable}` placeholders. Recognised kinds are
`movies`, `series`, and `songs` (and `albums` for folder templates). A trailing `?` on a
variable (e.g. `{quality?}`) marks it optional: if empty, it and one adjacent separator are
dropped.

```yaml title="unshackle.yaml"
output_template:
  movies: "{title}.{year}.{quality?}.{source}-{tag}"
  series: "{title}.{season_episode}.{episode_name?}.{quality?}.{source}-{tag}"
  songs: "{track_number}. {title}"
  folder:                       # special nested key, see below
    movies: "{title} ({year})"
    series: "{title} ({year})"
```

**The nested `folder` sub-key** controls output subfolders and is popped out of
`output_template`:

- A **dict** sets per-kind folder templates (kinds: `movies`, `series`, `songs`, `albums`).
- A **str** sets a single folder template for everything.

unshackle validates templates on load and emits warnings for unknown variables, non-string
values, filesystem-unsafe characters (`< > : " / \ | ? *`), empty templates, and unknown
folder-kind keys.

??? example "All valid template variables"
    `title`, `year`, `season`, `episode`, `season_episode`, `episode_name`, `date`,
    `quality`, `resolution`, `source`, `tag`, `track_number`, `artist`, `album_artist`,
    `album`, `disc`, `track_total`, `disc_total`, `release_type`, `genre`, `explicit`,
    `isrc`, `upc`, `label`, `audio`, `audio_channels`, `audio_full`, `atmos`, `dual`,
    `multi`, `video`, `hdr`, `hfr`, `edition`, `repack`, `lang_tag`.

### Tagging & naming keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `tag` | str | `""` | Release-group tag appended to filenames and written into MKV tags. |
| `tag_group_name` | bool | `true` | Include the group name (`tag`) in the MKV `Group` tag. |
| `tag_imdb_tmdb` | bool | `true` | Write IMDb/TMDB/TVDB external-ID tags into the MKV (needs metadata providers). |
| `chapter_fallback_name` | str | `""` | Fallback chapter-name template, e.g. `"Chapter {i:02}"`. |
| `unicode_filenames` | bool | `false` | Allow Unicode in filenames; when `false`, names are ASCII-sanitised. |

---

## DRM & CDM { #drm-cdm }

The full conceptual guide lives at [DRM & CDM](../guide/drm-and-cdm.md). These are the config
keys. Device files themselves live in [`directories.wvds`](#directories) (`.wvd`) and
`directories.prds` (`.prd`).

### `cdm`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Maps a service tag to the CDM (Widevine/PlayReady device) to use, with a `default` fallback.
Resolution is case-insensitive: an explicit CLI override wins, then the per-service entry,
then `default`.

```yaml
cdm:
  default: chromecdm_l3
  EXAMPLE2: android_l1
```

A value may also be a **nested dict** for advanced selection by quality, DRM system, or
profile:

```yaml
cdm:
  EXAMPLE1:
    ">=1080": android_l1        # by track height
    "<1080": chromecdm_l3
    widevine: android_l1        # by DRM system
    playready: sl3000
    default: chromecdm_l3
```

Quality keys support `1080`, `>=1080`, `>720`, `<=576`, `<480` style comparisons; DRM keys
are `widevine` / `playready`.

### `remote_cdm`

- **Type:** `list[dict]` &nbsp;·&nbsp; **Default:** `[]`

A list of remote CDM server definitions. Each entry is matched by its `name` (referenced from
[`cdm`](#cdm)), and its `type` selects the backend:

| `type` | Backend | Key fields |
|--------|---------|-----------|
| `decrypt_labs` | Decrypt Labs KeyXtractor | `host` (default `https://keyxtractor.decryptlabs.com`), `device_name`, `secret` (falls back to `decrypt_labs_api_key`) |
| `custom_api` | Fully YAML-configurable remote API | `device`, `auth`, `endpoints`, `request_mapping`, `response_mapping`, `caching`, `timeout` |
| *(none)*, `Device Type: PLAYREADY` | pyplayready `RemoteCdm` | `host`, `secret`, `device_name`, `security_level` (default 3000) |
| *(none)*, otherwise | pywidevine `RemoteCdm` | `host`, `secret`, `device_name`, `device_type`, `system_id`, `security_level` (default 3000) |

```yaml
remote_cdm:
  - name: keyxtractor
    type: decrypt_labs
    device_name: L1
  - name: my_wv_server
    host: https://cdm.example.com
    secret: s3cr3t
    device_name: android_l1
    device_type: ANDROID
    system_id: 26830
    security_level: 1
```

### `decryption`

- **Type:** `str` or `dict` &nbsp;·&nbsp; **Default:** `"shaka"`

Selects the tool used to physically decrypt CENC content. The value is compared
case-insensitively; `"mp4decrypt"` selects Bento4's mp4decrypt, and **anything else**
(including `"shaka"`) selects shaka-packager.

=== "Global"

    ```yaml
    decryption: shaka
    ```

=== "Per-service"

    ```yaml
    decryption:
      default: shaka
      SOMESERVICE: mp4decrypt
    ```

---

## Network & proxy { #network-proxy }

### `network`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

TLS-fingerprinting and HTTP client settings for the rnet-based session.

| Sub-key | Type | Default | Description |
|---------|------|---------|-------------|
| `browser` | str | `"Chrome131"` | Impersonation preset. Must be an exact preset name (e.g. `Chrome131`, `Firefox135`, `Edge101`, `Safari18`, `OkHttp4_12`, `OkHttp5`, `Opera118`); unknown names raise an error. |
| `http1_only` | bool | *(unset)* | Force HTTP/1.1. |
| `http2_only` | bool | *(unset)* | Force HTTP/2. |
| `pool_max_idle_per_host` | int | *(unset)* | Connection-pool tuning. |
| `pool_max_size` | int | *(unset)* | Connection-pool tuning. |
| `tcp_nodelay` | bool | *(unset)* | Disable Nagle's algorithm. |

```yaml
network:
  browser: Firefox135
  http2_only: true
```

!!! note "When to force `http1_only`"
    HTTP/2 is the right default on large CDNs, so leave it on. Force HTTP/1.1 only for a host that
    throttles per-connection, or a slow origin stuck behind HTTP/2 flow control. In benchmarks
    this won 30 to 50% on such hosts but **cost up to 27% on fast CDNs**, so enable it only when
    your own measurements justify it.

!!! warning "Renamed from `curl_impersonate`"
    The old `curl_impersonate` section is a deprecated alias. If you still use it, unshackle
    honours it (only when `network` is absent) but emits a `DeprecationWarning`. Rename it to
    `network`.

### `headers`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Default HTTP headers merged into every session unshackle creates.

```yaml
headers:
  Accept-Language: en-US,en;q=0.9
```

!!! warning "Don't set `Accept-Encoding` (and similar) here"
    Keep this block to sane cross-service defaults such as `Accept-Language` and `User-Agent`.
    Compatibility headers like `Accept-Encoding` are set for you by the rnet HTTP backend as
    part of its browser-impersonation profile, and overriding them can break the impersonation
    fingerprint. Anything useful to only some services belongs in that service's config, not
    here.

### `proxy_providers`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Proxy/VPN provider configuration. Each sub-key names a provider, and its block is passed
straight to that provider's constructor. See [Proxies & VPN](../guide/proxies-and-vpn.md) for
the full provider guide. Recognised providers and their exit ports:

| Provider | Config key | Credentials | Proxy scheme/port |
|----------|-----------|-------------|-------------------|
| Basic (static) | `basic` | country → URI(s) | as specified |
| NordVPN | `nordvpn` | 48-char **service** credentials | `https://...:89` |
| Surfshark | `surfsharkvpn` | 48-char **service** credentials | `https://...:443` |
| Windscribe | `windscribevpn` | service credentials | `https://...:443` |
| ExpressVPN | `expressvpn` | device login (`enable: true`) / token cache | `https://cat:...@...:443` |
| ProtonVPN | `protonvpn` | TV login or exported cookies | `https://...:4443` (or `:443` Secure Core) |
| Gluetun | `gluetun` | per-VPN keys/creds | `http://localhost:{port}` (local Docker) |
| Hola | *(none, auto)* | none | `http://...:{peer}` |

```yaml
proxy_providers:
  basic:
    us: http://user:pass@1.2.3.4:8080
    de:
      - http://a.example:8080
      - socks5://b.example:1080
  nordvpn:
    username: <48-char service username>
    password: <48-char service password>
```

!!! note "Provider loading differs between CLI and REST server"
    The `dl` CLI loads all providers, including `windscribevpn` and `gluetun`. The REST API /
    remote-client path uses a separate resolver that does **not** load `windscribevpn` or
    `gluetun`. ExpressVPN and ProtonVPN also auto-load when their cached session exists, and Hola
    auto-loads whenever the `hola-proxy` binary is present.

---

## Key vaults { #key-vaults }

The full guide is at [Vaults](../guide/vaults.md). Two keys configure them.

### `key_vaults`

- **Type:** `list[dict]` &nbsp;·&nbsp; **Default:** `[]`

An ordered list of key-vault backends. unshackle queries them in order and reuses content
keys instead of re-licensing. Each entry needs a `type` (the backend module name) and a
`name`, plus backend-specific keys.

| `type` | Purpose | Required keys | Notes |
|--------|---------|--------------|-------|
| `SQLite` | Local SQLite database | `name`, `path` | Loaded **critically**; a failure aborts the run. |
| `MySQL` | Remote MySQL database | `name`, `host`, `database`, `username` | Extra keys (e.g. `password`, `port`) forwarded to pymysql. |
| `API` | RESTful JSON API | `name`, `uri`, `token` | Honours `vault_timeout`. |
| `HTTP` | HTTP API with modes | `name`, `host`, one of `password`/`api_key` | `api_mode`: `query` (default), `json`, `decrypt_labs`. |

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

!!! tip "Common per-entry options"
    - `no_push: true` makes a vault read-only (keys are fetched but never written to it).
    - A vault of `type: api` whose `name` contains `decrypt_labs` auto-fills its `token` from
      [`decrypt_labs_api_key`](#external-api-keys) when not set inline.
    - An all-zero content key (32 zeros) is treated as "no key" everywhere and is never
      stored.

### `vault_timeout`

- **Type:** `float` &nbsp;·&nbsp; **Default:** `10.0`

Timeout in seconds for vault operations. Injected automatically into any backend whose
constructor accepts a `timeout` parameter (a per-vault `timeout` still wins).

---

## External API keys { #external-api-keys }

All default to an empty string and enable optional metadata/geolocation features.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `imdb_api_enabled` | bool | `false` | Use the free imdbapi.dev metadata provider, which needs no API key. Off by default since the site has been unreliable. |
| `tmdb_api_key` | str | `""` | TMDB API key for metadata enrichment and external-ID tags. |
| `simkl_client_id` | str | `""` | SIMKL client ID for metadata lookups; an alternative/fallback source to TMDB. |
| `decrypt_labs_api_key` | str | `""` | Global Decrypt Labs API key (used by remote CDM / vault). |
| `ipinfo_api_key` | str | `""` | ipinfo.io API key for IP/region lookups. |

!!! tip "SIMKL is a fallback for TMDB"
    SIMKL is positioned as an alternative metadata source, and it is most valuable precisely
    **when no `tmdb_api_key` is configured**. It improves title-matching and tagging
    reliability in that case rather than replacing TMDB where TMDB is available.

!!! note "`ipinfo_api_key` never touches your service sessions"
    The token is only ever sent to `api.ipinfo.io` as a per-request `Authorization` header; it
    is **never** attached to your service session, so it cannot leak to a streaming provider.
    Lookups degrade gracefully through a fallback chain: the authenticated Lite endpoint (higher
    rate limits and richer fields) → anonymous ipinfo → `ip-api.in` as a last resort.

---

## Caching & updates { #caching-updates }

### Title cache

unshackle caches fetched title metadata (region- and account-aware) to avoid repeat API
calls.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `title_cache_enabled` | bool | `true` | Master switch for the title cache. |
| `title_cache_time` | int (seconds) | `1800` (30 min) | Lifetime of fresh cached titles. |
| `title_cache_max_retention` | int (seconds) | `86400` (24 h) | How long stale cached data may still be served as a fallback when a live fetch fails. |

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

!!! tip "When to enable `set_terminal_bg`"
    When on, the theme's background colour is appended to the foreground styles so the full
    colour palette renders correctly on terminals whose *default* background differs from
    unshackle's theme. Turn it on if the ASCII-art banner or coloured output **looks off** on
    your terminal. Otherwise leave it off.

!!! danger "Key exposure"
    Decryption content keys are written to the structured debug log at INFO level during the
    DRM handshake. Treat `debug`, `debug_keys`, and the `unshackle_debug_*.jsonl` files as
    sensitive, and keep `redact_paths` enabled when sharing logs.

    `debug_keys` affects **only** content-encryption keys (the `content_key`/`key` fields).
    Passwords, tokens, cookies, and session tokens are **always** redacted regardless of this
    setting, and KIDs, key counts, and other metadata are always logged either way.

---

## Deprecated & removed keys { #deprecated-removed-keys }

| Key | Status | Behaviour |
|-----|--------|-----------|
| `curl_impersonate` | **Deprecated** → use [`network`](#network-proxy) | Emits a `DeprecationWarning`; still honoured only if `network` is absent. |
| `downloader` | **Deprecated** | Any value other than `"requests"` emits a `DeprecationWarning`; the value is otherwise ignored (the unified requests downloader is always used). |
| `scene_naming` | **Removed** | If present at all, unshackle exits with an error directing you to configure [`output_template`](#output-naming) instead. |

---

## A complete annotated example

The following config sets a handful of common keys. Every key not shown keeps its default.

```yaml title="unshackle.yaml"
directories:
  downloads: ~/Media/unshackle
  temp: /mnt/scratch/unshackle

network:
  browser: Chrome131

headers:
  Accept-Language: en-US,en;q=0.9

cdm:
  default: chromecdm_l3
  EXAMPLE2: android_l1

decryption: shaka

credentials:
  EXAMPLE2: user@example.com:hunter2

output_template:
  movies: "{title}.{year}.{quality?}.{source}-{tag}"
  series: "{title}.{season_episode}.{quality?}.{source}-{tag}"
  folder:
    movies: "{title} ({year})"
    series: "{title} ({year})"

tag: MYGRP

key_vaults:
  - type: SQLite
    name: Local
    path: ~/.unshackle/keys.db

proxy_providers:
  basic:
    us: http://user:pass@1.2.3.4:8080

tmdb_api_key: "your-tmdb-key"
title_cache_time: 3600
redact_paths: true
```

For how these interact at runtime, continue to [Downloading](../guide/downloading.md),
[DRM & CDM](../guide/drm-and-cdm.md), and [Output & naming](../guide/output-and-naming.md).
