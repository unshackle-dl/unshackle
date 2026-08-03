# Downloading

`unshackle dl` is the main download command: it fetches a title's manifest from a
streaming service, selects exactly the tracks you asked for, acquires and applies
decryption keys, and muxes everything into a finished Matroska file. The flags below
cover quality and codec selection, language and subtitle handling, episode ranges, and
the download flow itself.

!!! note "Where the full flag list lives"
    This page covers the flags you will use most and explains how they interact. It is
    not an exhaustive enumeration. For the complete, always-current list of every option
    and its exact wording, run `unshackle dl --help`. Every flag shown here can also be
    given a default in your configuration file. See
    [Configuration file](../getting-started/configuration-file.md).

## Command shape

A download is always three layers: the root CLI, the `dl` command (which is where all
the flags below are parsed), and a **service tag** subcommand that carries the title
argument.

```shell
unshackle dl [OPTIONS] SERVICE [SERVICE ARGUMENTS]
```

- `unshackle`: the root command.
- `dl`: the download command. **All of the options on this page are parsed here**, so
  they go *before* the service tag.
- `SERVICE`: a service tag such as `EXAMPLE1`, `EXAMPLE2`, `EXAMPLE3`. Service tags are
  case-insensitive and honour each service's aliases (for example `example+` resolves to
  its real tag).
- `SERVICE ARGUMENTS`: usually a title ID or URL. This positional argument belongs to
  the **service**, not to `dl`, so it comes *after* the tag.

```shell title="A minimal download"
unshackle dl EXAMPLE 81234567
```

```shell title="Flags belong to dl, the title belongs to the service"
unshackle dl -q 1080 -v H.265 -r HDR10 --lang en EXAMPLE 'https://www.example.com/...'
```

!!! tip "See what a service accepts"
    Because the positional argument is defined by each service, run
    `unshackle dl SERVICE --help` (e.g. `unshackle dl EXAMPLE --help`) to see that service's
    own argument and any service-specific options.

## The download flow

When you run a download, `dl` moves through the pipeline below. Understanding the order
helps explain why, for example, `--list` stops early and why keys are fetched even with
`--skip-dl`.

1. **Setup**: load the DRM CDM, key vaults, proxy providers, cookies, and credentials.
2. **Authenticate**: sign in to the service with your profile's cookies/credentials.
3. **Fetch titles**: retrieve the movie, episode list, or album (cached unless
   `--no-cache`/`--reset-cache`).
4. **Filter titles**: apply `--wanted`, `--latest-episode`, or `--select-titles`.
5. **Get tracks**: parse the manifest into video, audio, subtitle, and chapter tracks.
6. **Select tracks**: narrow each type down using your quality, codec, range, language,
   and bitrate flags.
7. **Download**: pull segments for the selected tracks (concurrently per `--downloads`).
8. **License & decrypt**: acquire content keys (vault → CDM), decrypt each track.
9. **Post-process**: extract closed captions, convert subtitles, repack, mux with
   `mkvmerge`, and move the finished file to your downloads directory.

`--list` prints the tracks a title exposes and stops before selection. `--list-titles`
prints every title the service returned and stops before `--wanted`/`--latest-episode`
filtering. `--skip-dl` runs the license step but skips the actual segment download.

## Choosing quality

### Resolution

`-q` / `--quality` takes one or more target resolutions (heights) as a comma-separated
list. Without it, unshackle picks the **best available** resolution.

```shell title="Single resolution"
unshackle dl -q 1080 EXAMPLE 81234567
```

```shell title="Multiple resolutions in one run"
unshackle dl -q 2160,1080,720 EXAMPLE 81234567
```

!!! note "16:9 canvas matching"
    Resolution matching is by track **height** first. If no track has an exact matching
    height, unshackle falls back to a 16:9-canvas match, so `-q 1080` will also match an
    anamorphic `1920×804` track (computed as `int(width × 9 / 16)`).

### Best-available and worst

By default a missing requested resolution is an error. Two flags change that:

| Flag | Behaviour |
| --- | --- |
| `--best-available` | If the requested resolution(s) aren't present, continue with the best that *is* available instead of failing. Also softens missing video/audio/subtitle languages and hybrid fallbacks. |
| `--worst` | Within the specified quality, pick the **lowest** bitrate rendition. **Requires `-q/--quality`.** |

