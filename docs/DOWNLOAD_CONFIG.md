# Download & Processing Configuration

This document covers configuration options related to downloading and processing media content.

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

---

## curl_impersonate (dict)

- `browser` - The Browser to impersonate as. A list of available Browsers and Versions are listed here:
  <https://github.com/yifeikong/curl_cffi#sessions>

  Default: `"chrome124"`

For example,

```yaml
curl_impersonate:
  browser: "chrome120"
```

---

## downloader (str | dict)

Choose what software to use to download data throughout unshackle where needed.
You may provide a single downloader globally or a mapping of service tags to
downloaders.

Options:

- `requests` (default) - <https://github.com/psf/requests>
- `aria2c` - <https://github.com/aria2/aria2>
- `curl_impersonate` - <https://github.com/yifeikong/curl-impersonate> (via <https://github.com/yifeikong/curl_cffi>)
- `n_m3u8dl_re` - <https://github.com/nilaoda/N_m3u8DL-RE>

Note that aria2c can reach the highest speeds as it utilizes threading and more connections than the other downloaders. However, aria2c can also be one of the more unstable downloaders. It will work one day, then not another day. It also does not support HTTP(S) proxies natively (non-HTTP proxies are bridged via pproxy).

Note that `n_m3u8dl_re` will automatically fall back to `requests` for track types it does not support, specifically: direct URL downloads, Subtitle tracks, and Attachment tracks.

Example mapping:

```yaml
downloader:
  NF: requests
  AMZN: n_m3u8dl_re
  DSNP: n_m3u8dl_re
  default: requests
```

The `default` entry is optional. If omitted, `requests` will be used for services not listed.

---

## n_m3u8dl_re (dict)

Configuration for N_m3u8DL-RE downloader. This downloader supports HLS, DASH, and ISM (Smooth Streaming) manifests.
It will automatically fall back to the `requests` downloader for unsupported track types (direct URLs, subtitles, attachments).

- `thread_count`
  Number of threads to use for downloading. Default: Uses the same value as max_workers from the command
  (which defaults to `min(32,(cpu_count+4))`).
- `ad_keyword`
  Keyword to identify and potentially skip advertisement segments. Default: `None`
- `use_proxy`
  Whether to use proxy when downloading. Default: `true`
- `retry_count`
  Number of times to retry failed downloads. Default: `10`

N_m3u8DL-RE also respects the `decryption` config setting. When content keys are provided, it will use
the configured decryption engine (`shaka` or `mp4decrypt`) and automatically locate the corresponding binary.

For example,

```yaml
n_m3u8dl_re:
  thread_count: 16
  ad_keyword: "advertisement"
  use_proxy: true
  retry_count: 10
```

---

## dl (dict)

Pre-define default options and switches of the `dl` command.
The values will be ignored if explicitly set in the CLI call.

The Key must be the same value Python click would resolve it to as an argument.
E.g., `@click.option("-r", "--range", "range_", type=...` actually resolves as `range_` variable.

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
| `range_` | str or list | `SDR` | Color range(s): `SDR`, `HDR10`, `HDR10+`, `HLG`, `DV`, `HYBRID` |
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
| `workers` | int | auto | Max threads per track download |
| `slow` | bool | `false` | Add 60-120s delay between titles |
| `skip_dl` | bool | `false` | Skip download, only get decryption keys |
| `cdm_only` | bool | `null` | Only use CDM (`true`) or only vaults (`false`) |

You can also set per-service `dl` overrides (see [Service Integration & Authentication Configuration](SERVICE_CONFIG.md)):

```yaml
dl:
  lang: en
  downloads: 4
  workers: 16
  AMZN:
    bitrate: CVBR
  NF:
    worst: true
    quality: 1080
```

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
  ATVP: mp4decrypt
  AMZN: shaka
  default: shaka
```

The `default` entry is optional. If omitted, `shaka` will be used for services not listed.

Simple configuration (single method for all services):

```yaml
decryption: mp4decrypt
```

---
