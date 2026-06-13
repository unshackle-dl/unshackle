# Download & Processing Configuration

This document covers configuration options related to downloading and processing media content.

## downloader

unshackle ships a single unified downloader at `unshackle/core/downloaders/requests.py`. The legacy
`aria2c`, `curl_impersonate`, and `n_m3u8dl_re` backends have been removed; their config blocks no
longer have any effect.

The unified downloader:

- Works with both a standard `requests.Session` and `RnetSession` (rnet/BoringSSL TLS impersonation,
  which replaces the previous `curl_cffi` backend). When a service exposes its own session via
  `self.session`, TLS fingerprinting is preserved on every segment.
- Uses adaptive chunk sizing between **512 KB and 4 MB**, picked from the response `Content-Length`.
- Spawns **up to `min(16, cpu_count + 4)` worker threads** by default for segmented downloads
  (override via `--workers` / `dl.workers`).
- Resumes interrupted downloads via HTTP `Range` requests (a sibling `<file>.!dev` control file
  marks an in-progress download).
- Has a single-URL fast path: if the server supports byte ranges and the file is at least 64 MB,
  the file is split into 16 MB parts and downloaded in parallel into a pre-allocated file.
- Is selected per-track via `track.downloader`, which defaults to this unified `requests` downloader.

There is no `downloader:` config key to set anymore. Setting one to a legacy value will emit a
`DeprecationWarning` and otherwise be ignored.

---

## dl (dict)

Pre-define default options and switches of the `dl` command.
The values will be ignored if explicitly set in the CLI call.

The Key is the option name with dashes as underscores (`--v-lang` -> `v_lang`). `range` and `list`
work as-is; their internal Python names (`range_`, `list_`, suffixed to avoid the builtins) are
also accepted.

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

to set `--bitrate=CVBR` for a specific service,

```yaml
lang: de
EXAMPLE:
  bitrate: CVBR
```

or to change the output subtitle format from the default (original format) to WebVTT,

```yaml
sub_format: vtt
```

### All Available `dl` Keys

Below is a comprehensive list of keys that can be pre-defined in the `dl` section. Each corresponds
to a CLI option on the `dl` command. CLI arguments always take priority over config values.

**Quality and codec:**

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `quality` | int or list | best | Resolution(s) to download (e.g., `1080`, `[1080, 2160]`) |
| `vcodec` | str or list | any | Video codec(s): `H264`, `H265`, `VP9`, `AV1`, `VC1` |
| `acodec` | str or list | any | Audio codec(s): `AAC`, `AC3`, `EC3`, `AC4`, `OPUS`, `FLAC`, `ALAC`, `DTS` |
| `vbitrate` | int | highest | Video bitrate in kbps |
| `abitrate` | int | highest | Audio bitrate in kbps |
| `vbitrate_range` | str | none | Video bitrate window in kbps, format `MIN-MAX` (e.g., `6000-7000`) |
| `abitrate_range` | str | none | Audio bitrate window in kbps, format `MIN-MAX` |
| `real_video_bitrate` | bool | `false` | Probe actual media size to compute true video bitrates, overriding the manifest's declared value (`-rvb`). See [Real bitrate probing](#real-bitrate-probing) |
| `real_audio_bitrate` | bool | `false` | Same as above for audio tracks (`-rab`). Slower than video (more renditions) |
| `range` (or `range_`) | str or list | `SDR` | Color range(s): `SDR`, `HDR10`, `HDR10+`, `HLG`, `DV`, `HYBRID` |
| `channels` | float | any | Audio channels (e.g., `5.1`, `7.1`) |
| `worst` | bool | `false` | Select the lowest bitrate track within the specified quality. Requires `quality` |
| `best_available` | bool | `false` | Continue if requested quality is unavailable |

**Language:**

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `lang` | str or list | `orig` | Language for video and audio (`orig` = original language) |
| `v_lang` | list | `[]` | Language override for video tracks only |
| `a_lang` | list | `[]` | Language override for audio tracks only |
| `s_lang` | list | `["all"]` | Language for subtitles |
| `require_subs` | list | `[]` | Required subtitle languages (skip title if missing) |
| `forced_subs` | bool | `false` | Include forced subtitle tracks |
| `exact_lang` | bool | `false` | Exact language matching (no regional variants) |
| `latest_episode` | bool | `false` | Download only the single most recent episode of a series |

