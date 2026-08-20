# Downloading

`unshackle dl` is the main download command: it fetches a title's manifest from a
streaming service, selects exactly the tracks you asked for, acquires and applies
decryption keys, and muxes everything into a finished Matroska file. The flags below
cover quality and codec selection, language and subtitle handling, episode ranges, and
the download flow itself.

!!! note "Where the full flag list lives"
    This page covers the flags you will use most and explains how they interact. It is
    not an exhaustive enumeration. For the complete, always-current list of every option
    and its exact wording, use `unshackle dl --help`. Every flag shown here can also be
    given a default in your configuration file. See
    [Configuration file](../getting-started/configuration-file.md).

## Command shape

A download is always three layers: the root CLI, the `dl` command (which is where
unshackle parses all the flags below), and a **service tag** subcommand that carries the
title argument.

```shell
unshackle dl [OPTIONS] SERVICE [SERVICE ARGUMENTS]
```

- `unshackle`: the root command.
- `dl`: the download command. **unshackle parses all of the options on this page here**,
  so they go *before* the service tag.
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
    Because each service defines its own positional argument, use
    `unshackle dl SERVICE --help` (for example `unshackle dl EXAMPLE --help`) to see that
    service's own argument and any service-specific options.

## The download flow

When you start a download, `dl` moves through the pipeline below. The order tells you
why, for example, `--list` stops early, and why unshackle fetches the content keys even
with `--skip-dl`.

1. **Setup**: load the DRM CDM, key vaults, proxy providers, cookies, and credentials.
2. **Authenticate**: sign in to the service with your profile's cookies/credentials.
3. **Fetch titles**: retrieve the movie, episode list, or album (cached unless
   `--no-cache`/`--reset-cache`).
4. **Filter titles**: apply `--wanted`, `--latest-episode`, or `--select-titles`.
5. **Get tracks**: parse the manifest into video, audio, subtitle, and chapter tracks.
6. **Select tracks**: narrow each type down using your quality, codec, range, language,
   and bitrate flags.
7. **Download**: pull segments for the selected tracks (concurrently per `--downloads`).
8. **License & decrypt**: get the content keys (vault → CDM), decrypt each track.
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
    Resolution matching is by track **height** first. If no track height matches exactly,
    unshackle falls back to a 16:9-canvas match, so `-q 1080` also selects an
    anamorphic `1920×804` track (computed as `int(width × 9 / 16)`).

### Best-available and worst

By default a missing requested resolution is an error. Two flags change that:

| Flag | Behaviour |
| --- | --- |
| `--best-available` | If the requested resolution(s) are not present, continue with the best that *is* available instead of failing. Also softens missing video/audio/subtitle languages and hybrid fallbacks. |
| `--worst` | Within the specified quality, pick the **lowest** bitrate rendition. **Requires `-q/--quality`.** |

```shell title="Never fail on a missing resolution"
unshackle dl -q 2160 --best-available EXAMPLE 0ABC123
```

## Video codec and colour range

### Codec

`-v` / `--vcodec` selects one or more video codecs. The default is any codec. It accepts
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

### Colour range

`-r` / `--range` selects one or more colour ranges. The default is `SDR`.

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
    `-r HYBRID` makes a single hybrid track by injecting the Dolby Vision RPU
    metadata onto an HDR10/HDR10+ base layer with **dovi_tool**.
    It requires the `dovi_tool` binary, resolved from unshackle's `binaries/` folder or
    your `PATH`. The normal case is a DV track plus an HDR10 or HDR10+ base. With HDR10+
    and no DV, unshackle converts the HDR10+ metadata to DV instead, which also needs
    `hdr10plus_tool`. With no DV and no HDR10+, the title fails.
    When HDR10+ is present, unshackle prefers it over HDR10 as the base layer.

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
    You cannot combine `--vbitrate` with `--vbitrate-range`, and likewise for the audio
    pair. Pick one form.

### Real bitrate probing

Manifest-declared bitrates are sometimes rounded or wrong. Services often advertise a
**peak** or **nominal** bandwidth that is far from the track's real average. This matters
because a track's bitrate drives the track listing, the sort order, *and* the
`--vbitrate`/`--vbitrate-range` selection above. A bogus declared value therefore makes
unshackle pick the wrong track. These flags probe the actual media size to compute a true
bitrate for the top renditions, overriding the manifest value:

