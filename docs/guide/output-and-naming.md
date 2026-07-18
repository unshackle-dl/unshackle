# Output & Naming

This page explains where unshackle writes finished downloads and how it names them. You control naming with **output templates** (the filename) and **folder templates** (the directory the file lands in), using a set of `{variable}` placeholders that unshackle fills in from the media it just downloaded. It also covers muxing options, release-group tagging, Unicode filenames, and how to relocate unshackle's working directories.

If you have not configured `unshackle.yaml` yet, read [Configuration File](../getting-started/configuration-file.md) first. Everything below lives in that file.

## How an output path is assembled

When a download finishes muxing, unshackle builds the final path from three pieces:

```text
<output directory> / <folder template> / <filename template><extension>
```

- **Output directory**: `directories.downloads` from your config, or whatever you pass to `-o/--output` on the command line for a single run.
- **Folder template**: an optional per-title subfolder. TV episodes and music tracks are **always** placed in a folder; movies only get one if you define a `movies` folder template, or a single-string `folder` template that applies to all title kinds (see [Folder templates](#folder-templates)).
- **Filename template**: the `output_template` for the title kind (movie, series, or song).
- **Extension**: chosen by the muxer: `.mkv` for video, `.mka` for audio-only, `.mks` for subtitle-only.

!!! example "A typical episode path"
    ```text
    /home/user/Downloads/
      └─ Show Name S01 1080p EXAMPLE WEB-DL DDP5.1 H.264/
         └─ Show.Name.S01E01.Pilot.1080p.EXAMPLE.WEB-DL.DDP5.1.H.264-TAG.mkv
    ```
    The folder name comes from the `series` folder template and the file from the `series` output template.

Two command-line flags change this layout for a single run:

| Flag | Effect |
|---|---|
| `-o`, `--output` | Use this directory instead of `directories.downloads`. |
| `--no-folder` | Skip the per-title subfolder; write the file directly into the output directory. |
| `--no-source` | Omit the service source tag from both the filename and folder (the `{source}` variable resolves to empty). |

## Output templates

Filename templates live under the `output_template` key, keyed by title kind. Each value is a format string built from `{variable}` placeholders.

```yaml title="unshackle.yaml"
output_template:
  movies: "{title}.{year}.{quality}.{source}.WEB-DL.{audio_full}.{video}-{tag}"
  series: "{title}.{season_episode}.{episode_name?}.{quality}.{source}.WEB-DL.{audio_full}.{video}-{tag}"
  songs: "{track_number}. {title}"
```

The recognized kinds are:

| Kind | Applies to | Default when unset |
|---|---|---|
| `movies` | Movies | *(required for movie downloads to be named, no built-in default)* |
| `series` | TV episodes | *(required for episode downloads to be named)* |
| `songs` | Music tracks | `"{track_number}. {title}"` |
| `albums` | Album **folders** only (see below) | - |

!!! note
    `movies` and `series` have no hardcoded filename fallback. If you download those title types, define their templates. `songs` falls back to `"{track_number}. {title}"` when omitted.

### Required vs. optional variables

- `{variable}`: **required**. If it is missing from the download's context, formatting raises an error. (Note that most variables are always present in context but resolve to an *empty string* when not applicable, so being "present but empty" is different from "missing".)
- `{variable?}`: **optional**. A trailing `?` marks the variable conditional. When it resolves to empty, unshackle removes the placeholder *and* one adjacent separator (`.`, `-`, or space), so the surrounding text collapses cleanly.

!!! tip "Use `?` for anything that isn't always there"
    Episode names, editions, tags, HDR labels and audio extras aren't present on every title. Mark them optional so you never get doubled dots or dangling separators:

    ```yaml
    series: "{title}.{season_episode}.{episode_name?}.{quality}.{hdr?}.{source}-{tag?}"
    ```
    For an episode with no on-screen name, `{episode_name?}` and its adjacent dot simply disappear.

### Separator style (dots vs. spaces)

unshackle looks at the characters you place *between* variables to decide the filename's separator style. If spaces outnumber dots in your template, it sanitizes with spaces; otherwise it uses dots (the scene-style default). This is also applied to the auto-generated date separators for daily/dated content.

=== "Scene style (dots)"

    ```yaml
    output_template:
      movies: "{title}.{year}.{quality}.{source}.WEB-DL.{audio_full}.{video}-{tag}"
    ```
    ```text
    The.Movie.2024.1080p.EXAMPLE.WEB-DL.DDP5.1.H.264-TAG.mkv
    ```

=== "Readable style (spaces)"

    ```yaml
    output_template:
      movies: "{title} ({year}) {quality} {source} WEB-DL {audio_full} {video}"
    ```
    ```text
    The Movie (2024) 1080p EXAMPLE WEB-DL DDP5.1 H.264.mkv
    ```

## Template variables

Every variable below is valid in both output and folder templates. Values are derived from the downloaded media (via MediaInfo), from the title metadata, or from your config. Variables that don't apply to a given title resolve to an empty string.

### Common / video

| Variable | Meaning | Example |
|---|---|---|
| `title` | Title name (movie/show/song name; `$` is rendered as `S`) | `The Show` |
| `year` | Release year | `2024` |
| `source` | Service tag / class name (empty with `--no-source`) | `EXAMPLE` |
| `quality` | Resolution with scan suffix | `1080p`, `2160p`, `576i` |
| `resolution` | Resolution number only | `1080` |
| `video` | Video codec | `H.264`, `H.265` |
| `hdr` | Dynamic-range label | `HDR`, `HDR10P`, `DV`, `DV.HDR`, `DV.HDR10P`, `HLG` |
| `hfr` | High frame rate marker (frame rate > 30) | `HFR` |
| `edition` | Edition label from the track, if any | `Directors Cut` |
| `tag` | Release-group tag (from `config.tag` or `--tag`) | `TAG` |
| `repack` | `REPACK` when the `--repack` flag is used | `REPACK` |
| `lang_tag` | Result of your `language_tags` rules, if configured | `MULTi` |

!!! note "How `language_tags` rules match"
    Rules are tried in order, but language comparison is **fuzzy**: a rule condition of `en` matches `en-US`, `en-GB`, and any other `en-*` variant. Within a single rule, **all** conditions must match (AND logic) for it to apply. The first rule that fully matches wins; if none match, `{lang_tag?}` is removed from the filename cleanly.

### Episode-specific

| Variable | Meaning | Example |
|---|---|---|
| `season` | Zero-padded season (`S%02d`) | `S01` |
| `episode` | Zero-padded episode (`E%02d`) | `E05` |
| `season_episode` | Combined | `S01E05` |
| `episode_name` | Episode title | `Pilot` |
| `date` | ISO air date for daily/dated content | `2024-06-01` |

!!! note "Daily & sports content"
    When an episode has an air date, unshackle switches to date-based naming automatically: `season` and `season_episode` become the formatted air date, `episode` and `year` are cleared, and `{date}` holds the ISO date. The date's internal separator (dots or spaces) follows your `series` template style.

### Audio

| Variable | Meaning | Example |
|---|---|---|
| `audio` | Audio codec | `DDP`, `DD`, `AAC` |
| `audio_channels` | Channel layout | `5.1`, `2.0` |
| `audio_full` | Codec + channels combined | `DDP5.1` |
| `atmos` | `Atmos` when any track carries JOC | `Atmos` |
| `dual` | `DUAL` for two audio languages (see [`dual_multi_mode`](../reference/configuration.md#dual_multi_mode)) | `DUAL` |
| `multi` | `MULTi` for more than two audio languages | `MULTi` |
| `dubbed` | `DUBBED` for a single audio language that is not the original (strict mode only) | `DUBBED` |

### Music-specific

| Variable | Meaning |
|---|---|
| `track_number` | Zero-padded track number |
| `artist` / `album_artist` | Artist / album artist (falls back to artist) |
| `album` | Album name |
| `disc` | Disc number (only shown when greater than 1) |
| `track_total` / `disc_total` | Totals |
| `release_type` | `album`, `single`, `ep`, etc. |
| `genre` | Genre |
| `explicit` | `Explicit` when flagged |
| `isrc` / `upc` / `label` | ISRC, UPC and record label |

!!! warning "Unknown variables are ignored, not errors"
    If you use a variable that isn't in the list above, unshackle emits a warning at startup but continues. Double-check spelling. A typo'd `{quailty}` will simply be left in the filename literally or dropped, not filled in.

## Folder templates

Folder templates are nested under a special `folder` key inside `output_template`. This key is handled separately from the filename kinds. You can give it a single string (used for all title kinds) or a per-kind map.

=== "Single folder template"

    ```yaml title="unshackle.yaml"
    output_template:
      movies: "{title}.{year}.{quality}.{source}-{tag}"
      series: "{title}.{season_episode}.{quality}.{source}-{tag}"
      folder: "{title}.{year}"
    ```

=== "Per-kind folder templates"

    ```yaml title="unshackle.yaml"
    output_template:
      movies: "{title}.{year}.{quality}.{source}-{tag}"
      series: "{title}.{season_episode}.{quality}.{source}-{tag}"
      folder:
        movies: "{title} ({year})"
        series: "{title} ({year})/Season {season}"
        albums: "{artist} - {album} ({year})"
    ```

The per-kind folder keys are `movies`, `series`, `songs`, and `albums`. Any other key produces a startup warning. Path separators (`/` or `\`) are allowed in folder templates. Each segment is formatted independently, so you can build nested directory structures like `Show/Season 01`.

**Fallback behavior:**

- A per-kind folder template wins if present; otherwise the single `folder` string is used; otherwise unshackle falls back to a built-in default.
- Built-in defaults when no folder template is set: movies get no folder at all unless a `movies` folder template (or a single-string `folder` template) exists; series fall back to a folder *derived* from the `series` output template (stripping `{episode}`, `{episode_name}`, and collapsing `{season_episode}` down to `{season}`); music albums fall back to `{artist} - {album} ({year})`.

!!! note "Movies are flat by default"
    Episodes and songs are always foldered. Movies are written directly into the output directory *unless* you define a `movies` folder template (or a single-string `folder` template, which folders every kind). Set one if you want each movie in its own directory.

## Muxing options

Muxing (combining video, audio, subtitle, chapter and attachment tracks into a single Matroska file with `mkvmerge`) is configured under the `muxing` key.

```yaml title="unshackle.yaml"
muxing:
  set_title: true
  merge_video: false
  merge_audio: true
  default_language:
    audio: en
    subtitle: en
```

| Key | Type | Default | Effect |
|---|---|---|---|
| `set_title` | bool | `true` | Write the title name into the MKV container title with `--title`. Set to `false` to omit it. |
| `merge_video` | bool | `false` | Group video tracks that share the same resolution, range, and codec into one file so only language varies inside it. |
| `merge_audio` | bool | `true` | Merge audio tracks of the same kind so multiple languages sit in one file. |
| `default_language` | map | *(unset)* | Preferred language per track type (`video` / `audio` / `subtitle`). A track in the preferred language is flagged as the default track. |

When no preferred language is matched, unshackle applies sensible defaults: the video default falls back to the title language / original-language track / first track; the audio default is the original-language track; and the subtitle default is a forced track matching the first audio's language.

!!! note "`default_language` only sets the default-track flag"
    `default_language` controls **which track carries the MKV `--default-track` flag**. Nothing else. It does not change track *selection* (that stays with `-l`/`--alang` and friends), and it does not touch which track is marked as original: `--original-flag` still tags the *true* original-audio track. That is the point: you can make your player default to, say, Polish audio on an English-original title without altering the original marker. When the configured language isn't present in the manifest, each track type falls back to its normal default rule described above.

!!! warning "`merge_video` collapses only the language dimension"
    `merge_video` groups video tracks by `(resolution, range, codec)` and merges them so that only **language** differs within a single file, with no re-encode and no concatenation. Different resolutions, ranges (SDR / HDR10 / HDR10+ / DV / HYBRID), and codecs (H.264 / H.265) **always** land in separate files. So `-r HYBRID,DV,HDR10,SDR --merge-video` produces one file *per range* (never a single fused file), whereas English + French video of identical resolution, range, and codec yields one file containing both video tracks. `merge_audio` works the same way for audio languages.

A few muxing behaviors are automatic and not configurable:

- `--no-date` is always passed for privacy (no timestamps embedded).
- Descriptive audio gets the visually-impaired flag; SDH subtitles get the hearing-impaired flag; forced subtitles get the forced flag.
- Chapters are written from the title's chapter list (see [Chapters](#chapters-naming) below).

The `--no-mux` flag skips muxing entirely and writes the individual track files, each with a track-type suffix, into the same output/folder structure.

## Tags & group naming

The release-group tag is the `-TAG` portion at the end of scene-style names, produced by the `{tag}` variable.

```yaml title="unshackle.yaml"
tag: "MYGROUP"
tag_group_name: true
tag_imdb_tmdb: true
```

| Key | Type | Default | Effect |
|---|---|---|---|
| `tag` | str | `""` | The release-group tag used for `{tag}`. |
| `tag_group_name` | bool | `true` | Write the group name (`config.tag`) into the MKV `Group` metadata tag. |
| `tag_imdb_tmdb` | bool | `true` | Look up and embed IMDb / TMDB / TVDB external-ID tags in the MKV metadata (uses `tmdb_api_key` / `simkl_client_id` when available). |

You can override the tag per run without editing config:

```bash
unshackle dl --tag OTHERGROUP EXAMPLE "..."
```

The `--repack` flag adds the `REPACK` marker (surfaced through the `{repack}` variable) to the filename for that run:

```bash
unshackle dl --repack EXAMPLE "..."
```

!!! note "Empty tags collapse cleanly"
    If `config.tag` is empty and your template ends with `-{tag?}`, the trailing `-` and the placeholder are both removed. Use the optional form `{tag?}` if you sometimes download without a tag set.

## Unicode filenames

By default unshackle transliterates non-ASCII characters (Korean, Japanese, Chinese, accented Latin, etc.) into ASCII equivalents, which is safest for older tools, DDL, and P2P sharing. Set `unicode_filenames` to keep the native characters.

```yaml title="unshackle.yaml"
unicode_filenames: true
```

| Value | Result |
|---|---|
| `false` (default) | `기생충` → transliterated ASCII; accents stripped. |
| `true` | Native characters preserved in filenames and folders. |

Regardless of this setting, a set of filesystem-unsafe and structural characters is always removed or replaced during sanitization (for example `/` and `;` become ` & `, and characters like `\ * ! ? , ' " < > | $ # ~` are stripped). This is why you should never put path-unsafe characters directly in a template. unshackle warns about templates containing `< > : " / \ | ? *` at startup (folder templates are checked per path segment, so `/` is allowed there as a directory separator).

## Directory & filename configuration

The `directories` key relocates unshackle's working folders. Only the folders listed here can be moved; core package paths are protected and silently ignored if you try to override them.

```yaml title="unshackle.yaml"
directories:
  downloads: "~/Media/unshackle"
  temp: "/mnt/fast/unshackle-temp"
  cache: "~/.cache/unshackle"
  cookies: "~/.config/unshackle/cookies"
  logs: "~/.config/unshackle/logs"
```

| Name | Purpose |
|---|---|
| `downloads` | Default output directory for finished files. |
| `temp` | Working directory for in-progress downloads, muxing, and intermediate files. |
| `cache` | Title/HTTP cache and update-check state. |
| `cookies` | Per-service cookie files. |
| `logs` | Log files. |
| `exports` | Export JSONs. |
| `services`, `vaults`, `fonts`, `commands` | Search paths for services, key-vault backends, bundled fonts, and CLI commands. |
| `wvds`, `prds`, `dcsl` | Widevine devices, PlayReady devices, and DCSL data. |

Paths support `~` expansion. The names `app_dirs`, `core_dir`, `namespace_dir`, `user_configs`, and `data` are protected and cannot be changed.

### Filename patterns

The `filenames` key overrides the naming patterns for a handful of internal files. These are **not** your media output names. They are logs, temp files, and per-service configs.

```yaml title="unshackle.yaml"
filenames:
  log: "unshackle_{name}_{time}.log"
  chapters: "Chapters_{title}_{random}.txt"
  subtitle: "Subtitle_{id}_{language}.srt"
```

| Name | Default | Where |
|---|---|---|
| `log` | `unshackle_{name}_{time}.log` | `directories.logs` |
| `debug_log` | `unshackle_debug_{service}_{time}.jsonl` | `directories.logs` |
| `config` | `config.yaml` | per-service directory |
| `root_config` | `unshackle.yaml` | main config filename |
| `chapters` | `Chapters_{title}_{random}.txt` | `directories.temp` |
| `subtitle` | `Subtitle_{id}_{language}.srt` | `directories.temp` |

### Chapters naming {#chapters-naming}

When a title has chapters, they are written to an OGM chapters file (`filenames.chapters`) and passed to the muxer. Unnamed chapters use the `chapter_fallback_name` template:

```yaml title="unshackle.yaml"
chapter_fallback_name: "Chapter {i:02}"
```

The fallback supports two placeholders: `{i}` (chapter number, starting at 1) and `{j}` (which increments only for unnamed chapters). Standard format directives like `{i:02}` are allowed.

## Removed & migrated options

!!! warning "`scene_naming` was removed"
    The old `scene_naming` option no longer exists. If it is present in your config, unshackle exits immediately with an error asking you to configure `output_template` instead. Migrate any scene-naming setup to the templates described on this page.

## Worked examples

Each tab shows a template and what it produces for a few different downloads, so you can see
how the optional (`?`) variables appear and disappear.

=== "Movies"

    ```yaml
    output_template:
      movies: "{title}.{year}.{edition?}.{quality}.{source}.WEB-DL.{audio_full}.{atmos?}.{video}.{hdr?}-{tag?}"
      folder:
        movies: "{title} ({year})"
    ```

    A 4K Atmos HDR download fills every placeholder:

    ```text
    The Movie (2024)/
      └─ The.Movie.2024.2160p.EXAMPLE.WEB-DL.DDP5.1.Atmos.H.265.HDR-TAG.mkv
    ```

    The same template on a plain 1080p SDR stereo download, where `{edition?}`, `{atmos?}`
    and `{hdr?}` collapse along with their dots:

    ```text
    The Movie (2024)/
      └─ The.Movie.2024.1080p.EXAMPLE.WEB-DL.AAC2.0.H.264-TAG.mkv
    ```

    A director's cut fills `{edition?}`:

    ```text
    The Movie (2024)/
      └─ The.Movie.2024.Directors.Cut.2160p.EXAMPLE.WEB-DL.DDP5.1.Atmos.H.265.DV.HDR-TAG.mkv
    ```

=== "TV series"

    ```yaml
    output_template:
      series: "{title}.{season_episode}.{episode_name?}.{quality}.{source}.WEB-DL.{audio_full}.{video}-{tag}"
      folder:
        series: "{title} ({year})/Season {season}"
    ```
    ```text
    The Show (2023)/Season.S02/
      └─ The.Show.S02E04.The.Reckoning.2160p.EXAMPLE.WEB-DL.DDP5.1.H.265-TAG.mkv
    ```

    Each folder segment picks its separator style independently: `{title} ({year})` has
    spaces around its variables, but `Season {season}` alone counts as dot style, so its
    space becomes a dot.

    When an episode has no on-screen name, `{episode_name?}` disappears cleanly:

    ```text
    The Show (2023)/Season.S02/
      └─ The.Show.S02E05.2160p.EXAMPLE.WEB-DL.DDP5.1.H.265-TAG.mkv
    ```

=== "Multi-language"

    Place `{dual?}`, `{multi?}` and `{dubbed?}` next to each other. At most one of them is
    ever set (see [`dual_multi_mode`](../reference/configuration.md#dual_multi_mode)), and the
    empty ones collapse:

    ```yaml
    output_template:
      movies: "{title}.{year}.{quality}.{source}.WEB-DL.{dual?}.{multi?}.{dubbed?}.{audio_full}.{video}-{tag}"
    ```

    Original English audio plus a French dub (`-l en,fr`):

    ```text
    The.Movie.2024.1080p.EXAMPLE.WEB-DL.DUAL.DDP5.1.H.264-TAG.mkv
    ```

    Original plus several dubs (`-l en,fr,de,es`):

    ```text
    The.Movie.2024.1080p.EXAMPLE.WEB-DL.MULTi.DDP5.1.H.264-TAG.mkv
    ```

    Only a dub, original audio left out (`-l de` on an English-original title):

    ```text
    The.Movie.2024.1080p.EXAMPLE.WEB-DL.DUBBED.DDP5.1.H.264-TAG.mkv
    ```

    Two dialects of one language (`en-US` + `en-GB`) count as a single language, so none of the
    three variables is set:

    ```text
    The.Movie.2024.1080p.EXAMPLE.WEB-DL.DDP5.1.H.264-TAG.mkv
    ```

    For finer control (e.g. a `SUBBED` tag driven by subtitle languages), use `{lang_tag?}`
    with [`language_tags` rules](../reference/configuration.md#language_tags) instead.

=== "Daily shows"

    Dated content needs no special template. When an episode carries an air date,
    `{season_episode}` becomes the date automatically and `{episode}` and `{year}` clear:

    ```yaml
    output_template:
      series: "{title}.{season_episode}.{episode_name?}.{quality}.{source}.WEB-DL.{audio_full}.{video}-{tag}"
    ```

    A normal episode and a dated one, same template:

    ```text
    The.Daily.Show.S28E101.1080p.EXAMPLE.WEB-DL.DDP5.1.H.264-TAG.mkv
    The.Daily.Show.2024.06.01.1080p.EXAMPLE.WEB-DL.DDP5.1.H.264-TAG.mkv
    ```

    The date separator follows your template style: dots here, spaces if your `series`
    template uses spaces.

=== "Music"

    ```yaml
    output_template:
      songs: "{track_number}. {title}"
      folder:
        albums: "{album_artist} - {album} ({year})"
    ```
    ```text
    The Artist - The Album (2024)/
      └─ 01.Opening.Track.mka
    ```

    In `"{track_number}. {title}"` dots and spaces are tied, and ties go to dot style; add
    more spaces between variables (as below) if you want space-separated names.

    A multi-disc album with explicit tracks, using more of the music variables. `{disc}` is
    empty on disc 1, so `{disc?}` and its separator only appear from disc 2 onward:

    ```yaml
    output_template:
      songs: "{disc?}-{track_number}. {title} {explicit?}"
      folder:
        albums: "{album_artist} - {album} ({year}) [{label?}]"
    ```
    ```text
    The Artist - The Album (2024) [The Label]/
      ├─ 01. Opening Track.mka
      └─ 02-03. Closing Track Explicit.mka
    ```

## See also

- [Configuration File](../getting-started/configuration-file.md): the full `unshackle.yaml` structure.
- [Downloading](downloading.md): the `dl` command and per-run flags like `-o`, `--no-folder`, `--tag`, and `--repack`.
