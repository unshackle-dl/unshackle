# CLI Reference

A complete reference for every `unshackle` command, subcommand, and flag. Commands are grouped by purpose:

- **[`dl`](#dl)**: the download pipeline (the command you use most).
- **[`search`](#search)**: find titles on a service.
- **[`import`](#import)**: rebuild a download from an export file.
- **[`cfg`](#cfg)**, **[`env`](#env)**: manage configuration and the environment.
- **[`kv`](#kv)**: Key Vault operations.
- **[`wvd`](#wvd)**, **[`prd`](#prd)**: Widevine / PlayReady device management.
- **[`serve`](#serve)**: run the local CDM + REST API server.
- **[`util`](#util)**: helper media utilities.

!!! tip "Getting help on any command"
    Every command accepts `-h`, `--help`, or `-?`. For example `unshackle dl --help` or `unshackle wvd new --help`. Service-specific arguments for `dl` and `search` (such as a title ID or search query) are defined by each service, so check `unshackle dl SERVICE --help` for those.

---

## Root command

```
unshackle [OPTIONS] COMMAND [ARGS]...
```

> unshackle: Modular Movie, TV, and Music Archival Software.

Every invocation prints a banner and, when `update_checks` is enabled in your config, checks for a newer release.

| Option | Description |
|---|---|
| `-v`, `--version` | Print version information and exit. |
| `-d`, `--debug` | Enable `DEBUG`-level logs and JSON debug logging. Silences noisy HTTP libraries unless `debug_requests` is set in config. |
| `-h`, `--help`, `-?` | Show help and exit. |

Commands are auto-discovered, so third-party command modules dropped into the commands directory appear automatically.

---

## `dl`

The download command. It is itself a group whose **subcommands are the installed streaming services**, so an invocation always has three layers:

```
unshackle dl [OPTIONS] SERVICE [SERVICE ARGS...]
```

- `OPTIONS`: every flag below, parsed at the `dl` level.
- `SERVICE`: a service tag (e.g. `EXAMPLE1`, `EXAMPLE2`). Tags are case-insensitive and honour each service's aliases (`example+`, `EXAMPLE2`, etc. all resolve to the same tag).
- `SERVICE ARGS`: the title, URL, or ID, plus any service-specific options. These belong to the service, not to `dl`.

!!! example "Typical downloads"
    ```shell
    # Best available 1080p SDR of a title
    unshackle dl -q 1080 EXAMPLE 81234567

    # 4K HDR10, English audio + subs, from a URL
    unshackle dl -q 2160 -r HDR10 -l en EXAMPLE "https://www.example.com/..."

    # A season range, HEVC video, EC3 audio
    unshackle dl -w S01-S03 -v H.265 -a EC3 SERVICE TITLE-ID
    ```

!!! note "Config-driven defaults"
    Any `dl` flag can be given a default under the `dl:` section of `unshackle.yaml`, and per-service under `services.<TAG>.dl`. Explicit command-line values (and environment values) always win over both; service defaults only fill in options you did not set. See the [configuration file](../getting-started/configuration-file.md) guide.

### Quality, codec, bitrate & range

| Flag | Default | Description |
|---|---|---|
| `-p`, `--profile` | - | Profile for credentials and cookies. |
| `-q`, `--quality` | best | Resolution(s), comma-separated, e.g. `1080,720`. `-q 1080` also matches anamorphic tracks by 16:9 canvas. |
| `-v`, `--vcodec` | any | Video codec(s). Accepts names or values: `AVC`/`H.264`, `HEVC`/`H.265`, `VC1`, `VP8`, `VP9`, `AV1`. |
| `-a`, `--acodec` | any | Audio codec(s), comma-separated. Accepts `AAC`, `AC3`/`DD`, `EC3`/`DD+`/`eac3`/`ddp`, `AC4`, `OPUS`, `OGG`/`vorbis`, `DTS`, `ALAC`, `FLAC`. |
| `-vb`, `--vbitrate` | highest | Exact video bitrate in kbps. |
| `-ab`, `--abitrate` | highest | Exact audio bitrate in kbps. |
| `-vb-range`, `--vbitrate-range` | - | Video bitrate range in kbps, e.g. `6000-7000`; picks highest within. Mutually exclusive with `--vbitrate`. |
| `-ab-range`, `--abitrate-range` | - | Audio bitrate range in kbps, e.g. `128-256`. Mutually exclusive with `--abitrate`. |
| `-r`, `--range` | `SDR` | Colour range(s): `SDR`, `HLG`, `HDR10`, `HDR10P` (HDR10+), `DV`, `HYBRID`. |
| `-c`, `--channels` | - | Audio channels; matches sub-layouts (5.1 ≈ 6.0). |
| `-naa`, `--noatmos` | off | Exclude Dolby Atmos audio tracks. |
| `--worst` | off | Pick the lowest bitrate within the requested quality. **Requires `-q`.** |
| `--best-available` | off | Continue with the best available if a requested resolution/language is absent, instead of failing. |
| `-rvb`, `--real-video-bitrate` | off | Probe real media size for true video bitrates, overriding the manifest. |
| `-rab`, `--real-audio-bitrate` | off | Same for audio (slower). |

!!! note "`HYBRID` range"
    `-r HYBRID` fetches both HDR10/HDR10+ and Dolby Vision and merges them with `dovi_tool`. It requires the `dovi_tool` binary on your `PATH`.

### Language & subtitles

| Flag | Default | Description |
|---|---|---|
| `-l`, `--lang` | `orig` | Language(s) for **both** video and audio. `orig` = the title's original language; e.g. `orig,en`. A `-` prefix excludes, e.g. `all,-es`. |
| `-vl`, `--v-lang` | - | Video-only language (overrides `-l` for video). A `-` prefix excludes. |
| `-al`, `--a-lang` | - | Audio-only language (overrides `-l` for audio). A `-` prefix excludes. |
| `-sl`, `--s-lang` | `all` | Subtitle language(s). A `-` prefix excludes, e.g. `all,-es`. |
| `--require-subs` | - | Required subtitle langs; keeps **all** subs only if these exist. **Cannot combine with `--s-lang`.** |
| `-fs`, `--forced-subs` | off | Include forced subtitle tracks. |
| `-fsl`, `--forced-s-lang` | none | Language(s) wanted for forced subtitles; implies `-fs`. A `-` prefix excludes. |
| `--exact-lang` | off | Exact matching only: `-l es-419` matches `es-419`, not `es-ES`. Applies to selection and to sort order. |
| `--sub-format` | - | Output subtitle format (`SRT`/`srt`, `VTT`/`webvtt`, `ASS`/`ssa`, `TTML`, `SMI`, ...), or `original` to keep the source format. |

!!! tip "These flags select languages, they do not order them"
    Naming languages with `-l` or `-sl` removes the others. To keep every language but put
    your preferred ones first, use `audio.language_priority` and
    [`subtitle.language_priority`](../reference/configuration/download.md#subtitle) in your
    configuration file.

The special language tokens `orig`, `all`, and `best` are honoured everywhere a language is expected.

### Title & episode selection

| Flag | Description |
|---|---|
| `-w`, `--wanted` | Wanted episodes, e.g. `S01-S05,S07`, `S01E01-S02E03`. Supports exclusions with a leading `-` (e.g. `-S03`). For a [split episode](downloading.md#split-episodes), `.N` picks one part (`S01E01.2`), a range must stay inside the episode (`S01E01.1-S01E01.3`), and `S01E01` on its own takes every part. |
| `--select-titles` | Interactively select what to download: episodes of a series, or films when a title has more than one. **Cannot combine with `-w`.** |
| `--latest-episode` | Download only the single most recent episode. |
| `--list-titles` | List titles only; do not download. |

### Track-type inclusion / exclusion

Keep only certain track types, or skip certain track types. Attachments are always kept.

| Include-only | Skip |
|---|---|
| `-V`, `--video-only` | `-nv`, `--no-video` |
| `-A`, `--audio-only` | `-na`, `--no-audio` |
| `-S`, `--subs-only` | `-ns`, `--no-subs` |
| `-C`, `--chapters-only` | `-nc`, `--no-chapters` |

| Flag | Description |
|---|---|
| `-ad`, `--audio-description` | Include descriptive (audio-description) tracks. |
| `--skip-subtitle-errors` | Skip a failed subtitle instead of aborting the title. Video/audio failures remain fatal. |

### Output, muxing & files

| Flag | Description |
|---|---|
| `--split-audio` | Write a separate output file per audio codec instead of merging. Defaults to config `muxing.merge_audio`. |
| `--merge-video` | Mux all selected video tracks into one file. Defaults to config `muxing.merge_video`. |
| `-o`, `--output` | Override the output directory for this run. |
| `--no-folder` | Disable folder creation for TV shows. |
| `--no-source` | Remove the source tag from the filename/path. |
| `--no-mux` | Do not mux; keep individual track files. |
| `--tag` | Group tag override. |
| `--repack` | Add a `REPACK` tag to the filename. |

### Metadata & tagging

| Flag | Description |
|---|---|
| `--tmdb` | TMDB ID (integer). |
| `--imdb` | IMDb ID, e.g. `tt1375666`. |
| `--tvdb` | TVDB ID (integer). Skips the series lookup that `--tvdb-order` would otherwise do. `--enrich` reads it too. |
| `--animeapi` | AnimeAPI ID, e.g. `mal:12345` or `anilist:98765` (defaults to MAL). Back-fills TMDB/IMDb/TVDB. |
| `--enrich` | Override show title/year from the external source, and fill in the original language. **Requires** one of `--tmdb`, `--imdb`, `--tvdb`, or `--animeapi`. |
| `--tvdb-order` | Renumber episodes to a TVDB season order: `official` (aired), `dvd`, `absolute`, `alternate`, or `regional`. Needs `tvdb_api_key`. |

#### Episode ordering

A service does not always number a series the way TVDB's aired order does. Some services list
Futurama in TVDB's `alternate` (Streaming) order, for example. `--tvdb-order` works out which
order the service used, then renumbers the episodes into the order you asked for.

!!! note "Orders that do not cover the whole series"
    An order can omit episodes the service carries; TVDB's `dvd` order for Futurama leaves out
    the four movies. Those episodes keep their original numbering. If that would give two
    episodes the same season/episode slot, and so the same filename, unshackle logs an error
    and keeps the service's numbering unchanged. Pick an order that covers the whole series.

### DRM, keys & decryption

| Flag | Description |
|---|---|
| `--cdm-only` / `--vaults-only` | Use only the CDM, or only Key Vaults, for key acquisition. |
| `--skip-dl` | Skip the download but still retrieve keys. |
| `--export` | Export track info and keys to a JSON file in the exports directory. |

### Network & proxy

| Flag | Description |
|---|---|
| `--proxy` | Proxy URI, a 2-letter country code resolved from configured providers, or `provider:region` (e.g. `nordvpn:ca`, `gluetun:us`, `protonvpn:de:berlin`). |
| `--no-proxy` | Force-disable all proxy use. |
| `--no-proxy-download` | Bypass the proxy for **all downloads** (manifest, licence, and auth stay proxied). |
| `--remote` | Use a remote unshackle server. |
| `--server` | Name a remote server from the `remote_services` config. |

### Concurrency, caching & listing

| Flag | Default | Description |
|---|---|---|
| `--workers` | downloader default | Per-track download threads. |
| `--downloads` | `1` | Number of tracks downloaded concurrently. |
| `--speed-limit` | unlimited | Cap total download speed across all threads and tracks, e.g. `500k`, `5M`, `1.5G` or plain bytes/sec. Values are bytes, not bits (`5M` = 5.0 MB/s). `off` disables a configured limit. |
| `--no-cache` | off | Bypass the title cache. |
| `--reset-cache` | off | Clear the title cache. |
| `--list` | off | List available/would-be-downloaded tracks; do not download. |
| `--slow` | - | Inter-title delay. Bare `--slow` = 60-120s; `--slow 20-40` = custom range (minimum 20s). |

!!! warning "Some flags cannot be combined"
    `--require-subs` and `--s-lang`; `--select-titles` and `--wanted`; `--worst` requires `--quality`; `--vbitrate` and `--vbitrate-range` (and the audio equivalents) are mutually exclusive.

---

## `search`

Search a service for titles. Like `dl`, `search` is a group whose subcommands are the installed services, and it reuses `dl`'s authentication, cookie, and proxy machinery.

```
unshackle search [OPTIONS] SERVICE [QUERY...]
```

The query syntax is defined per service. Results are printed as a tree of titles with their service IDs. Feed an ID straight into `dl`.

| Option | Description |
|---|---|
| `-p`, `--profile` | Profile for credentials and cookies. |
| `--proxy` | Proxy URI, 2-letter country code, or `provider:region`. |
| `--no-proxy` | Force-disable all proxy use. |

!!! example
    ```shell
    unshackle search EXAMPLE "My Show"
    unshackle search -p myprofile EXAMPLE "Another Show"
    unshackle search --proxy nordvpn:ca SERVICE "query"
    ```

---

## `import`

Reconstruct a download (download → decrypt → mux) from an `--export` JSON file without re-contacting the service. It re-fetches the manifest, injects the stored keys, and then runs the normal `dl` pipeline. The service tag is read from the export file, not passed by you.

```
unshackle import EXPORT_FILE [DL_ARGS...]
```

Any `dl` options placed after the file are forwarded verbatim, so you can override quality, range, proxy, and so on.

!!! example
    ```shell
    unshackle import export.json
    unshackle import export.json -r HDR10 --proxy US
    ```

The export file must be a valid v2 export (created by a current build of unshackle via `dl --export`) and must contain a `service` tag.

---

## `cfg`

Read, set, delete, or list configuration values in `unshackle.yaml` without hand-editing YAML.

```
unshackle cfg [KEY] [VALUE] [OPTIONS]
```

| Argument / Option | Description |
|---|---|
| `KEY` | Dotted path into the config, e.g. `tag`, `serve.api_secret`, `directories.downloads`. |
| `VALUE` | Value to set. Parsed as a Python literal when possible, so `true`, `123`, `['a','b']`, and `{'k':1}` become real types; bare words stay strings. |
| `--unset` | Remove the configuration value. |
| `--list` | List all set configuration values. |

!!! warning "Comments are stripped"
    Writing a value through `cfg` rewrites the YAML file and **removes all comments** from it. If your config relies on comments, edit it by hand instead. Setting a value and using `--unset` together is an error.

!!! example
    ```shell
    unshackle cfg --list                        # print entire config
    unshackle cfg serve.api_secret              # read a nested key
    unshackle cfg tag MYGROUP                    # set a string
    unshackle cfg update_checks false           # literal → boolean False
    unshackle cfg directories.downloads /mnt/dl # set a nested path
    unshackle cfg tag --unset                    # remove a key
    ```

---

## `env`

Inspect and manage the project environment.

```
unshackle env COMMAND [ARGS]...
```

### `env check`

```
unshackle env check
```

Prints a dependency table (Category / Tool / Status / Required / Purpose) showing which external tools resolve on your `PATH`, and a summary of how many required tools are installed.

| Category | Tools |
|---|---|
| Core | FFmpeg*, FFprobe*, MKVToolNix*, mkvpropedit* |
| DRM | shaka-packager*, mp4decrypt, ML-Worker |
| HDR | dovi_tool, HDR10Plus_tool |
| Subtitle | SubtitleEdit, CCExtractor |
| Player | FFplay, MPV |
| Network | HolaProxy, Caddy, Docker, git |

<small>* required; all others are optional.</small>

### `env info`

```
unshackle env info
```

Shows where the config was loaded from (or lists the candidate config locations if none was found) and prints a table of every configured directory (downloads, temp, cache, cookies, logs, exports, WVDs, PRDs, services, and more).

### `env clear`

Clear an environment directory. The directory is emptied and recreated, and the number of files and bytes freed is reported.

| Command | Description |
|---|---|
| `unshackle env clear cache [SERVICE]` | Clear the cache directory, or just one service's cache subdirectory. |
| `unshackle env clear temp` | Clear the temp directory. |

!!! example
    ```shell
    unshackle env check
    unshackle env info
    unshackle env clear cache
    unshackle env clear cache EXAMPLE   # one service only
    unshackle env clear temp
    ```

---

## `kv`

Manage Key Vaults. Vaults are configured under `key_vaults` in `unshackle.yaml`, each with a `name`, a `type` (`sqlite`, `mysql`, `http`, `api`), and type-specific options. Service tags are normalised automatically.

```
unshackle kv COMMAND [ARGS]...
```

### `kv copy TO_VAULT FROM_VAULT...`

Copy keys from one or more source vaults into a destination vault. Rows with matching KIDs are skipped unless the existing row has no key; existing data is never altered or deleted.

| Option | Description |
|---|---|
| `-s`, `--service` | Only copy data for a specific service. |
| `-l`, `--local-only` | Only copy data for services installed locally. Mutually exclusive with `--service`. |

### `kv sync VAULT...`

Ensure two or more vaults hold the same set of keys, essentially a chained bidirectional copy. Requires more than one vault. Accepts the same `--service` / `--local-only` options as `copy`.

### `kv add FILE SERVICE VAULT...`

Add content keys to one or more vaults for a service. `FILE` contains one `KID:KEY` pair per line (32 hex : 32 hex, UTF-8). Lines that don't match are skipped.

### `kv search KID`

Search configured vaults for a KID (32 hex characters, no dashes) and report any matching key.

| Option | Description |
|---|---|
| `-s`, `--service` | Limit the search to a specific service tag. |
| `-v`, `--vault` | Limit the search to a specific configured vault by name. |

!!! note
    Remote vaults cannot be enumerated without a service, so pass `--service` when searching them.

### `kv prepare VAULT...`

Create service tables on vaults that use tables, for every installed service, where they don't already exist.

!!! example
    ```shell
    unshackle kv copy main backup1 backup2
    unshackle kv copy main backup -s EXAMPLE
    unshackle kv sync main mysql_vault
    unshackle kv add keys.txt EXAMPLE main backup
    unshackle kv search 0123456789abcdef0123456789abcdef -s EXAMPLE -v main
    unshackle kv prepare main mysql_vault
    ```

---

## `wvd`

Manage Widevine Device (`.wvd`) files. Devices live in the configured WVDs directory.

```
unshackle wvd COMMAND [ARGS]...
```

| Command | Description |
|---|---|
| `wvd add PATHS...` | Validate and move one or more `.wvd` files into the WVDs directory. |
| `wvd delete NAMES...` | Delete `.wvd` files by name (without extension). Prompts for confirmation. |
| `wvd parse PATH` | Parse a `.wvd` and print its System ID, security level, type, flags, and client info. Relative paths resolve against the WVDs directory. |
| `wvd dump WVD_PATHS... OUT_DIR` | Extract a device's contents (metadata, private key, client ID, VMP) into `OUT_DIR/<name>/`. With no paths, dumps every WVD in the WVDs directory. |
| `wvd new NAME PRIVATE_KEY CLIENT_ID [FILE_HASHES]` | Create a new `.wvd` from a PEM private key and a `ClientIdentification` blob, optionally with a VMP (`FileHashes`) blob. |

`wvd new` options:

| Option | Default | Description |
|---|---|---|
| `-t`, `--type` | `Android` | Device type. |
| `-l`, `--level` | `1` | Security level (1-3). |
| `-o`, `--output` | WVDs dir | Output directory. |

!!! example
    ```shell
    unshackle wvd add device.wvd
    unshackle wvd parse nexus_6p_1234_l1
    unshackle wvd dump device.wvd ./out_dir
    unshackle wvd new "Nexus 6P" private_key.pem client_id.bin vmp.bin -t ANDROID -l 3
    ```

---

## `prd`

Manage PlayReady Device (`.prd`) files. Devices live in the configured PRDs directory. Built on `pyplayready`.

```
unshackle prd COMMAND [ARGS]...
```

### `prd new PATHS...`

Create a new `.prd`. Provide either a single folder containing `zgpriv.dat` (group key) and `bgroupcert.dat` (group certificate), or two file paths (group key, then group certificate).

| Option | Description |
|---|---|
| `-e`, `--encryption_key` | Optional device ECC private encryption key (generated if omitted). |
| `-s`, `--signing_key` | Optional device ECC private signing key (generated if omitted). |
| `-o`, `--output` | Output directory or `.prd` file path. |

`prd new` will not overwrite an existing file.

### `prd reprovision PRD_PATH`

Reprovision an existing device by replacing its leaf certificate and keys. The device must support reprovisioning (version 3 or higher). Accepts the same `-e` / `-s` / `-o` options; defaults to overwriting the device in place.

### `prd test DEVICE`

Test a device against the Microsoft PlayReady demo server and print the returned keys.

| Option | Default | Description |
|---|---|---|
| `-c`, `--ckt` | `aesctr` | Content key encryption type: `aesctr` or `aescbc`. |
| `-sl`, `--security-level` | `2000` | Minimum security level: `150`, `2000`, or `3000`. |

!!! example
    ```shell
    unshackle prd new ./device_folder/
    unshackle prd new zgpriv.dat bgroupcert.dat -o /out/dir
    unshackle prd reprovision mydevice.prd
    unshackle prd test mydevice.prd -sl 3000 -c aescbc
    ```

---

## `serve`

Serve your local Widevine/PlayReady devices and the REST API for remote access. Built on `aiohttp`.

```
unshackle serve [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `-h`, `--host` | `127.0.0.1` | Host to serve from. |
| `-p`, `--port` | `8786` | Port to serve from. |
| `--caddy` | off | Also serve through Caddy (requires the `caddy` binary and a `Caddyfile`). |
| `--api-only` | off | Serve only the REST API, not the CDM endpoints. Incompatible with `--no-widevine`/`--no-playready`. |
| `--no-widevine` | off | Disable the Widevine CDM endpoints. |
| `--no-playready` | off | Disable the PlayReady CDM endpoints. |
| `--no-key` | off | Disable API-key authentication (allows all requests). |
| `--debug-api` | off | Include tracebacks/stderr in API error responses. |
| `--debug` | off | Enable debug logging for API operations. |
| `--remote-only` | off | Expose only the remote service session endpoints (health, services, search, session). Implies `--api-only`. |

WVD files in the WVDs directory and PRD files in the PRDs directory are auto-loaded. The REST API lives under `http://<host>:<port>/api/`, with Swagger UI at `/api/docs/` and a health check at `/api/health` (exempt from auth).

!!! warning "Configure `api_secret` first"
    Unless you pass `--no-key`, `serve.api_secret` must be set in your config. Requests authenticate with the `X-Secret-Key` header. Running with `--no-key` disables authentication entirely and should only be used on a trusted, private network.

    ```yaml title="unshackle.yaml"
    serve:
      api_secret: "your-api-secret"
      users:
        your-secret-key:
          devices: ["device_name"]           # Widevine devices
          playready_devices: ["device_name"] # PlayReady devices
          username: user
    ```

!!! example
    ```shell
    unshackle serve
    unshackle serve -h 0.0.0.0 -p 8080
    unshackle serve --api-only
    unshackle serve --remote-only
    unshackle serve --caddy
    ```

---

## `util`

Various helper media utilities. When a command is given a directory, it processes every `.mkv`/`.mp4` inside it in natural (S01E01 before S01E10) order.

```
unshackle util COMMAND [ARGS]...
```

### `util refresh-services`

```
unshackle util refresh-services
```

Force a refresh (git pull / hard reset) of all service repos configured under `directories.services`.

### `util crop PATH ASPECT`

Losslessly crop H.264/H.265 video at the bitstream level. `ASPECT` is a `W:H` target such as `2.39:1`.

| Option | Default | Description |
|---|---|---|
| `--letter` / `--pillar` | `--letter` | Crop top/bottom (`--letter`) or the sides (`--pillar`). |
| `-o`, `--offset` | `0` | Fine-tune the computed crop area if not perfectly centred. |
| `-p`, `--preview` | off | Preview the crop in MPV (or FFplay) instead of writing a file. |

### `util range PATH`

Losslessly set the video range flag to full or limited at the bitstream level.

| Option | Default | Description |
|---|---|---|
| `--full` / `--limited` | - | Full (0-255) or limited (16-235) range. |
| `-p`, `--preview` | off | Preview instead of writing a file. |

### `util test PATH`

Decode an entire video with FFmpeg and report any corruptions or errors. All streams are tested by default; subtitles cannot be tested.

| Option | Default | Description |
|---|---|---|
| `-m`, `--map` | `0` | Test specific streams via FFmpeg's `-map`, e.g. `0:v:0` or `0:a`. |

!!! example
    ```shell
    unshackle util refresh-services
    unshackle util crop video.mkv 2.39:1
    unshackle util crop ./season/ 16:9 --pillar -o 10
    unshackle util range video.mkv --full
    unshackle util test video.mkv -m 0:v:0
    ```

---

## See also

- [Installation](../getting-started/installation.md): install unshackle and its dependencies.
- [Quickstart](../getting-started/quickstart.md): your first download.
- [Configuration file](../getting-started/configuration-file.md): set defaults for any `dl` flag and configure vaults, proxies, and directories.
