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

## surge (dict)

Configuration for the Surge downloader. unshackle starts an isolated `surge server`
instance in the background and sends downloads to its local HTTP API.

- `max_connections_per_host`
  Maximum concurrent connections allowed to one host. Default: `max_workers`
- `max_concurrent_downloads`
  Maximum downloads Surge should run at once. Default: `max_workers`
- `user_agent`
  Optional fallback User-Agent for the managed Surge server. Default: `""`
  Note: Per-request headers from unshackle still take precedence.
- `proxy_url`
  Optional fallback proxy URI for the managed Surge server. Default: `""`
  Note: CLI/service/session proxy settings override this value when present.
- `sequential_download`
  Download pieces in order instead of aggressively parallelizing. Default: `false`
- `min_chunk_size`
  Minimum chunk size in bytes. Default: `2097152` (2 MiB)
- `worker_buffer_size`
  Per-worker buffer size in bytes. Default: `524288` (512 KiB)

For example,

```yaml
surge:
  max_connections_per_host: 16
  max_concurrent_downloads: 8
  sequential_download: false
  min_chunk_size: 2097152
  worker_buffer_size: 524288
```

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
- `surge` - <https://github.com/surge-downloader/Surge>
- `curl_impersonate` - <https://github.com/yifeikong/curl-impersonate> (via <https://github.com/yifeikong/curl_cffi>)
- `n_m3u8dl_re` - <https://github.com/nilaoda/N_m3u8DL-RE>

Note that aria2c can reach the highest speeds as it utilizes threading and more connections than the other downloaders. However, aria2c can also be one of the more unstable downloaders. It will work one day, then not another day. It also does not support HTTP(S) proxies natively (non-HTTP proxies are bridged via pproxy).

Note that `surge` is managed in headless server mode by unshackle. It works well for direct URL downloads and segment-based manifest downloads, but it does not replace `n_m3u8dl_re`'s whole-manifest handling.

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
