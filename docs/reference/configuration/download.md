# Download & processing { #download-processing }

## `dl`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Persistent defaults for the `dl` command. The block is used as Click's `default_map`, so any
`dl` flag can be given a default here. See [Downloading](../../guide/downloading.md) and the
[CLI Reference](../../guide/cli-reference.md) for what each flag does.

**Key naming.** A key is the flag's long name with dashes replaced by underscores
(`--best-available` → `best_available`, `--sub-format` → `sub_format`). A few keys are named
after the flag's internal destination rather than its visible name; set these exact keys:

| Flag | Config key |
|------|-----------|
| `-r` / `--range` | `range` |
| `--list` | `list` |
| `--tmdb` | `tmdb_id` |
| `--imdb` | `imdb_id` |
| `--animeapi` | `animeapi_id` |
| `-naa` / `--noatmos` | `no_atmos` |
| `-o` / `--output` | `output_dir` |
| `--cdm-only` / `--vaults-only` | `cdm_only` |

**Precedence** (highest first): an explicit CLI flag → a per-service
[`services.<TAG>.dl`](services.md) override → this global `dl:` block → the built-in
default. In other words, config only fills in options you did not set on the command line.

Common keys (a useful subset; every `dl` flag works):

| Key | Type | Default | Sets |
|-----|------|---------|------|
| `lang` | list | `["orig"]` | Video/audio language(s); `orig` = original language. |
| `a_lang` / `v_lang` | list | `[]` | Audio- / video-only language override. |
| `s_lang` | list | `["all"]` | Subtitle language(s). |
| `quality` | list | `[]` (best) | Resolution(s), e.g. `[1080]`. |
| `vcodec` | list | `[]` (any) | Video codec(s), e.g. `[H265]`. |
| `acodec` | list | `[]` (any) | Audio codec(s), e.g. `[EC3]`. |
| `range` | list | `["SDR"]` | Colour range(s): `SDR`, `HLG`, `HDR10`, `HDR10P`, `DV`, `HYBRID`. |
| `channels` | float | *(unset)* | Audio channels, e.g. `6` for 5.1. |
| `sub_format` | str | *(unset)* | Convert subtitles to this format (`srt`, `vtt`, `original`, ...). |
| `forced_subs` | bool | `false` | Include forced subtitle tracks. |
| `forced_s_lang` | list | `[]` | Forced subtitle language(s); implies `forced_subs`. |
| `no_subs` / `no_audio` / `no_chapters` | bool | `false` | Skip that track type. |
| `downloads` | int | `1` | Tracks downloaded concurrently. |
| `workers` | int | *(downloader default)* | Threads per track. |
| `speed_limit` | str | *(unlimited)* | Total download speed cap across all downloads combined, e.g. `500k`, `5M`, `1.5G` or plain bytes/sec. Values are bytes, not bits (`5M` = 5.0 MB/s). |
| `slow` | str | *(unset)* | Inter-title delay, e.g. `"20-40"`. |
| `best_available` | bool | `false` | Fall back to best quality if the request is unavailable. |
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
    to set server-wide download defaults for every request it handles. Keys the API does not know
    are ignored there (`speed_limit`, for example; `serve` has its own `global_speed_limit`).

## `subtitle`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Subtitle handling: SDH stripping, format conversion, and whether subtitles are muxed in or
written as sidecar files. See [Subtitles](../../guide/subtitles.md) for the full guide.

| Sub-key | Type | Default | Description |
|---------|------|---------|-------------|
| `strip_sdh` | bool | `true` | Strip SDH/CC cues (hearing-impaired annotations) into a clean track. |
| `sdh_method` | str | `"auto"` | SDH-stripping backend to use. |
| `preserve_formatting` | bool | `true` | Keep styling/positioning when converting or stripping. |
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

    Nothing is removed, unlike `-sl`, which drops every subtitle language it does not name.

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

```yaml
muxing:
  set_title: true
  default_language:
    audio: en
    subtitle: en
```

## `language_tags`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Language-tag remapping and the rule engine behind the `{lang_tag}` filename variable. When a
`rules` list is present, each rule is evaluated in order and the first match's `tag` is used.
A rule may test:

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