```shell title="Never fail on a missing resolution"
unshackle dl -q 2160 --best-available EXAMPLE 0ABC123
```

## Video codec and color range

### Codec

`-v` / `--vcodec` selects one or more video codecs; the default is any codec. It accepts
either enum **names** or their **values**, comma-separated.

| Name | Value |
| --- | --- |
| `AVC` | `H.264` |
| `HEVC` | `H.265` |
| `VC1` | `VC-1` |
| `VP8` | `VP8` |
| `VP9` | `VP9` |
| `AV1` | `AV1` |

```shell title="Either spelling works"
unshackle dl -v HEVC   EXAMPLE 81234567
unshackle dl -v H.265  EXAMPLE 81234567
unshackle dl -v hevc,avc EXAMPLE 81234567
```

### Color range

`-r` / `--range` selects one or more color ranges; the default is `SDR`.

| Range | Meaning |
| --- | --- |
| `SDR` | Standard dynamic range (default) |
| `HLG` | Hybrid Log-Gamma |
| `HDR10` | HDR10 |
| `HDR10P` | HDR10+ |
| `DV` | Dolby Vision |
| `HYBRID` | Fetch both an HDR10/HDR10+ base and a DV track, then merge them |

```shell title="Grab HDR10 and Dolby Vision in one run"
unshackle dl -q 2160 -r HDR10,DV EXAMPLE '...'
```

!!! warning "HYBRID requires dovi_tool"
    `-r HYBRID` produces a single hybrid stream by injecting the Dolby Vision RPU
    metadata onto an HDR10/HDR10+ base layer using **dovi_tool**.
    It requires the `dovi_tool` binary, resolved from unshackle's `binaries/` folder or
    your `PATH`. The normal case is a DV track plus an HDR10 or HDR10+ base. With HDR10+
    and no DV, unshackle converts the HDR10+ metadata to DV instead, which also needs
    `hdr10plus_tool`. With no DV and no HDR10+, the title fails.
    When HDR10+ is present it is preferred over HDR10 as the base layer.

    To keep an HDR10+ deliverable *alongside* the hybrid, request both ranges:
    `-r HYBRID,HDR10P`. `-r HYBRID` on its own muxes only the merged hybrid.

## Bitrate selection

By default unshackle keeps the highest-bitrate rendition for each selected
resolution/codec/range/language combination. You can constrain this.

| Flag | Purpose |
| --- | --- |
| `-vb` / `--vbitrate` | Exact video bitrate in kbps. |
| `-ab` / `--abitrate` | Exact audio bitrate in kbps. |
| `-vb-range` / `--vbitrate-range` | Video bitrate range in kbps, e.g. `6000-7000`; picks the highest within it. |
| `-ab-range` / `--abitrate-range` | Audio bitrate range in kbps, e.g. `128-256`. |

!!! warning "Exact vs range are mutually exclusive"
    `--vbitrate` cannot be combined with `--vbitrate-range`, and likewise for the audio
    pair. Pick one form.

### Real bitrate probing

Manifest-declared bitrates are sometimes rounded or wrong. Services often advertise a
**peak** or **nominal** bandwidth that is far from the stream's real average. This matters
because a track's bitrate drives the track listing, the sort order, *and* the
`--vbitrate`/`--vbitrate-range` selection above. A bogus declared value therefore makes
unshackle pick the wrong track. These flags probe the actual media size to compute a true
bitrate for the top renditions, overriding the manifest value:

- `-rvb` / `--real-video-bitrate`: probe real video bitrates (per codec/range).
- `-rab` / `--real-audio-bitrate`: probe real audio bitrates (per codec/channels/
  language). Slower than the video variant because there are more renditions to probe.

!!! note "Reading the probed numbers"
    A single-file track (DASH `SegmentBase`/`BaseURL`) is measured **exactly**. A
    multi-segment track (most HLS) is a **sampled estimate**, normally within a few percent
    of the true value. For MPEG-TS HLS the probed figure also reads a few percent *above*
    the demuxed elementary stream, because the segment bytes include container overhead.
    That is the real *delivered* size, not an over-report or a bug.

