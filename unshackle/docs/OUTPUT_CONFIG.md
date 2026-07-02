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

Configure custom output filename templates for movies, series, songs, and albums.
This is **required** in your `unshackle.yaml` - a warning is shown if not configured.

Add `?` suffix to make a variable conditional (omitted when empty): `{year?}`, `{hdr?}`, `{repack?}`

### General variables

| Variable | Meaning |
|---|---|
| `title` | Title of the movie, episode, or track |
| `year` | Release year |
| `source` | Service source tag (the uppercase service directory name) |
| `tag` | Group/release tag configured via `tag:` |
| `edition` | Edition label (e.g. `Extended`, `Theatrical`) |
| `repack` | `REPACK` when the release is a repack, otherwise empty |
| `lang_tag` | Language tag produced by `language_tags` rules (e.g. `NORDiC`) |

### Series / episode variables

| Variable | Meaning |
|---|---|
| `season` | Season number (e.g. `1`) |
| `episode` | Episode number (e.g. `1`) |
| `season_episode` | Combined season/episode (e.g. `S01E01`) |
| `episode_name` | Episode title |
| `date` | Air date in ISO form (e.g. `2024-06-30`), only for daily/sports content |

!!! note "Daily / sports content"
    When a service supplies an air date (daily talk shows, news, sports events),
    `{season_episode}` automatically renders the date using your template's separator
    (e.g. `2024.06.30` for a dotted template, `2024 06 30` for a spaced one). The
    standalone `{year}` is dropped from the filename — the air date is the sole date —
    so every episode of a show names consistently. Folders still keep the year (e.g.
    `Show Name (2010)/2024/`). No template change is needed.

    Use the `{date}` variable instead if you want a fixed `YYYY-MM-DD` form regardless
    of separator. Folders for dated episodes group by air year (e.g. `Show Name/2024/`).

### Video / audio variables

| Variable | Meaning |
|---|---|
| `quality` | Resolution label (e.g. `1080p`, `2160p`) |
| `resolution` | Raw resolution string |
| `video` | Video codec label (e.g. `H.264`, `H.265`) |
| `hdr` | HDR format label (e.g. `HDR`, `DV`) |
| `hfr` | HFR label when frame rate exceeds normal threshold |
| `audio` | Audio codec label (e.g. `AAC`, `EAC3`) |
| `audio_channels` | Channel count (e.g. `2.0`, `5.1`) |
| `audio_full` | Combined codec + channels (e.g. `DDP5.1`) |
| `atmos` | `Atmos` when Dolby Atmos is present, otherwise empty |
| `dual` | `DUAL` when two audio languages are present, otherwise empty |
| `multi` | `MULTi` when three or more audio languages are present, otherwise empty |

### Music variables

| Variable | Meaning |
|---|---|
| `track_number` | Track number within album |
| `disc` | Disc number |
| `track_total` | Total tracks on disc |
| `disc_total` | Total discs in release |
| `artist` | Track artist |
| `album_artist` | Album-level artist (may differ from track artist) |
| `album` | Album title |
| `release_type` | Release type (e.g. `Album`, `Single`, `EP`) |
| `genre` | Genre string |
| `explicit` | `Explicit` when track has explicit content flag, otherwise empty |
| `isrc` | International Standard Recording Code |
| `upc` | Universal Product Code (album barcode) |
| `label` | Record label name |

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
- Scene movies: `Example.Movie.2024.1080p.EXAMPLE.WEB-DL.DDP5.1.H.264-TAG`
- Scene movies (REPACK): `Example.Movie.2024.REPACK.2160p.EXAMPLE.WEB-DL.DDP5.1.H.265-TAG`
- Scene series: `Example.Show.2024.S01E01.Pilot.1080p.EXAMPLE.WEB-DL.DDP5.1.H.264-TAG`
- Plex movies: `Example Movie (2024) 1080p`

### folder (optional)

Controls the folder name for downloaded content. Uses the same template variables as the file templates above.

If not configured, the default folder naming is used:
- Movies: `Title (Year)`
- Series: Derived from the `series` template with episode-specific variables removed
- Songs: `Artist - Album (Year)`

`folder` accepts either a single string (applies to all title kinds) or a mapping with per-kind
templates keyed by `movies`, `series`, `songs`, and/or `albums`. Unknown keys are warned about and ignored.