**Track selection:**

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `video_only` | bool | `false` | Only download video tracks |
| `audio_only` | bool | `false` | Only download audio tracks |
| `subs_only` | bool | `false` | Only download subtitle tracks |
| `chapters_only` | bool | `false` | Only download chapters |
| `no_video` | bool | `false` | Skip video tracks |
| `no_audio` | bool | `false` | Skip audio tracks |
| `no_subs` | bool | `false` | Skip subtitle tracks |
| `no_chapters` | bool | `false` | Skip chapters |
| `no_atmos` | bool | `false` | Exclude Dolby Atmos audio tracks |
| `audio_description` | bool | `false` | Include audio description tracks |

**Output and tagging:**

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `tag` | str | config default | Override group tag |
| `repack` | bool | `false` | Add REPACK tag to output filename |
| `sub_format` | str | original | Output subtitle format: `srt`, `vtt`, `ass`, `ssa`, `ttml` |
| `no_folder` | bool | `false` | Disable folder creation for TV shows |
| `no_source` | bool | `false` | Remove source tag from filename |
| `no_mux` | bool | `false` | Do not mux tracks into a container file |
| `split_audio` | bool | `false` | Create separate output files per audio codec |
| `export` | bool | `false` | Write a JSON sidecar with manifest URLs, subtitles, per-track KID:KEY, codec/track info |

**Metadata enrichment:**

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `tmdb_id` | int | `null` | Use specific TMDB ID for tagging |
| `imdb_id` | str | `null` | Use specific IMDB ID (e.g., `tt1375666`) |
| `animeapi_id` | str | `null` | Anime database ID via AnimeAPI (e.g., `mal:12345`, `anilist:98765`) |
| `enrich` | bool | `false` | Override show title and year from external source. Requires `tmdb_id`, `imdb_id`, or `animeapi_id` |

**Download behavior:**

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `downloads` | int | `1` | Concurrent track downloads |
| `workers` | int | `min(16, cpu_count + 4)` | Max threads per track download (segments / ranged parts) |
| `slow` | bool or `MIN-MAX` | `false` | Randomized delay between titles. `true` uses 60-120s; pass `MIN-MAX` (e.g., `20-40`) for a custom range |
| `no_proxy_download` | bool | `false` | Bypass proxy for segment downloads only. Manifest, license, and auth still use proxy |
| `skip_dl` | bool | `false` | Skip download, only get decryption keys |
| `cdm_only` | bool | `null` | Only use CDM (`true`) or only vaults (`false`) |

### Real bitrate probing

Some services declare inaccurate `bandwidth`/`BANDWIDTH` in their manifests — often
a peak or nominal figure that is far from the real average. Because `track.bitrate`
drives the track listing, sorting, and `--vbitrate` / `--vbitrate-range` selection,
a wrong value picks the wrong track.

`-rvb` / `--real-video-bitrate` (and `-rab` / `--real-audio-bitrate` for audio)
probe the actual media size and overwrite `track.bitrate` with the measured value
(`bytes * 8 / duration`) before listing and selection. So `-rvb --list` shows the
true numbers, and `-rvb --vbitrate-range 6000-7000` selects against them. Without
the flag, behaviour is unchanged (the manifest value is used).

How it works:

- **Single-file tracks** (one whole file per rendition — e.g. DASH `SegmentBase`
  or services that collapse to a `BaseURL`) are measured **exactly**: the whole
  file size over the track duration.
- **Multi-segment tracks** (most HLS) are a **sampled estimate** — a spread of
  segments is probed and extrapolated, typically within a few percent. Segment
  bytes include container overhead, so MPEG-TS HLS reads a few percent above the
  demuxed stream (this is the real *delivered* size).
- Only the top renditions per quality tier are probed (video grouped by
  codec + range, audio by codec + channels + language), in parallel, then extended
  downward only as far as needed to keep ranking correct. This keeps the pass fast
  even when a service exposes dozens of renditions.
- Tracks whose duration cannot be determined fall back to `ffprobe`; probe failures
  are non-fatal and leave the manifest bitrate in place.