- `-rvb` / `--real-video-bitrate`: probe real video bitrates (per codec/range).
- `-rab` / `--real-audio-bitrate`: probe real audio bitrates (per codec/channels/
  language). Slower than the video variant because there are more renditions to probe.

!!! note "Reading the probed numbers"
    unshackle measures a single-file track (DASH `SegmentBase`/`BaseURL`) **exactly**. A
    multi-segment track (most HLS) is a **sampled estimate**, normally within a few percent
    of the true value. For MPEG-TS HLS the probed figure also reads a few percent *above*
    the demuxed elementary stream, because the segment bytes include container overhead.
    That is the real *delivered* size, not an over-report or a bug.

!!! tip "Why not every rendition is probed"
    Probing does not touch every rendition. unshackle probes only the five highest
    declared-bitrate renditions of each quality tier, in parallel (video grouped by codec
    and range, audio by codec, channels, language, and descriptive flag). It extends a
    group downward while a lower unprobed rendition could still outrank a probed one.
    This keeps probing fast even when a service exposes dozens of renditions. A track
    whose duration unshackle cannot find falls back to `ffprobe`. A probe failure is
    non-fatal: the manifest value stays in place.

## Audio codec, channels, and Atmos

### Codec

`-a` / `--acodec` selects one or more audio codecs (comma-separated). The default is any.

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

unshackle accepts names, values, and a few aliases: `eac3` and `ddp` both give `EC3`,
and `vorbis` gives `OGG`.

```shell title="Prefer Dolby Digital Plus, fall back to AAC"
unshackle dl -a EC3,AAC EXAMPLE 81234567
```

### Channels and Atmos

- `-c` / `--channels`: desired channel layout, for example `5.1` or `2.0`. Matching is by
  ceiling, so `5.1` implicitly matches a `6.0`-reported layout.
- `-naa` / `--noatmos`: exclude Atmos audio tracks from selection.

```shell title="5.1 audio, no Atmos"
unshackle dl -c 5.1 --noatmos EXAMPLE 0ABC123
```

### Music quality

A music service gives one `Audio` track for each codec and bitrate it offers for a song.
The audio options above choose between them: `-a` picks the codec and `-ab` or `-ab-range`
picks the bitrate. If you set neither, unshackle takes the best track.

`-q` / `--quality` is a video height. It has no effect on a music download.

```shell title="Take the lossless track"
unshackle dl -a FLAC EXAMPLE 81234567
```

```shell title="Take the lossy track at 320 kb/s"
unshackle dl -a AAC -ab 320 EXAMPLE 81234567
```

!!! tip "Two FLAC tracks in one release"
    A service can offer both a CD FLAC and a hi-res FLAC. The two have the same codec, so
    the bitrate is what separates them. Use `--list` to read the bitrates, then give `-ab`
    the one you want.

## Languages

### Video and audio language

`-l` / `--lang` sets the wanted language(s) for **both video and audio**. The default is
`orig`, the title's original language.

```shell title="Original language plus English"
unshackle dl -l orig,en EXAMPLE 81234567
```

unshackle changes the special token `orig` to the title's own original language
everywhere you use it. You can override each track type independently:

- `-vl` / `--v-lang`: language for **video only** (overrides `-l` for video). Useful
  when the burned-in video language differs from the audio you want.
- `-al` / `--a-lang`: language for **audio only** (overrides `-l` for audio).

```shell title="English audio over the original video"
unshackle dl -al en EXAMPLE 81234567
```

### Excluding languages

Put a `-` in front of a language to remove it from the selection. This works on `-l`,
`-vl`, `-al`, `-sl`, and `-fsl`, in the same way as it works on `-w` / `--wanted`.

```shell title="Every subtitle language except Spanish"
unshackle dl -sl all,-es EXAMPLE 81234567
```

unshackle subtracts the exclusions from the languages the flag would otherwise select,
and the order of the values does not matter. If you give only exclusions, the flag keeps its own
default: `-sl -es` is the same as `-sl all,-es`, and `-l -es` is the same as `-l orig,-es`.

```shell title="Only exclusions"
# every subtitle language except Spanish and French
unshackle dl -sl -es,-fr EXAMPLE 81234567

# the original audio language, unless it is Spanish
unshackle dl -l -es EXAMPLE 81234567
```

`-vl` and `-al` have no default of their own: they cascade to `-l`. An override that names
a language replaces `-l` for that track type, exclusions included. An override that gives
only exclusions adds them to the ones from `-l`.

