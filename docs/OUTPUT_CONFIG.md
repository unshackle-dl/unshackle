# Output & Naming Configuration

This document covers output file organization and naming configuration options.

## filenames (dict)

Override the default filenames used across unshackle.
The filenames use various variables that are replaced during runtime.

The following filenames are available and may be overridden:

- `log` - Log filenames. Uses `{name}` and `{time}` variables.
- `debug_log` - Debug log filenames. Uses `{service}` and `{time}` variables.
- `config` - Service configuration filenames.
- `root_config` - Root configuration filename.
- `chapters` - Chapter export filenames. Uses `{title}` and `{random}` variables.
- `subtitle` - Subtitle export filenames. Uses `{id}` and `{language}` variables.

For example,

```yaml
filenames:
  log: "unshackle_{name}_{time}.log"
  debug_log: "unshackle_debug_{service}_{time}.jsonl"
  config: "config.yaml"
  root_config: "unshackle.yaml"
  chapters: "Chapters_{title}_{random}.txt"
  subtitle: "Subtitle_{id}_{language}.srt"
```

---

## output_template (dict)

Configure custom output filename templates for movies, series, and songs.
This is **required** in your `unshackle.yaml` — a warning is shown if not configured.

Available variables: `{title}`, `{year}`, `{season}`, `{episode}`, `{season_episode}`, `{episode_name}`,
`{quality}`, `{resolution}`, `{source}`, `{audio}`, `{audio_channels}`, `{audio_full}`,
`{video}`, `{hdr}`, `{hfr}`, `{atmos}`, `{dual}`, `{multi}`, `{tag}`, `{edition}`, `{repack}`,
`{lang_tag}`

Add `?` suffix to make a variable conditional (omitted when empty): `{year?}`, `{hdr?}`, `{repack?}`

```yaml
output_template:
  # Scene-style (dot-separated)
  movies: '{title}.{year}.{repack?}.{edition?}.{quality}.{source}.WEB-DL.{dual?}.{multi?}.{audio_full}.{atmos?}.{hdr?}.{hfr?}.{video}-{tag}'
  series: '{title}.{year?}.{season_episode}.{episode_name?}.{repack?}.{edition?}.{quality}.{source}.WEB-DL.{dual?}.{multi?}.{audio_full}.{atmos?}.{hdr?}.{hfr?}.{video}-{tag}'
  songs: '{track_number}.{title}.{repack?}.{edition?}.{source?}.WEB-DL.{audio_full}.{atmos?}-{tag}'

  # Plex-friendly (space-separated)
  # movies: '{title} ({year}) {quality}'
  # series: '{title} {season_episode} {episode_name?}'
  # songs: '{track_number}. {title}'
```

Example outputs:
- Scene movies: `The.Matrix.1999.1080p.NF.WEB-DL.DDP5.1.H.264-EXAMPLE`
- Scene movies (REPACK): `Dune.2021.REPACK.2160p.HBO.WEB-DL.DDP5.1.H.265-EXAMPLE`
- Scene series: `Breaking.Bad.2008.S01E01.Pilot.1080p.NF.WEB-DL.DDP5.1.H.264-EXAMPLE`
- Plex movies: `The Matrix (1999) 1080p`

---

---

## language_tags (dict)

Automatically adds language-based identifiers (e.g., `DANiSH`, `NORDiC`, `DKsubs`) to output filenames
based on audio and subtitle track languages. Use `{lang_tag?}` in your `output_template` to place the tag.

Rules are evaluated in order; the first matching rule wins. All conditions within a single rule
must match (AND logic). If no rules match, `{lang_tag?}` is cleanly removed from the filename.

### Conditions

| Condition | Type | Description |
|-----------|------|-------------|
| `audio` | string | Matches if any selected audio track has this language |
| `subs_contain` | string | Matches if any selected subtitle has this language |
| `subs_contain_all` | list | Matches if subtitles include ALL listed languages |

Language matching uses fuzzy matching (e.g., `en` matches `en-US`, `en-GB`).

### Example: Nordic tagging