!!! tip "Why not every rendition is probed"
    Probing does not touch every rendition. Only the five highest declared-bitrate
    renditions of each quality tier are probed in parallel (video grouped by codec and
    range, audio by codec, channels, language, and descriptive flag), and unshackle
    extends a group downward while a lower unprobed rendition could still outrank a
    probed one. This keeps probing fast even when a service exposes dozens of renditions.
    Tracks whose duration can't be determined fall back to `ffprobe`, and a probe failure
    is non-fatal: the manifest value is simply left in place.

## Audio codec, channels, and Atmos

### Codec

`-a` / `--acodec` selects one or more audio codecs (comma-separated); the default is any.

| Name | Value | Codec |
| --- | --- | --- |
| `AAC` | `AAC` | Advanced Audio Coding |
| `AC3` | `DD` | Dolby Digital |
| `EC3` | `DD+` | Dolby Digital Plus |
| `AC4` | `AC-4` | Dolby AC-4 |
| `OPUS` | `OPUS` | Opus |
| `OGG` | `VORB` | Vorbis |
| `DTS` | `DTS` | DTS |
| `ALAC` | `ALAC` | Apple Lossless |
| `FLAC` | `FLAC` | FLAC |

Names, values, and a few aliases are accepted: `eac3` and `ddp` both resolve to `EC3`,
and `vorbis` resolves to `OGG`.

```shell title="Prefer Dolby Digital Plus, fall back to AAC"
unshackle dl -a EC3,AAC EXAMPLE 81234567
```

### Channels and Atmos

- `-c` / `--channels`: desired channel layout, e.g. `5.1` or `2.0`. Matching is by
  ceiling, so `5.1` implicitly matches a `6.0`-reported layout.
- `-naa` / `--noatmos`: exclude Dolby Atmos audio tracks from selection.

```shell title="5.1 audio, no Atmos"
unshackle dl -c 5.1 --noatmos EXAMPLE 0ABC123
```

## Languages

### Video and audio language

`-l` / `--lang` sets the wanted language(s) for **both video and audio**. The default is
`orig`, the title's original language.

```shell title="Original language plus English"
unshackle dl -l orig,en EXAMPLE 81234567
```

The special token `orig` is resolved to the title's actual original language everywhere
it is used. You can override each stream type independently:

- `-vl` / `--v-lang`: language for **video only** (overrides `-l` for video). Useful
  when the burned-in video language differs from the audio you want.
- `-al` / `--a-lang`: language for **audio only** (overrides `-l` for audio).

```shell title="English audio over the original video"
unshackle dl -al en EXAMPLE 81234567
```

### Sort order

`-l` and `-sl` **select** languages: naming some removes the rest. To keep every language
but decide which ones come first, set a priority list in your configuration file:

```yaml title="unshackle.yaml"
audio:
  language_priority: [orig, en]
subtitle:
  group_by: language
  language_priority: [en, es, fr]
```

Subtitles then start with English, Spanish, and French. The languages you leave out follow
alphabetically, after the title's original language. Nothing is removed. Audio works the
same way, but the languages you leave out keep their bitrate and codec order instead of an
alphabetical one.

