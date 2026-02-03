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

## scene_naming (bool)

Set scene-style naming for titles. When `true` uses scene naming patterns (e.g., `Prime.Suspect.S07E01...`), when
`false` uses a more human-readable style (e.g., `Prime Suspect S07E01 ...`). Default: `true`.

---

## series_year (bool)

Whether to include the series year in series names for episodes and folders. Default: `true`.

---

## tag (str)

Group or Username to postfix to the end of download filenames following a dash.
Only applies when `scene_naming` is enabled.
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
downloads: "D:/Downloads/unshackle"
temp: "D:/Temp/unshackle"
```

There are directories not listed that cannot be modified as they are crucial to the operation of unshackle.

---
