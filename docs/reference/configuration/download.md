# Download & processing { #download-processing }

## `dl`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Persistent defaults for the `dl` command. unshackle uses the block as Click's `default_map`,
so you can give any `dl` flag a default here. See [Downloading](../../guide/downloading.md) and the
[CLI Reference](../../guide/cli-reference.md) for what each flag does.

**Config key naming.** A config key is the flag's long name with dashes replaced by
underscores (`--best-available` → `best_available`, `--sub-format` → `sub_format`). A few
config keys use the flag's internal destination and not its visible name. Set these exact
config keys:

| Flag | Config key |
|------|-----------|
| `-r` / `--range` | `range` |
| `--list` | `list` |
| `--tmdb` | `tmdb_id` |
| `--imdb` | `imdb_id` |
| `--anilist` | `anilist_id` |
| `-naa` / `--noatmos` | `no_atmos` |
| `-o` / `--output` | `output_dir` |
| `--cdm-only` / `--vaults-only` | `cdm_only` |

**Precedence** (highest first): an explicit CLI flag → a per-service
[`services.<TAG>.dl`](services.md) override → this global `dl:` block → the built-in
default. In other words, config only fills in options you did not set on the command line.

Common config keys. This is a useful subset, and every `dl` flag works:

| Key | Type | Default | Sets |
|-----|------|---------|------|
| `lang` | list | `["orig"]` | Video/audio language(s); `orig` = original language. A `-` prefix excludes, e.g. `[all, -es]`. |
| `a_lang` / `v_lang` | list | `[]` | Audio- / video-only language override. A `-` prefix excludes. |
| `s_lang` | list | `["all"]` | Subtitle language(s). A `-` prefix excludes, e.g. `[all, -es]`. |
| `quality` | list | `[]` (best) | Resolution(s), e.g. `[1080]`. |
| `vcodec` | list | `[]` (any) | Video codec(s), e.g. `[H265]`. |
| `acodec` | list | `[]` (any) | Audio codec(s), e.g. `[EC3]`. |
| `range` | list | `["SDR"]` | Colour range(s): `SDR`, `HLG`, `HDR10`, `HDR10P`, `DV`, `HYBRID`. |
| `channels` | float | *(unset)* | Audio channels, e.g. `6` for 5.1. |
| `sub_format` | str | *(unset)* | Convert subtitles to this format (`srt`, `vtt`, `original`, ...). |
| `forced_subs` | bool | `false` | Include forced subtitle tracks. |
| `forced_s_lang` | list | `[]` | Forced subtitle language(s); implies `forced_subs`. A `-` prefix excludes. |
| `no_subs` / `no_audio` / `no_chapters` / `no_attachments` | bool | `false` | Skip that track type. `no_attachments` also skips subtitle font attaching. |
| `downloads` | int | `1` | Tracks downloaded concurrently. |
| `workers` | int | *(downloader default)* | Threads per track. |
| `speed_limit` | str | *(unlimited)* | Total download speed cap across all downloads combined, e.g. `500k`, `5M`, `1.5G` or plain bytes/sec. Values are bytes, not bits (`5M` = 5.0 MB/s). |
| `slow` | str | *(unset)* | Inter-title delay, e.g. `"20-40"`. |
| `best_available` | bool | `false` | Warn instead of failing when a requested resolution, range, or language is missing. The CLI flag is `--best-available`, also spelled `--warn-only`. |
| `proxy` | str | *(unset)* | Default proxy URI or 2-letter country. |
| `no_folder` | bool | `false` | Do not create a per-title folder. |

```yaml title="Global download defaults"
dl:
  lang: [en]
  quality: [1080]
  range: [HDR10]
  vcodec: [H265]
  sub_format: srt
  downloads: 2
```

