# Output & naming { #output-naming }

Keys that control the output filename, the per-title subfolder, release-group tagging, and
filename character handling. For template cookbook material and worked naming examples, see
the [Output & naming guide](../../guide/output-and-naming.md).

## `output_template`

- **Type:** `dict` &nbsp;·&nbsp; **Required** (no usable default; `dl` exits with an error if this is missing)

Filename templates keyed by media kind, using `{variable}` placeholders. Recognised kinds are
`movies`, `series`, and `songs` (and `albums` for folder templates). A trailing `?` on a
variable (e.g. `{quality?}`) marks it optional: if empty, it and one adjacent separator are
dropped.

```yaml title="unshackle.yaml"
output_template:
  movies: "{title}.{year}.{quality?}.{source}-{tag}"
  series: "{title}.{season_episode}.{episode_name?}.{quality?}.{source}-{tag}"
  songs: "{track_number}. {title}"
  folder:                       # special nested key, see below
    movies: "{title} ({year})"
    series: "{title} ({year})"
```

**The nested `folder` sub-key** controls output subfolders, and unshackle pops it out of
`output_template`:

- A **dict** sets per-kind folder templates (kinds: `movies`, `series`, `songs`, `albums`).
- A **str** sets a single folder template for everything.

unshackle validates templates on load and emits warnings for unknown variables, non-string
values, filesystem-unsafe characters (`< > : " / \ | ? *`), empty templates, and unknown
folder-kind keys.

??? example "All valid template variables"
    `title`, `year`, `season`, `episode`, `season_episode`, `episode_name`, `part`,
    `absolute`, `date`, `quality`, `resolution`, `source`, `tag`, `track_number`, `artist`,
    `album_artist`, `album`, `disc`, `track_total`, `disc_total`, `release_type`, `genre`,
    `explicit`, `isrc`, `upc`, `label`, `audio`, `audio_channels`, `audio_full`, `atmos`,
    `dual`, `multi`, `dubbed`, `video`, `hdr`, `hfr`, `edition`, `repack`, `lang_tag`,
    `title_type`.