See [`subtitle.language_priority`](../reference/configuration.md#subtitle) for the details.

### Exact vs fuzzy matching

By default language matching is fuzzy: `-l en` also accepts `en-US`, `en-GB`, etc. (up
to a small distance). Pass `--exact-lang` to require an exact match: with it, `-l es-419`
matches only `es-419`, not `es-ES`.

!!! tip "The `all` and `best` tokens"
    For audio, the tokens `all` and `best` bypass the usual one-track-per-language pick
    and instead select the best track for **each** language present. For subtitles, `all`
    keeps every subtitle language.

## Subtitles

### Selecting subtitle languages

`-sl` / `--s-lang` sets the wanted subtitle language(s). The default is `all`, meaning
every available subtitle language is downloaded.

```shell title="Only English and Spanish subtitles"
unshackle dl -sl en,es EXAMPLE 81234567
```

### Requiring subtitles

`--require-subs` takes a list of languages that **must** exist. If they all exist, *all*
subtitles are downloaded; if any is missing, the title fails. This is useful for gating a
download on the presence of a specific subtitle track.

!!! warning "Cannot combine with `--s-lang`"
    `--require-subs` and `--s-lang` are mutually exclusive. Use one or the other.

### Forced subtitles and output format

- `-fs` / `--forced-subs`: include forced subtitle tracks (signs/foreign dialogue).
  Without this flag, forced subs are dropped from selection.
- `-fsl` / `--forced-s-lang`: keep forced subtitles only in these languages (implies
  `-fs`). Works independently of `--s-lang`, so `-sl all -fsl en` grabs every full
  subtitle but only the English forced track.
- `--sub-format`: set the output subtitle format, converting only when necessary.
  Accepts codec names/values and common aliases (`srt`, `vtt`, `ass`, `ssa`, `ttml`,
  etc.), or the literal `original` to keep the source format.

| Value | Format |
| --- | --- |
| `SRT` / `srt` | SubRip |
| `VTT` / `vtt` | WebVTT |
| `ASS` / `ass` | Advanced SubStation Alpha |
| `SSA` / `ssa` | SubStation Alpha |
| `TTML` / `ttml` | Timed Text Markup |
| `original` | Keep the source format, no conversion |

```shell title="Convert subtitles to SRT"
unshackle dl --sub-format srt EXAMPLE 81234567
```

!!! note "SDH stripping happens by default"
    When a subtitle track is marked SDH (for the deaf/hard-of-hearing) and there is no
    plain same-language subtitle, unshackle produces a stripped, non-SDH variant
    automatically. This behaviour is governed by the `subtitle.strip_sdh` config option
    (default on).

## Selecting episodes

For series, several flags control which episodes are downloaded. By default, **all**
episodes are downloaded.

### Wanted ranges

`-w` / `--wanted` accepts flexible season/episode ranges, comma-separated. Prefix a token
with `-` to exclude it.

=== "Whole seasons"

    ```shell
    unshackle dl -w S01 EXAMPLE 81234567
    unshackle dl -w S01-S03 EXAMPLE 81234567
    unshackle dl -w S01,S03,S05 EXAMPLE 81234567
    ```

=== "Specific episodes"

    ```shell
    unshackle dl -w S01E01 EXAMPLE 81234567
    unshackle dl -w S01E01-S01E05 EXAMPLE 81234567
    unshackle dl -w S01E01-S02E03 EXAMPLE 81234567
    ```

=== "Exclusions"

    ```shell
    # Seasons 1 through 5, but not season 3
    unshackle dl -w S01-S05,-S03 EXAMPLE 81234567
    ```

### Other selection flags

| Flag | Behaviour |
| --- | --- |
| `--select-titles` | Interactively pick episodes of a series, or films when a title has more than one. **Cannot combine with `-w`.** |
| `--latest-episode` | Download only the single most recent episode. |
| `--list-titles` | List every title the service returned, then stop. `-w`/`--latest-episode` are not applied to this listing. |

```shell title="Grab just the newest episode"
unshackle dl --latest-episode EXAMPLE 81234567
```

## Including and excluding track types

You can restrict a download to certain track categories, either positively (only these)
or negatively (everything but these).

**Only these types** (`*-only` flags):

- `-V` / `--video-only`
- `-A` / `--audio-only`
- `-S` / `--subs-only`
- `-C` / `--chapters-only`

**Skip these types** (`no-*` flags):

- `-nv` / `--no-video`
- `-na` / `--no-audio`
- `-ns` / `--no-subs`
- `-nc` / `--no-chapters`

Additional track-type flags:

- `-ad` / `--audio-description`: include descriptive (audio-description) tracks, which
  are dropped by default.
- `--skip-subtitle-errors`: if a subtitle fails to download, skip it and continue rather
  than aborting the whole title. Video and audio failures remain fatal.

```shell title="Subtitles only"
unshackle dl -S -sl en EXAMPLE 81234567
```

```shell title="Everything except chapters"
unshackle dl -nc EXAMPLE 81234567
```

!!! note
    You can combine an `*-only` flag with `no-*` flags to fine-tune: `*-only` chooses the
    starting set of categories, then `no-*` subtracts from it. Attachments (e.g. fonts)
    are always kept.

## Listing and dry runs

Before committing to a long download, inspect what unshackle *would* do:

| Flag | Effect |
| --- | --- |
| `--list` | List the tracks the service exposes for each title, then stop. No selection, no download. |
| `--list-titles` | List every title the service returned, then stop. `-w`/`--latest-episode` are not applied to this listing. |
| `--skip-dl` | Skip downloading but still acquire the decryption keys. |

```shell title="See the track selection without downloading"
unshackle dl -q 1080 -v H.265 -r HDR10 --list EXAMPLE 81234567
```

## Output and muxing

### Output location and folders

- `-o` / `--output`: override the output directory for this run (otherwise the
  configured downloads directory is used).
- `--no-folder`: do not create a per-show folder for TV downloads.
- `--no-source`: remove the service source tag from the filename and path.

```shell title="Send this download somewhere specific"
unshackle dl -o ~/Videos/incoming EXAMPLE 81234567
```

### Muxing behaviour

By default, tracks are muxed into a single Matroska (`.mkv`) file with `mkvmerge`. These
flags change how the output is assembled:

| Flag | Behaviour | Default source |
| --- | --- | --- |
| `--no-mux` | Do not mux; keep the individual track files. | - |
| `--split-audio` | Write a separate output file per audio codec instead of merging all audio. | config `muxing.merge_audio` (on) |
| `--merge-video` | Mux video tracks that share a height, range, and codec into one file, so only language varies inside a file. | config `muxing.merge_video` (off) |

### Naming tags

- `--tag`: set the release group tag (overrides the configured tag).
- `--repack`: add a `REPACK` tag to the output filename.

```shell title="Custom group tag and a REPACK label"
unshackle dl --tag MYGRP --repack EXAMPLE 81234567
```

## Proxies

`--proxy` accepts a full proxy URI, a 2-letter country code (resolved through your
configured proxy providers), or a `provider:region` form.

```shell title="Proxy forms"
unshackle dl --proxy us EXAMPLE 81234567
unshackle dl --proxy nordvpn:ca EXAMPLE 81234567
unshackle dl --proxy 'http://user:pass@host:8080' EXAMPLE 81234567
```

Two related flags:

- `--no-proxy`: force-disable all proxy use for this run.
- `--no-proxy-download`: bypass the proxy for **all downloads**. The manifest,
  license, and authentication requests still go through the proxy. This is useful when the
  proxy is only needed to satisfy geo-checks, not to move the bulk of the data.

## Performance and caching

| Flag | Purpose |
| --- | --- |
| `--workers N` | Threads used per track for segment downloads. Default depends on the downloader. |
| `--downloads N` | Number of tracks downloaded concurrently. Default `1`. |
| `--slow [MIN-MAX]` | Add a delay between titles to look more like a real device. `--slow` alone means 60-120s; `--slow 20-40` sets a custom range. Minimum 20s. |
| `--no-cache` | Bypass the title cache for this download. |
| `--reset-cache` | Clear the title cache before fetching. |

```shell title="Two tracks at a time, eight threads each"
unshackle dl --downloads 2 --workers 8 EXAMPLE 81234567
```

```shell title="Space out a season download"
unshackle dl -w S01 --slow 30-60 EXAMPLE 81234567
```

## Keys, vaults, and export

By default, unshackle checks your **key vaults** first and only asks a **CDM** to license
a key when the vault misses. You can force one side or the other:

- `--cdm-only`: only use the CDM (skip vaults).
- `--vaults-only`: only use key vaults (never license via the CDM); a missing key fails.

```shell title="Fetch keys only, no download"
unshackle dl --skip-dl EXAMPLE 81234567
```

`--export` writes a JSON file, into the configured exports directory, containing track
info and the acquired content keys for each title. This is the format consumed by
`unshackle import` to reconstruct a download later.

```shell title="Export track info and keys"
unshackle dl --skip-dl --export EXAMPLE 81234567
```

!!! note "Region is recorded only with a proxy"
    When `--proxy` is used, the export records the region so an import can reproduce the
    correct geofence. Without a proxy, no region is stored.

!!! warning "Some DASH and Smooth exports need a title language"
    An import re-fetches the DASH or ISM manifest and parses it again. Most manifests label
    their own streams, and those import fine. When a manifest labels nothing, the parse falls
    back to the title's original language, which comes from `Title.language` on the exporting
    service. If a service never sets it, importing that export fails with a message naming
    the service. Neither end guesses a language for you, so the fix belongs in the service.

## Metadata and tagging

unshackle looks up metadata automatically, but you can override the identifiers used for
tagging and naming:

| Flag | Example | Purpose |
| --- | --- | --- |
| `--tmdb` | `--tmdb 27205` | Use this TMDB ID instead of automatic lookup. |
| `--imdb` | `--imdb tt1375666` | Use this IMDb ID. |
| `--tvdb` | `--tvdb 73871` | Use this TVDB ID instead of looking the series up. |
| `--animeapi` | `--animeapi mal:12345` | Resolve via AnimeAPI (`mal:`/`anilist:` prefix; defaults to MAL). |
| `--enrich` | - | Override the show title and year from an external source. **Requires** one of `--tmdb`, `--imdb`, or `--animeapi`. |
| `--tvdb-order` | `--tvdb-order dvd` | Renumber episodes to a TVDB season order. Needs `tvdb_api_key`. |

```shell title="Force the right IMDb match and enrich the title"
unshackle dl --imdb tt1375666 --enrich EXAMPLE 81234567
```

### Episode ordering

A service does not always number a series the way TVDB's aired order does. Some services list
Futurama in TVDB's `alternate` (Streaming) order, for example, while TVDB's `official` order
holds back four season-one episodes to the start of season two. `--tvdb-order` works out which
order the service used, then renumbers the episodes into the order you asked for:

```shell title="Download Futurama in DVD order"
unshackle dl --tvdb-order dvd EXAMPLE 81234567
```

Available orders are `official` (aired), `dvd`, `absolute`, `alternate`, and `regional`. Set
`tvdb_order` in your config to apply one by default.

!!! warning "Orders that do not cover the whole series"
    An order can leave out episodes the service carries. TVDB's `dvd` order does not list
    Futurama's four movies. Those episodes keep their original numbering. If that would give
    two episodes the same season/episode slot, and so the same filename, unshackle logs an
    error and leaves the numbering untouched. Pick an order that covers the whole series.

## Configuration defaults

Every flag on this page can be set as a default in your configuration file so you don't
have to type it each time, and defaults can be scoped per service. See
[Configuration file](../getting-started/configuration-file.md) for the `dl:` block and
per-service overrides.

!!! tip "Explicit flags always win"
    A value you pass on the command line (or via an environment variable) always takes
    precedence over both the global `dl:` defaults and any per-service `dl:` overrides.
    Config only fills in options you didn't set explicitly.

## Quick reference

A condensed lookup of the most-used options. Run `unshackle dl --help` for the complete,
authoritative list.

| Flag | Short | Meaning |
| --- | --- | --- |
| `--quality` | `-q` | Target resolution(s), e.g. `1080,720`. |
| `--vcodec` | `-v` | Video codec(s). |
| `--acodec` | `-a` | Audio codec(s). |
| `--range` | `-r` | Color range(s); default `SDR`. |
| `--channels` | `-c` | Audio channel layout. |
| `--noatmos` | `-naa` | Exclude Atmos audio. |
| `--lang` | `-l` | Video + audio language(s); default `orig`. |
| `--a-lang` | `-al` | Audio-only language override. |
| `--v-lang` | `-vl` | Video-only language override. |
| `--s-lang` | `-sl` | Subtitle language(s); default `all`. |
| `--forced-subs` | `-fs` | Include forced subtitles. |
| `--forced-s-lang` | `-fsl` | Forced subtitle language(s); implies `-fs`. |
| `--sub-format` | | Output subtitle format. |
| `--wanted` | `-w` | Episode/season range. |
| `--select-titles` | | Interactively pick episodes or films. |
| `--latest-episode` | | Only the newest episode. |
| `--video-only` / `--audio-only` / `--subs-only` | `-V` / `-A` / `-S` | Restrict track types. |
| `--no-video` / `--no-audio` / `--no-subs` / `--no-chapters` | `-nv` / `-na` / `-ns` / `-nc` | Skip track types. |
| `--worst` | | Lowest bitrate within `-q`. |
| `--best-available` | | Degrade gracefully instead of failing. |
| `--output` | `-o` | Output directory for this run. |
| `--split-audio` / `--merge-video` / `--no-mux` | | Muxing behaviour. |
| `--proxy` / `--no-proxy` / `--no-proxy-download` | | Proxy control. |
| `--workers` / `--downloads` / `--slow` | | Concurrency and pacing. |
| `--list` / `--list-titles` / `--skip-dl` | | Dry runs. |
| `--cdm-only` / `--vaults-only` | | Key source control. |
| `--export` | | Export track info and keys to JSON. |
| `--tmdb` / `--imdb` / `--tvdb` / `--animeapi` / `--enrich` | | Metadata overrides. |
| `--tvdb-order` | | Renumber episodes to a TVDB season order. |