```shell title="Cascade"
# video keeps the Japanese track; only the audio drops Spanish
unshackle dl -l en,-es -vl ja EXAMPLE 81234567
```

!!! note "Rules for exclusion tokens"
    - `orig` becomes the title's own original language, so `-sl -orig` drops the
      subtitles in that language.
    - `--exact-lang` applies to exclusions with the same matching rules as selection:
      `-es` removes exactly the tracks that `es` would select. Without it, `-es` also
      removes `es-419` and `es-ES`.
    - Tracks with no language tag are never excluded.
    - unshackle rejects `-all`. Name the languages you do not want instead.
    - The comma form is the documented style. `-sl -es` also works, but a shell can read
      a lone `-es` as another option.

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
alphabetically, after the title's original language. unshackle removes nothing. Audio
works the same way, but the languages you leave out keep their bitrate and codec order instead of an
alphabetical one.

See [`subtitle.language_priority`](../reference/configuration/download.md#subtitle) for the details.

### Exact vs fuzzy matching

By default language matching is fuzzy: `-l en` also accepts `en-US`, `en-GB`, and the
other regional forms (up to a small distance). Pass `--exact-lang` when only an exact
match is acceptable: with it, `-l es-419` selects only `es-419`, not `es-ES`.

!!! tip "The `all` and `best` tokens"
    For audio, the tokens `all` and `best` bypass the usual one-track-per-language pick
    and instead select the best track for **each** language present. For subtitles, `all`
    keeps every subtitle language.

## Subtitles

### Selecting subtitle languages

`-sl` / `--s-lang` sets the wanted subtitle language(s). The default is `all`, so
unshackle downloads every available subtitle language.

```shell title="Only English and Spanish subtitles"
unshackle dl -sl en,es EXAMPLE 81234567
```

To keep the default and drop one language, use the `-` prefix described in
[Excluding languages](#excluding-languages). `-sl all,-es` keeps every subtitle language
except Spanish, forced Spanish subtitles included.

### Requiring subtitles

`--require-subs` takes a list of languages that **must** exist. If they all exist,
unshackle downloads *all* subtitles. If any is missing, the title fails. This is useful
when you want a download only if a specific subtitle track is present.

!!! warning "Cannot combine with `--s-lang`"
    `--require-subs` and `--s-lang` are mutually exclusive. Use one or the other.

### Forced subtitles and output format

- `-fs` / `--forced-subs`: include forced subtitle tracks (signs/foreign dialogue).
  Without this flag, unshackle drops forced subtitle tracks from the selection.
- `-fsl` / `--forced-s-lang`: keep forced subtitles only in these languages (implies
  `-fs`). Works independently of `--s-lang`, so `-sl all -fsl en` grabs every full
  subtitle but only the English forced track. It accepts exclusions too: `-fsl all,-es`
  keeps every forced subtitle except the Spanish one, and `-fsl -es` means the same.
- `--sub-format`: set the output subtitle format, converting only when necessary.
  Accepts codec names/values and common aliases (`srt`, `vtt`, `ass`, `ssa`, `ttml`,
  and the other codec aliases), or the literal `original` to keep the source format.

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
    When a subtitle track carries the SDH flag (for the deaf/hard-of-hearing) and there
    is no plain same-language subtitle, unshackle makes a stripped, non-SDH track
    automatically. The `subtitle.strip_sdh` config option controls this behaviour
    (default on).

## Selecting episodes and tracks

For series, several flags control which episodes unshackle downloads. The same flags
select the tracks of a music release. By default, unshackle downloads **all** episodes and
**all** tracks.

### Wanted ranges

`-w` / `--wanted` accepts season and episode ranges, comma-separated. Prefix a token
with `-` to exclude it. For a music release, the same option takes track numbers. Read
[music tracks](#music-tracks) below.

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

=== "Split episodes"

    ```shell
    # Part 2 of episode 1 only
    unshackle dl -w S01E01.2 EXAMPLE 81234567

    # Parts 1 through 3 of episode 1
    unshackle dl -w S01E01.1-S01E01.3 EXAMPLE 81234567

    # All of season 1 except episode 1 part 2
    unshackle dl -w S01,-S01E01.2 EXAMPLE 81234567
    ```

=== "Air dates"

    ```shell
    # One day
    unshackle dl -w 2026-08-11 EXAMPLE 81234567

    # A range of days, colon separated
    unshackle dl -w 2026-08-01:2026-08-31 EXAMPLE 81234567

    # August, but not the 15th
    unshackle dl -w 2026-08-01:2026-08-31,-2026-08-15 EXAMPLE 81234567
    ```

### Music tracks

`-w` selects the tracks of an album, EP, single, or playlist. Give a track by its number.
A release with more than one disc uses the `{disc}x{track}` form. A number on its own is a
track on disc 1, which is the number the track list shows for a single-disc release.

=== "Tracks"

    ```shell
    # Track 3
    unshackle dl -w 3 EXAMPLE 81234567

    # Tracks 1 through 5
    unshackle dl -w 1-5 EXAMPLE 81234567

    # Tracks 1, 3 and 7
    unshackle dl -w 1,3,7 EXAMPLE 81234567
    ```

=== "More than one disc"

    ```shell
    # Disc 2, track 3
    unshackle dl -w 2x3 EXAMPLE 81234567

    # Disc 2, tracks 1 through 4
    unshackle dl -w 2x1-2x4 EXAMPLE 81234567

    # Disc 1 track 3, through disc 2 track 2
    unshackle dl -w 1x3-2x2 EXAMPLE 81234567
    ```

=== "Exclusions"

    ```shell
    # Tracks 1 through 8, but not track 4
    unshackle dl -w 1-8,-4 EXAMPLE 81234567
    ```

!!! tip "Read the track list first"
    `--list-titles` prints the release with its disc and track numbers, so you can see
    which numbers to give `-w`.

### Daily and date-based content

Talk shows, news and sports have no official episode numbering, so unshackle names them
by air date. unshackle writes an episode that carries an air date as `Show.YYYY.MM.DD`
instead of `SxxExx`, the `{date}` token holds the ISO date, and the season folder becomes
the year.

A service that only carries this kind of title sets `DAILY = True` on its class, and a
service can set `air_date` on each episode itself. Add `--daily` to mark any other title
as date-based:

```shell
unshackle dl --daily --tvdb 73871 --enrich EXAMPLE 81234567
```

With `--enrich` and a TVDB ID, `--daily` fills in the air date of every episode that has
none. An air date the service already set is kept. unshackle skips dates before 1970 and
dates in the future, because TVDB carries placeholder schedule dates for episodes that
have not aired. Without `--enrich` unshackle has no source to fill from, and says so.

A dated episode answers to its air date in `-w`, as well as to its `SxxExx` token. A date
token is a plain ISO date (`2026-08-11`). A date range uses a colon (`2026-08-01:2026-08-31`),
because the dashes in a date are part of the date. A range cannot span more than 1000 days.
You can mix date tokens and `SxxExx` tokens in one `-w`.

### Split episodes

A few services split one episode into several separately playable videos. Where a service
reports that, add `.N` after the episode to pick one part.

| Token | Selects |
| --- | --- |
| `S01E01` | All parts of episode 1. |
| `S01E01.2` | Part 2 of episode 1, and nothing else. |
| `S01E01.1-S01E01.3` | Parts 1 to 3 of episode 1. |
| `S01,-S01E01.2` | All of season 1 except episode 1 part 2. Parts 1 and 3 are kept. |

A season token such as `S01` covers every episode and every part, so you only need `.N`
when you want a part on its own.

!!! warning "A part range stays inside one episode"
    `S01E01.1-S01E01.3` is valid. unshackle rejects `S01E01.1-S01E02.3`, because the
    parser cannot know how many parts episode 1 has and so cannot work out where the range
    ends. To span episodes, write the parts you want as separate tokens:
    `-w S01E01.2,S01E02.1`.

!!! note "A part of an unsplit episode selects nothing"
    `-w S01E02.2` on an episode that was never split is deliberately empty: it tells you
    the episode has no parts, rather than quietly handing you the whole episode. Use
    `--list-titles` to see which episodes carry parts.

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
  unshackle drops by default.
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
    starting set of categories, then `no-*` subtracts from it. Attachments (for example fonts)
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

- `-o` / `--output`: override the output directory for this run (otherwise unshackle
  uses the configured downloads directory).
- `--no-folder`: do not make a per-show folder for TV downloads.
- `--no-source`: remove the service source tag from the filename and path.

```shell title="Send this download somewhere specific"
unshackle dl -o ~/Videos/incoming EXAMPLE 81234567
```

### Muxing behaviour

By default, `mkvmerge` muxes the tracks of a movie or an episode into a single Matroska
(`.mkv`) file. These flags change how unshackle assembles the output:

| Flag | Behaviour | Default source |
| --- | --- | --- |
| `--no-mux` | Do not mux; keep the individual track files. | - |
| `--split-audio` | Write a separate output file per audio codec instead of merging all audio. | config `muxing.merge_audio` (on) |
| `--merge-video` | Mux video tracks that share a height, range, and codec into one file, so only language varies inside a file. | config `muxing.merge_video` (off) |

!!! note "Music is never muxed"
    A song skips the muxer. unshackle keeps the container that the service
    delivered, such as `.flac`, `.m4a`, or `.mp3`, and writes the metadata into that file.
    Read [Music output files](output-and-naming.md#music-output-files).

### After the download

`--postscript "<command>"` runs your own command once per output file, with unshackle's
metadata substituted into `{variable}` placeholders. It is repeatable, and it replaces the
`post_scripts` config for that run. No `success` hook operates under `--no-mux`, because
that run writes no muxed output. A `failure` hook still operates if the download fails.

```shell title="Hand each finished file to an uploader"
unshackle dl --postscript "python /opt/upload.py {filepath} --service={service}" EXAMPLE 81234567
```

For hooks that continue across runs, for the `season` and `run` modes, for `failure` hooks
and for the full variable list, see
[Post-download scripts](../reference/configuration/post-scripts.md).

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
  license, and authentication requests still go through the proxy. This is useful when you
  need the proxy only to satisfy geo-checks, not to move the bulk of the data.

## Performance and caching

| Flag | Purpose |
| --- | --- |
| `--workers N` | Threads used per track for segment downloads. Default depends on the downloader. |
| `--adaptive-workers` | Opt-in: start with a moderate per-track worker count and ramp it up or back off based on measured CDN throughput and errors, capped at `--workers`. Off by default (fixed worker count). |
| `--download-processes N` | Opt-in: split a large segment batch across `N` download processes, each with its own worker pool. A single process tops out around 1.3 Gb/s (Python interpreter limit); `2` reaches 2.5GbE line rate on fast CDNs. Only engages for batches of 24+ segments. Ignored while `--speed-limit` (or serve's `global_speed_limit`) is set: the cap is one shared budget, which extra processes cannot share, so the download stays in a single process. Default `1`. |
| `--continue-downloads` | Opt-in: keep completed segment files when a download fails so the next run resumes instead of restarting from zero. One-off enable of the [`continue_downloads`](../reference/configuration/download.md#continue_downloads) config option, which documents what can and cannot resume. |
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
a content key when the vault misses. You can force one side or the other:

- `--cdm-only`: only use the CDM (skip vaults).
- `--vaults-only`: only use key vaults and never license through the CDM. A missing
  content key fails.

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
    When you use `--proxy`, the export records the region so an import can reproduce the
    correct geofence. Without a proxy, the export stores no region.

!!! warning "Some DASH and Smooth exports need a title language"
    An import re-fetches the DASH or ISM manifest and parses it again. Most manifests label
    their own streams, and those import fine. When a manifest labels nothing, the parse falls
    back to the title's original language, which comes from `Title.language` on the exporting
    service. If a service never sets it, importing that export fails with a message naming
    the service. Neither end guesses a language for you, so the fix belongs in the service.

## Metadata and tagging

unshackle looks up metadata automatically, but you can override the identifiers used for
tagging and naming. An ID you give is authoritative: unshackle looks it up directly instead of
searching by title, and the ID stays in the tags. An ID only settles *which* title this is.
To also take that source's title, year and original language, add `--enrich`. Give at most one
of `--tmdb`, `--imdb` and `--tvdb`, and unshackle works the rest out from it. To stop the automatic
lookups but keep the IDs you give, set
[`disable_metadata`](../reference/configuration/misc.md#external-api-keys) in your config.

| Flag | Example | Purpose |
| --- | --- | --- |
| `--tmdb` | `--tmdb 27205` | Use this TMDB ID instead of an automatic search. Needs `tmdb_api_key`. |
| `--imdb` | `--imdb tt1375666` | Use this IMDb ID instead of an automatic search. Needs no API key. |
| `--tvdb` | `--tvdb 73871` | Use this TVDB ID instead of looking the series up. Needs `tvdb_api_key`. |
| `--anilist` | `--anilist 21` | Use this AniList ID instead of an automatic search. `mal:12345` is also accepted. Needs no API key. |
| `--enrich` | - | Overwrite the show title, year and original language with the external source's. **Requires** one of `--tmdb`, `--imdb`, `--tvdb`, or `--anilist`. |
| `--tvdb-order` | `--tvdb-order dvd` | Renumber episodes to a TVDB season order. Needs `tvdb_api_key`. |

```shell title="Force the right IMDb match, and keep the service's own naming"
unshackle dl --imdb tt1375666 EXAMPLE 81234567
```

```shell title="Force the match and take IMDb's title, year and language too"
unshackle dl --imdb tt1375666 --enrich EXAMPLE 81234567
```

### What each metadata provider supplies

Metadata providers differ in what they answer with, so the fields `--enrich` can replace
depend on which ID you gave. Every metadata provider except `imdb` and `anilist` only
operates when you set its config key:

| Provider | Config key | Title and year | Original language | External IDs it returns |
| --- | --- | --- | --- | --- |
| TMDB | `tmdb_api_key` | yes | yes, alpha-2 such as `ko` | IMDb, TMDB, TVDB |
| TVDB | `tvdb_api_key` | yes | yes, alpha-3 such as `kor` | TMDB, TVDB, sometimes IMDb |
| OMDb | `omdb_api_key` | yes | yes, as an English name such as `Korean` | IMDb |
| IMDb | *(no config key needed)* | yes | yes, alpha-2 such as `ko` | IMDb |
| SIMKL | `simkl_client_id` | yes | no, it publishes a country and no language | IMDb, TMDB, TVDB |
| AniList | *(no config key needed)* | yes | yes, worked out from the country of origin, such as `ja` | AniList |

Whatever the tag looks like, unshackle normalises it before use, so `ko`, `kor` and `Korean`
all end up as the same language.

!!! note "AniList only answers for anime"
    It is last in the default metadata provider order and returns nothing for a title
    that is not anime. The only cost is one search that misses. Put `anilist` earlier in
    [`metadata_providers`](../reference/configuration/misc.md#external-api-keys) if you mostly
    download anime. [`anilist_title_language`](../reference/configuration/misc.md#external-api-keys)
    sets which of the three AniList title variants unshackle uses.

    A service can also mark its titles as anime. For those titles unshackle tries AniList
    first. When AniList has no match, it falls through to the normal order.

Each ID goes to the providers that read that kind of ID, in
[`metadata_providers`](../reference/configuration/misc.md#external-api-keys) order:

- `--tmdb` reads TMDB.
- `--imdb` reads IMDb, then falls back to OMDb. IMDb needs no API key, so this path works
  out of the box, and an OMDb API key only matters when IMDb has no answer.
- `--tvdb` reads TVDB. It needs `tvdb_api_key`.
- `--anilist` reads AniList, which needs no API key. It supplies the title, the year and the
  original language, and writes an AniList tag. It knows no TMDB, IMDB or TVDB ID, so pair it
  with `--tmdb`, `--imdb` or `--tvdb` when you want those tags as well.

Give **one** of `--tmdb`, `--imdb` and `--tvdb`. You cannot combine them, because one ID
does the job on its own: unshackle finds the other two from it and writes all three to the
tags.
Passing two is an error rather than a silent choice between them. `--anilist` is the exception
and still pairs with one of the three, which is how you tag an anime title with a western ID.

The ID you give must have a metadata provider that can find it, so unshackle makes sure of
that before it downloads anything. `--tmdb` without `tmdb_api_key`, or an ID whose metadata
providers your `metadata_providers` list leaves out, fails immediately with a message naming
what to set. `--imdb` needs no API key, because the `imdb` metadata provider is keyless
and in the default order.

With `--enrich`, the title, year and original language are all replaced with the external
source's values, whether or not the service already filled them in. A field the source does
not answer with is left alone, and unshackle logs which fields those were, so a metadata provider
with a thin record cannot blank out what the service told you.

`--enrich` also fills in the absolute episode number of each episode from TVDB's absolute
order, for any series that has one and where the service did not supply it. Anime is the
usual beneficiary, but nothing here applies only to anime. This only adds the
[`{absolute}`](../reference/configuration/output.md#output_template) naming variable.
`--enrich` never changes the season and episode numbers.

With `--daily`, `--enrich` also fills in the air date of each episode from TVDB. See
[Daily and date-based content](#daily-and-date-based-content).

!!! warning "`--enrich` replaces the original language, which affects more than the filename"
    Track selection reads the original language, so replacing it changes which audio is
    treated as the original. That is the point when a service mislabels it, but it means a
    wrong ID can pick the wrong audio track and not only write a wrong name. Without
    `--enrich`, unshackle keeps the service's own value and does not change track
    selection.

### Episode ordering

A service does not always number a series the way TVDB's aired order does. Some services use
TVDB's `alternate` (Streaming) order for Futurama, for example, while TVDB's `official` order
holds back four season-one episodes to the start of season two. `--tvdb-order` works out which
order the service used, then renumbers the episodes into the order you asked for:

```shell title="Download Futurama in DVD order"
unshackle dl --tvdb-order dvd EXAMPLE 81234567
```

Available orders are `official` (aired), `dvd`, `absolute`, `alternate`, and `regional`. Set
`tvdb_order` in your config to apply one by default.

!!! warning "Orders that do not cover the whole series"
    An order can leave out episodes the service carries. TVDB's `dvd` order does not
    contain Futurama's four movies. Those episodes keep their original numbering. If that would give
    two episodes the same season/episode slot, and so the same filename, unshackle logs an
    error and leaves the numbering untouched. Pick an order that covers the whole series.

## Configuration defaults

You can set every flag on this page as a default in your configuration file so you do not
have to type it each time, and you can scope defaults per service. See
[Configuration file](../getting-started/configuration-file.md) for the `dl:` block and
per-service overrides.

!!! tip "Explicit flags always win"
    A value you pass on the command line (or through an environment variable) always takes
    precedence over both the global `dl:` defaults and any per-service `dl:` overrides.
    Config only fills in options you did not set explicitly.

## Quick reference

A condensed lookup of the most-used options. Use `unshackle dl --help` for the complete,
authoritative list.

| Flag | Short | Meaning |
| --- | --- | --- |
| `--quality` | `-q` | Target resolution(s), e.g. `1080,720`. |
| `--vcodec` | `-v` | Video codec(s). |
| `--acodec` | `-a` | Audio codec(s). |
| `--range` | `-r` | Colour range(s). Default `SDR`. |
| `--channels` | `-c` | Audio channel layout. |
| `--noatmos` | `-naa` | Exclude Atmos audio. |
| `--lang` | `-l` | Video + audio language(s); default `orig`. `-` excludes, e.g. `all,-es`. |
| `--a-lang` | `-al` | Audio-only language override. `-` excludes. |
| `--v-lang` | `-vl` | Video-only language override. `-` excludes. |
| `--s-lang` | `-sl` | Subtitle language(s); default `all`. `-` excludes, e.g. `all,-es`. |
| `--forced-subs` | `-fs` | Include forced subtitles. |
| `--forced-s-lang` | `-fsl` | Forced subtitle language(s); implies `-fs`. `-` excludes. |
| `--sub-format` | | Output subtitle format. |
| `--wanted` | `-w` | Episode/season range, an air date, or a track number for a music release (`1-5`, `2x3`). |
| `--daily` | | Fill missing air dates from TVDB during `--enrich`. |
| `--select-titles` | | Interactively pick episodes or films. |
| `--latest-episode` | | Only the newest episode. |
| `--video-only` / `--audio-only` / `--subs-only` | `-V` / `-A` / `-S` | Restrict track types. |
| `--no-video` / `--no-audio` / `--no-subs` / `--no-chapters` | `-nv` / `-na` / `-ns` / `-nc` | Skip track types. |
| `--worst` | | Lowest bitrate within `-q`. |
| `--best-available` | | Degrade gracefully instead of failing. |
| `--output` | `-o` | Output directory for this run. |
| `--split-audio` / `--merge-video` / `--no-mux` | | Muxing behaviour. |
| `--postscript` | | Run a command after each output file. Repeatable. |
| `--proxy` / `--no-proxy` / `--no-proxy-download` | | Proxy control. |
| `--workers` / `--downloads` / `--slow` | | Concurrency and pacing. |
| `--list` / `--list-titles` / `--skip-dl` | | Dry runs. |
| `--cdm-only` / `--vaults-only` | | Content key source control. |
| `--export` | | Export track info and keys to JSON. |
| `--tmdb` / `--imdb` / `--tvdb` / `--anilist` / `--enrich` | | Metadata overrides. |
| `--tvdb-order` | | Renumber episodes to a TVDB season order. |