!!! warning "`{part?}` is already inside `{season_episode}`"
    `part` holds the bare part index of a
    [split episode](../../guide/output-and-naming.md#split-episodes) (`2`) and is empty on
    every other title. unshackle folds it into `{episode}` and `{season_episode}`, so the
    shipped `series` template names split episodes correctly with no edit. Putting
    `{part?}` next to either of them renders the part twice (`S01E01.Part.2.2`). Use the
    standalone variable only in a template that names neither `{episode}` nor
    `{season_episode}`, and note that it is always empty in a folder template, since
    folders do not carry the part.

!!! tip "`{absolute}` counts episodes across seasons"
    `absolute` holds the absolute episode number, zero-padded to 3 digits (`007`), and is
    empty on titles that have none. You add it to the name yourself, and it never replaces `{season}`
    or `{episode}`. Any series with a TVDB absolute order can use it, anime most often.
    Services can supply it, and
    [`--enrich`](../../guide/downloading.md#metadata-and-tagging) fills it in from TVDB.
    Use `{absolute?}` so the name stays clean when it is unknown.

## Tagging & naming keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `tag` | str | `""` | Release-group tag. Fills the `{tag}` template variable and is written into MKV/audio tags. |
| `tag_group_name` | bool | `true` | Include the group name (`tag`) in the MKV `Group` tag. |
| `tag_imdb_tmdb` | bool | `true` | Write IMDb/TMDB/TVDB external-ID tags into the MKV (needs metadata providers). |
| `chapter_fallback_name` | str | `""` | Fallback template for chapter names, e.g. `"Chapter {i:02}"`. |
| `unicode_filenames` | bool | `false` | Allow Unicode in filenames; when `false`, names are ASCII-sanitised. |

## `tag_rules`

- **Type:** `list` &nbsp;·&nbsp; **Default:** `[]`

Conditionally replaces [`tag`](#tagging-naming-keys) for a given download. Each rule pairs a `when`
dict of conditions with the `tag` to use when they hold. unshackle evaluates the rules in
order and the first match wins. If no rule matches, the global `tag` stays.

```yaml title="unshackle.yaml"
tag_rules:
  - when: { title_type: movie, lang_tag: SUBBED }
    tag: SUBMOVIES
  - when: { quality: 2160p, hdr: [DV, DV.HDR, DV.HDR10P, HDR, HDR10P] }
    tag: UHDGROUP
```

Rule semantics:

- Every condition in `when` must hold (they are AND-ed).
- A value can be a scalar or a list. A list matches if **any** entry matches.
- Comparison is case-insensitive string equality.
- The keys of `when` are the filename template variables for the release itself:
  `title_type`, `quality`, `resolution`, `source`, `video`, `hdr`, `hfr`, `audio`,
  `audio_channels`, `audio_full`, `atmos`, `dual`, `multi`, `dubbed`, `edition`, `repack`,
  and `lang_tag`. Only these are available. unshackle adds the title's own fields (`title`,
  `year`, `season`, `episode`, and so on) after it evaluates the rules, so a condition cannot
  use them.
- An unknown key in `when` is a mistake: unshackle logs a warning that names the key
  and skips that rule. unshackle also warns about a rule without conditions or without a `tag`, and skips it.

Put the more specific rules first, because unshackle stops at the first match.

!!! warning "Match `hdr` on the exact value"
    Matching is exact, and `hdr` is a composite label for Dolby Vision titles: a DV title with
    an HDR10 base layer gives `DV.HDR`, and with an HDR10+ base layer `DV.HDR10P`. Plain `DV`
    does **not** match those. Name every variant you want, as the example above does. Note
    also that HDR10 gives `HDR`, not `HDR10`. The full set of values is `HDR`, `HDR10P`, `DV`,
    `DV.HDR`, `DV.HDR10P`, and `HLG`, as listed in the
    [variable table](../../guide/output-and-naming.md#template-variables).

### Operators

A condition value can start with a comparison operator, which unshackle applies to the rest of
the value:

```yaml title="unshackle.yaml"
tag_rules:
  - when: { resolution: "<1080" }
    tag: SDGROUP
  - when: { quality: ">=2160" }
    tag: UHDGROUP
  - when: { video: "!=H.265" }
    tag: AVCGROUP
```

- `>`, `<`, `>=`, `<=` compare numbers. unshackle reads the first number out of the actual
  value, so `">=2160"` matches a quality of `2160p`. If the actual value has no number, the
  condition fails.
- `=`, `==`, and `!=` compare text, case-insensitively, when the operand is not a number.
  With a numeric operand they compare numbers like the operators above. A value with no number
  in it is never equal to a number, so `"!=2160"` holds for it and `"==2160"` does not.
- An ordering operator with a text operand (for example `">DV"`) is a mistake: unshackle logs
  a warning and the condition fails. So does an operator with nothing after it (`">="`).
- A value with no operator keeps the plain equality behaviour described above.

!!! warning "Quote operator values in YAML"
    Two of these operators break an unquoted YAML value, and both stop the config from
    loading:

    - `>` opens a folded block scalar, so `quality: >=2160` fails with a "while scanning a
      block scalar" error.
    - `!` starts a tag, so `hdr: !=DV` fails with a "could not determine a constructor"
      error.

    Quote every operator value: `quality: ">=2160"`, `resolution: "<1080"`, `video: "!=H.265"`.

!!! note "Keep a negation out of lists"
    A list matches if any entry matches, so two or more `!=` entries in one list are always
    true. Give a negation as a single value, not as a list entry.

`title_type` is the media kind of the title: `movie`, `series`, or `music`. It is useful to
give each kind its own group:

```yaml
tag_rules:
  - when: { title_type: movie }
    tag: MOVIEGRP
  - when: { title_type: series }
    tag: TVGRP
```

The resolved tag applies to the `{tag}` filename variable only. The `Group` metadata tag
written into MKV and music files always uses [`tag`](#tagging-naming-keys).

!!! note "`--tag` wins"
    An explicit `dl --tag` on the command line disables the rules completely and uses the
    given tag.