Use `/` in a folder template to create nested directories - each segment is sanitized
independently and joined as real path separators. For example `'{source}/Series/{title}.{year?}'`
produces `EXAMPLE/Series/The.Show.2024/`. Note `{source}` is the service **tag** (e.g. `EXAMPLE`,
`EXAMPLE2`), not the display name; it is blank when `--no-source` is used.

Movies only get their own folder when a `movies` folder template is set; without one the movie file
is written directly into the downloads directory (unchanged default behaviour). Series and songs
always get a folder unless `--no-folder` is passed.

Useful music variables for album folders: `album_artist`, `album`, `artist`, `year`, `genre`, `label`, `release_type`, `track_total`, `disc_total`.

```yaml
output_template:
  movies: '{title}.{year}.{repack?}.{edition?}.{quality}.{source}.WEB-DL.{dual?}.{multi?}.{audio_full}.{atmos?}.{hdr?}.{hfr?}.{video}-{tag}'
  series: '{title}.{year?}.{season_episode}.{episode_name?}.{repack?}.{edition?}.{quality}.{source}.WEB-DL.{dual?}.{multi?}.{audio_full}.{atmos?}.{hdr?}.{hfr?}.{video}-{tag}'
  songs: '{track_number}.{title}.{repack?}.{edition?}.{source?}.WEB-DL.{audio_full}.{atmos?}-{tag}'

  # Scene-style folder (single template, applies to all kinds)
  folder: '{title}.{year?}.{repack?}.{edition?}.{lang_tag?}.{quality}.{source}.WEB-DL.{dual?}.{multi?}.{audio_full}.{atmos?}.{hdr?}.{hfr?}.{video}-{tag}'

  # Plex-friendly folder
  # folder: '{title} ({year?})'

  # Per-kind folder templates
  # folder:
  #   movies: '{title} ({year})'
  #   series: '{title} ({year?})'
  #   songs: '{artist} - {album} ({year?})'
  #   albums: '{album_artist} - {album} ({year?})'

  # Nested per-service layout (EXAMPLE/Series/Title.Year/...)
  # folder:
  #   series: '{source}/Series/{title}.{year?}'
  #   movies: '{source}/Movies/{title}.{year?}'
  #   songs:  '{source}/Music/{album_artist}/{album}.{year?}'
```

Example outputs:
- Scene folder: `Example.Show.2024.S01.1080p.EXAMPLE.WEB-DL.DDP5.1.H.264-TAG/`
- Plex folder: `Example Show (2024)/`
- Nested folder: `EXAMPLE/Series/Example.Show.2024/`

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
- Danish audio: `Example.Show.S01E01.DANiSH.1080p.EXAMPLE.WEB-DL.DDP5.1.H.264-TAG`
- English audio + multiple Nordic subs: `Example.Show.S01E01.NORDiC.1080p.EXAMPLE.WEB-DL.DDP5.1.H.264-TAG`
- English audio + Danish subs only: `Example.Show.S01E01.DKsubs.1080p.EXAMPLE.WEB-DL.DDP5.1.H.264-TAG`
- No matching languages: `Example.Show.S01E01.1080p.EXAMPLE.WEB-DL.DDP5.1.H.264-TAG`

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

- `merge_video`
  Merge video **language variants** into one file. Default: `false`
  - `false`: One MKV per video track (the default behaviour).
  - `true`: Group the selected video tracks by `(resolution, range, codec)` and merge
    each group into one MKV, so only language varies within a file. The player switches
    between the language tracks. No re-encode, no concatenation.

  Only the language dimension is collapsed. Different **resolutions**, **ranges**
  (SDR/HDR10/HDR10+/DV/HYBRID) and **codecs** (H264/H265) always stay in separate files.
  For example, `-r HYBRID,DV,HDR10,SDR --merge-video` produces one file per range (never a
  single combined file), while a title offering English + French video of the same
  resolution/range/codec produces one file containing both video tracks.

  Note: The `--merge-video` CLI flag overrides this setting. Can be set per service under
  `services.<TAG>.muxing.merge_video`. Change `group_videos_by_variant` in
  `unshackle/commands/dl.py` to adjust the grouping.