Per-track before→after values are logged at debug level (run with `-d`); the
corrected values always appear in the Available Tracks panel.

You can also set per-service `dl` overrides under the `services` section (see
[Service Integration & Authentication Configuration](SERVICE_CONFIG.md)). Per-service values beat the
global `dl:` section; explicit CLI arguments beat both:

```yaml
dl:
  lang: en
  downloads: 4
  workers: 16

services:
  EXAMPLE:
    dl:
      v_lang: [en]
      quality: 1080
```

---

## audio (dict)

Configuration for audio track selection.

- `codec_priority`
  Optional list of audio codec names defining the preferred order when multiple audio
  tracks share the same bitrate and language. Listed codecs are ranked in the order given.
  Codecs not in the list retain their bitrate-based ordering and are placed after all
  listed codecs (i.e. soft priority — nothing is dropped).

  Atmos tracks still take precedence over codec priority, and audio description tracks
  are still moved to the end.

  Valid codec names: `AAC`, `AC3`, `EC3`, `AC4`, `OPUS`, `OGG`, `DTS`, `ALAC`, `FLAC`.

For example,

```yaml
audio:
  codec_priority: [FLAC, ALAC, AC4, EC3, DTS, AC3, OPUS, AAC, OGG]
```

Or to only prefer a subset (e.g. surround codecs first, everything else falls back to
bitrate order):

```yaml
audio:
  codec_priority: [EC3, DTS, AC3, AAC]
```

When unset, audio tracks are sorted by bitrate alone (with Atmos/descriptive rules still
applied).

---

## subtitle (dict)

Configuration for subtitle processing and conversion.

- `conversion_method`
  Method to use for converting subtitles between formats. Default: `"auto"`
  - `"auto"` — Smart routing: uses subby for WebVTT/SAMI, pycaption for others.
  - `"subby"` — Always use subby with advanced processing.
  - `"pycaption"` — Use only pycaption library (no SubtitleEdit, no subby).
  - `"subtitleedit"` — Prefer SubtitleEdit when available, fall back to pycaption.
  - `"pysubs2"` — Use pysubs2 library (supports SRT/SSA/ASS/WebVTT/TTML/SAMI/MicroDVD/MPL2/TMP).
- `sdh_method`
  Method to use for SDH (hearing impaired) stripping. Default: `"auto"`
  - `"auto"` — Try subby (SRT only), then SubtitleEdit (if available), then subtitle-filter.
  - `"subby"` — Use subby library (SRT only).
  - `"subtitleedit"` — Use SubtitleEdit tool (Windows only, falls back to subtitle-filter).
  - `"filter-subs"` — Use subtitle-filter library directly.
- `strip_sdh`
  Automatically create stripped (non-SDH) versions of SDH subtitles. Default: `true`
- `convert_before_strip`
  Auto-convert VTT/other formats to SRT before using subtitle-filter for SDH stripping.
  Ensures compatibility when subtitle-filter is used as fallback. Default: `true`
- `preserve_formatting`
  Preserve original subtitle formatting (tags, positioning, styling).
  When `true`, skips pycaption processing for WebVTT files to keep tags like `<i>`, `<b>`,
  positioning intact. Combined with no `sub_format` setting, ensures subtitles remain in
  their original format. Default: `true`
- `output_mode`
  Output mode for subtitles. Default: `"mux"`
  - `"mux"` — Embed subtitles in MKV container only.
  - `"sidecar"` — Save subtitles as separate files only.
  - `"both"` — Embed in MKV and save as sidecar files.
- `sidecar_format`
  Format for sidecar subtitle files when `output_mode` is `"sidecar"` or `"both"`. Default: `"srt"`
  Options: `srt`, `vtt`, `ass`, `original` (keep current format).

For example,

```yaml
subtitle:
  conversion_method: auto
  sdh_method: auto
  strip_sdh: true
  convert_before_strip: true
  preserve_formatting: true
  output_mode: mux
  sidecar_format: srt
```

---

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
  EXAMPLE: mp4decrypt
  EXAMPLE2: shaka
  default: shaka
```

The `default` entry is optional. If omitted, `shaka` will be used for services not listed.

Simple configuration (single method for all services):

```yaml
decryption: mp4decrypt
```

---