!!! note "Language exclusions in YAML"
    The language keys accept the same `-` exclusion tokens as the command line. Give them
    as a list or as one comma-separated string.

    ```yaml
    dl:
      s_lang: [all, -es]
      a_lang: "orig,-es"
    ```

    See [Excluding languages](../../guide/downloading.md#excluding-languages) for the rules.

!!! note "Per-service overrides"
    A `dl` block nested under a service in [`services`](services.md) takes the same keys and
    applies to that service only.

    ```yaml
    dl:
      lang: [en]
    services:
      EXAMPLE1:
        dl:
          lang: [en, ja]
          range: [DV]
    ```

!!! note "`serve` uses these same keys"
    The [`serve`](services.md#serve) block accepts most of the same `dl` option keys at its top level
    to set server-wide download defaults for every request it handles. The API ignores the
    config keys it does not know there, `speed_limit` for example. `serve` has its own
    `global_speed_limit`.

## `continue_downloads`

- **Type:** `bool` &nbsp;·&nbsp; **Default:** `false`

Keep a failed download's completed segment files so the next download resumes from where it
stopped instead of restarting from byte zero.

unshackle reuses only whole, fully downloaded segments. It always discards partially written
segments, and no partial-byte (`Range`) resume across runs takes place. Reuse is possible only
with proof that the segmentation did not change. The manifest URL, segment count, and byte
ranges must all match the previous download, or the download silently restarts clean.

!!! note "What can and cannot resume"
    - Segmented DASH, ISM, and HLS tracks resume.
    - Single-file (non-segmented) downloads and DASH tracks collapsed to a single ranged
      download never make segment files, so this option does not apply to them (the
      downloader has its own in-flight partial-file handling for those).
    - HLS resumes Widevine/PlayReady and unencrypted titles only. An AES-128 title always
      restarts. An HLS failure during the post-download merge step also restarts.

For a one-off resume without changing config, pass `dl --continue-downloads` on the
command line (the flag can only enable resume, never disable it).

```yaml title="Resume failed downloads"
continue_downloads: true
```

## `subtitle`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Subtitle handling: SDH stripping, format conversion, and whether unshackle muxes the subtitles
in or writes them as sidecar files. See [Subtitles](../../guide/subtitles.md) for the full guide.

| Sub-key | Type | Default | Description |
|---------|------|---------|-------------|
| `strip_sdh` | bool | `true` | Strip SDH/CC cues (hearing-impaired annotations) into a clean track. |
| `sdh_method` | str | `"auto"` | SDH-stripping backend to use. |
| `preserve_formatting` | bool | `true` | Pass WebVTT cue text through as written. `false` re-encodes it through pycaption, which merges identical cues but flattens styling. unshackle removes WebVTT-only cue markup either way. |
| `convert_before_strip` | bool | `true` | Convert to the working format before stripping SDH. |
| `conversion_method` | str | `"auto"` | Subtitle conversion backend. |
| `output_mode` | str | `"mux"` | `mux` embeds subtitles in the MKV; `sidecar` writes separate files; `both` writes sidecars and still muxes. |
| `sidecar_format` | str | `"srt"` | Format for sidecar files when `output_mode` is `sidecar` or `both`. |
| `type_priority` | list | *(unset)* | Ordered ranking of subtitle types (`forced`, `normal`, `sdh`; CC counts as SDH). Types you leave out fall to the end. |
| `group_by` | str | `"type"` | Which key groups the subtitle tracks. `type` puts all forced tracks together, then all normal, then all SDH. `language` keeps each language next to its own variants. |
| `language_priority` | list | *(unset)* | Languages to sort to the top, in the order you give. The rest keep the default order: original language first, then alphabetical. Nothing is removed. |

```yaml
subtitle:
  strip_sdh: true
  output_mode: sidecar
  sidecar_format: srt
```

!!! note "`group_by` track order"
    | `group_by` | Result |
    |---|---|
    | `type` | Danish, Finnish, French, … Danish SDH, Finnish SDH, French SDH |
    | `language` | Danish, Danish SDH, Finnish, Finnish SDH, French, French SDH |

    `type_priority` still sets the order of the variants, so with `group_by: language` it
    controls the order inside each language.

!!! note "`language_priority` sorts languages, it does not select them"
    ```yaml
    subtitle:
      group_by: language
      language_priority: [en, es, fr]
    ```

    | | Result for an English title |
    |---|---|
    | Default | English, Arabic, Bulgarian, Spanish, French, Japanese |
    | With the list above | English, Spanish, French, Arabic, Bulgarian, Japanese |

    unshackle removes nothing, unlike `-sl`, which drops every subtitle language it does not name.

    Use `orig` in the list for the title's original language. A base tag also covers its
    regional forms, so `en` sorts `en-US` and `en-GB`, but `zh` does **not** cover
    `zh-Hant`, which you must name. The sort ignores entries that match no track, and it
    ignores entries that are not valid language tags.

    `--exact-lang` applies here too: with it, `en` sorts only `en`, and `en-US` falls to
    the alphabetical tail with the rest. Name each tag you want when you use that flag.

!!! note "`type_priority` picks the default subtitle only with `muxing.default_language`"
    At mux time the `default` flag goes to every forced subtitle whose language matches the
    first audio track, so a full dialogue track never becomes default on its own.
    `type_priority` alone does not change that. Name a subtitle language in
    [`muxing.default_language`](#muxing) and the first track in that language gets the flag
    instead, which is where `type_priority` decides which track that is:

    ```yaml
    muxing:
      default_language:
        subtitle: en
    subtitle:
      type_priority: [normal, sdh, forced]
    ```

## `audio`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Audio handling options. The most notable sub-key is **`codec_priority`**, an ordered list of
audio codecs used to break ties in track selection. **`language_priority`** is an ordered list
of languages that sort to the top of the audio tracks.

```yaml
audio:
  codec_priority:
    - EAC3
    - AC3
  language_priority:
    - orig
    - en
```

!!! note "`codec_priority` is a soft priority: nothing is dropped"
    `codec_priority` outranks bitrate: any listed codec sorts above every unlisted one, and among
    listed codecs the list order decides. Bitrate order survives only within a single codec rank.
    Nothing is ever removed. Two higher-level rules still override it: **Atmos
    tracks always take precedence over `codec_priority`**, and **audio-description tracks are
    always forced to the end** regardless of it. When `codec_priority` is unset, tracks sort by
    bitrate alone, with those same Atmos/descriptive rules still applied.

## `muxing`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Matroska (MKV) muxing options.

| Sub-key | Type | Default | Description |
|---------|------|---------|-------------|
| `set_title` | bool | `true` | Write a human-readable title into the MKV container. |
| `default_language` | dict | `{}` | Force which language is flagged *default* per track type, e.g. `{audio: en, subtitle: en}`. |
| `merge_audio` | bool | `true` | Merge all audio into one file. `--split-audio` on the CLI flips this off. |
| `merge_video` | bool | `false` | Merge video tracks that share height, range, and codec into one file, so only language varies inside a file. `--merge-video` on the CLI flips this on. |
| `concurrency` | int | `4` | How many mkvmerge processes run at the same time when a title muxes more than one output file (one per video variant or audio codec). Each process uses one CPU core, so this shortens the mux step on an SSD. Set it to `1` on a hard disk, where parallel writes are slower. |

```yaml
muxing:
  set_title: true
  concurrency: 4
  default_language:
    audio: en
    subtitle: en
```

## `language_tags`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Language-tag remapping and the rule engine behind the `{lang_tag}` filename variable. When a
`rules` list is present, unshackle evaluates each rule in order and uses the first match's
`tag`. A rule can examine:

| Condition | Meaning |
|-----------|---------|
| `audio` | Matches against the audio track languages. |
| `subs_contain` | At least one subtitle language matches. |
| `subs_contain_all` | Every listed language must be present (scalar or list). |

```yaml
language_tags:
  rules:
    - audio: [ja]
      subs_contain: [en]
      tag: "SUBBED"
```

## `dual_multi_mode`

- **Type:** `str` &nbsp;·&nbsp; **Default:** `strict`

Rule set for the `{dual}`, `{multi}`, and `{dubbed}` filename variables.

=== "`strict` (default)"

    | Variable | Set when |
    |----------|----------|
    | `{dual}` | Exactly two audio languages **and one is the title's original language**. |
    | `{multi}` | Three or more audio languages, even without the original. |
    | `{dubbed}` | A single audio language that is not the title's original language (requires a known original). |

=== "`count` (legacy)"

    | Variable | Set when |
    |----------|----------|
    | `{dual}` | Exactly two audio languages (original language ignored). |
    | `{multi}` | Three or more audio languages. |
    | `{dubbed}` | Never set. |

```yaml
dual_multi_mode: strict
```

!!! note "Dialects count as one language"
    Regional variants of the same base language (e.g. `en-US` and `en-GB`) collapse to a single
    language in both modes, so they never trigger `{dual}` on their own.
