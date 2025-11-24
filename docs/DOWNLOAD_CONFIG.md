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

Note that aria2c can reach the highest speeds as it utilizes threading and more connections than the other downloaders. However, aria2c can also be one of the more unstable downloaders. It will work one day, then not another day. It also does not support HTTP(S) proxies while the other downloaders do.

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

Configuration for N_m3u8DL-RE downloader. This downloader is particularly useful for HLS streams.

- `thread_count`
  Number of threads to use for downloading. Default: Uses the same value as max_workers from the command.
- `ad_keyword`
  Keyword to identify and potentially skip advertisement segments. Default: `None`
- `use_proxy`
  Whether to use proxy when downloading. Default: `true`

For example,

```yaml
n_m3u8dl_re:
  thread_count: 16
  ad_keyword: "advertisement"
  use_proxy: true
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