- `default_language` (dict)
  Override which track is flagged as the default in the muxed MKV, regardless
  of the title's original language. Useful when you always want your player to
  open on a specific language (e.g. always default to Polish audio even on
  English originals). Only affects the MKV `--default-track` flag - track
  selection (`-l`, `--alang`, etc.) is unchanged. All keys are optional; each
  track type falls back to its previous default rule when the configured
  language isn't present in the manifest.

  - `audio`: BCP-47 tag (e.g. `pl`, `en`, `pt-BR`). Wins over `is_original_lang`.
    The `--original-flag` continues to mark the true original-audio track.
  - `video`: BCP-47 tag. Wins over the title-language / first-track rule.
  - `subtitle`: BCP-47 tag. Wins over the "forced sub matching audio" rule.

  Languages are matched with the same close-match logic used elsewhere
  (`pt` matches `pt-BR`, etc.). Supports per-service overrides like the rest
  of `muxing`.

  ```yaml
  muxing:
    default_language:
      audio: pl
      video: pl
      subtitle: pl
  ```

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
- `exports` - JSON sidecar exports written when `--export` is used on `dl`.
- `wvds` - Widevine Devices.
- `prds` - PlayReady Devices.
- `dcsl` - Device Certificate Status List.

Notes:

- `services` accepts either a single directory or a list of directories to search for service modules.
  Entries may also be **git repo specs** to load service packs hosted in a repo (see below).

For example,

```yaml
directories:
  downloads: "D:/Downloads/unshackle"
  temp: "D:/Temp/unshackle"
```

There are directories not listed that cannot be modified as they are crucial to the operation of unshackle.

### Loading services from a git repo

A `services` entry may be a git repo instead of a local path, letting you host service packs on
GitHub or any git host (GitLab, Gitea, self-hosted). Local paths and repo specs can be mixed:

```yaml
directories:
  services:
    - https://github.com/you/your-services         # https repo (highest priority - listed first)
    - git@gitlab.com:you/your-services.git         # ssh repo (private, via your git auth)
    - you/your-services@main                        # owner/repo shorthand + optional @branch
    - ~/my-local-services                          # local path (fallback - listed last)
```

How it works:

- On first use the repo is cloned (shallow) to `<your-services-dir>/_repos/<repo-name>/` - the first
  local `services` entry, or the bundled `unshackle/services` if you configured none. **Nothing is
  written to the `cache` directory.**
- After that, unshackle does **not** hit the network on every run. It re-pulls at most once every 24h,
  or immediately when you run `unshackle util refresh-services`.
- Requires `git` on your PATH. Private repos use your existing git credential helper - unshackle
  stores no tokens. Git use is **read-only on the remote** - only `clone`, `fetch`, `pull`, and a
  local `reset` are run; nothing is ever pushed.
- **Local edits and refresh.** You can edit a clone under `_repos/` directly. The two refresh paths
  treat your edits differently:
  - *Automatic* (the 24h TTL during a normal `dl`/`search` run): if the clone has uncommitted
    changes to tracked files or unpushed local commits, unshackle **refuses to refresh and exits**,
    naming the clone, so a background pull never clobbers work in progress. Commit and push it
    upstream (or revert), then it refreshes normally.
  - *Manual* (`unshackle util refresh-services`): an explicit "get upstream's latest" - it
    **hard-resets the clone to upstream, discarding local changes**. Run it only when you want to
    throw away local edits.
  - Untracked files (new service folders, `__pycache__`) never block the automatic path - a
    fast-forward pull doesn't touch them.
- The repo's **top level** must contain `<TAG>/__init__.py` service dirs (same layout as
  `unshackle/services/`).
- **Priority is list order.** The first source to define a tag is the one that loads; if a later
  source (repo or local) has the same tag, that copy is treated as a duplicate and ignored. So list
  the sources you trust most first - e.g. repos first and local last to make local a fallback, or
  local first to let your local tweaks override a repo.
- **What you see on load.** A one-line summary is logged each run, e.g.:

  ```text
  Loaded 103 services (36 duplicate(s) ignored)
  ```

  The full per-duplicate detail is logged only at debug verbosity (`unshackle -d ...`), one line per
  duplicate, naming the path that loaded and the path that was ignored:

  ```text
  EXAMPLE: using <unshackle>/services/EXAMPLE/__init__.py, ignoring duplicate <unshackle>/services/_repos/your-repo/EXAMPLE/__init__.py
  ```

  Paths are shortened to `<unshackle>`/`<venv>`/`~` tokens; set `redact_paths: false` to show full
  absolute paths (see [Debug Logging](DEBUG_LOGGING.md)).

If `<your-services-dir>` is inside the installed package, a reinstall may remove the clones; they are
simply re-cloned on next use. On read-only installs, point `services` at a writable path.

---