```yaml
language_tags:
  rules:
    - audio: da
      tag: DANiSH
    - audio: sv
      tag: SWEDiSH
    - audio: nb
      tag: NORWEGiAN
    - audio: en
      subs_contain_all: [da, sv, nb]
      tag: NORDiC
    - audio: en
      subs_contain: da
      tag: DKsubs

output_template:
  movies: '{title}.{year?}.{lang_tag?}.{quality}.{source}.WEB-DL.{audio_full}.{video}-{tag}'
```

Example outputs:
- Danish audio: `Show.S01E01.DANiSH.1080p.NF.WEB-DL.DDP5.1.H.264-TAG`
- English audio + multiple Nordic subs: `Show.S01E01.NORDiC.1080p.NF.WEB-DL.DDP5.1.H.264-TAG`
- English audio + Danish subs only: `Show.S01E01.DKsubs.1080p.NF.WEB-DL.DDP5.1.H.264-TAG`
- No matching languages: `Show.S01E01.1080p.NF.WEB-DL.DDP5.1.H.264-TAG`

### Example: Other regional tags

```yaml
language_tags:
  rules:
    - audio: nl
      tag: DUTCH
    - audio: de
      tag: GERMAN
    - audio: fr
      subs_contain: en
      tag: ENGFR
    - audio: fr
      tag: FRENCH
```

---

## unicode_filenames (bool)

Allow Unicode characters in output filenames. When `false`, Unicode characters are transliterated
to ASCII equivalents. Default: `false`.

---

## tag (str)

Group or Username to postfix to the end of download filenames following a dash.
Use `{tag}` in your output template to include it.
For example, `tag: "J0HN"` will have `-J0HN` at the end of all download filenames.

---

## tag_group_name (bool)

Enable/disable tagging downloads with your group name when `tag` is set. Default: `true`.

---

## tag_imdb_tmdb (bool)

Enable/disable tagging downloaded files with IMDB/TMDB/TVDB identifiers (when available). Default: `true`.

---

## muxing (dict)

- `set_title`
  Set the container title to `Show SXXEXX Episode Name` or `Movie (Year)`. Default: `true`
- `merge_audio`
  Merge all audio tracks into each output file. Default: `true`
  - `true`: All selected audio tracks are muxed into one MKV per quality.
  - `false`: Separate MKV per (quality, audio_codec) combination.
    For example: `Title.1080p.AAC.mkv`, `Title.1080p.EC3.mkv`.

  Note: The `--split-audio` CLI flag overrides this setting. When `--split-audio` is passed,
  `merge_audio` is effectively set to `false` for that run.

---

## chapter_fallback_name (str)

The Chapter Name to use when exporting a Chapter without a Name.
The default is no fallback name at all and no Chapter name will be set.

The fallback name can use the following variables in f-string style:

- `{i}`: The Chapter number starting at 1.
  E.g., `"Chapter {i}"`: "Chapter 1", "Intro", "Chapter 3".
- `{j}`: A number starting at 1 that increments any time a Chapter has no title.
  E.g., `"Chapter {j}"`: "Chapter 1", "Intro", "Chapter 2".

These are formatted with f-strings, directives are supported.
For example, `"Chapter {i:02}"` will result in `"Chapter 01"`.

---

## directories (dict)

Override the default directories used across unshackle.
The directories are set to common values by default.

The following directories are available and may be overridden,

- `commands` - CLI Command Classes.
- `services` - Service Classes.
- `vaults` - Vault Classes.
- `fonts` - Font files (ttf or otf).
- `downloads` - Downloads.
- `temp` - Temporary files or conversions during download.
- `cache` - Expiring data like Authorization tokens, or other misc data.
- `cookies` - Expiring Cookie data.
- `logs` - Logs.
- `wvds` - Widevine Devices.
- `prds` - PlayReady Devices.
- `dcsl` - Device Certificate Status List.

Notes:

- `services` accepts either a single directory or a list of directories to search for service modules.

For example,

```yaml
directories:
  downloads: "D:/Downloads/unshackle"
  temp: "D:/Temp/unshackle"
```

There are directories not listed that cannot be modified as they are crucial to the operation of unshackle.

---
