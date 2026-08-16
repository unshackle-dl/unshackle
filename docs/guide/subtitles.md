# Subtitles

Subtitles are first-class tracks in unshackle. When you download a title, subtitle
tracks are parsed from the manifest alongside video and audio, downloaded, cleaned up,
optionally converted to the format you want, and then either muxed into the final
Matroska file or written out as sidecar files next to it. The sections below cover the
supported formats, how tracks are normalized after download, how to select and filter
subtitles, and every subtitle-related configuration option.

Most of this is controlled with a handful of `dl` flags and a small `subtitle` block in
your [configuration file](../getting-started/configuration-file.md). See
[Downloading](downloading.md) for how subtitle flags fit into a full download command.

## Supported formats

unshackle understands the following subtitle codecs. The **Format** column is the value
shown in track listings; the **Extension** column is the file extension used on disk.

| Codec (internal name) | Format | Extension | Notes |
| --- | --- | --- | --- |
| `SubRip` | SRT | `srt` | SubRip, the most widely compatible plain-text format |
| `SubStationAlpha` | SSA | `ssa` | SubStation Alpha (styled) |
| `SubStationAlphav4` | ASS | `ass` | Advanced SubStation Alpha (styled) |
| `TimedTextMarkupLang` | TTML | `ttml` | Timed Text Markup Language (IMSC/DFXP) |
| `WebVTT` | VTT | `vtt` | WebVTT |
| `SAMI` | SMI | `smi` | Synchronized Accessible Media Interchange |
| `MicroDVD` | SUB | `sub` | MicroDVD |
| `MPL2` | MPL2 | `mpl2` | MPL2 |
| `TMP` | TMP | `tmp` | TMP |
| `fTTML` | STPP | `stpp` | MPEG-DASH box-encapsulated TTML (IMSC1) |
| `fVTT` | WVTT | `wvtt` | MPEG-DASH box-encapsulated WebVTT |

!!! note "The two boxed formats convert automatically"
    `fTTML` (STPP) and `fVTT` (WVTT) are the fragmented, MP4-box-encapsulated forms that
    DASH and HLS deliver. You never keep these as-is: right after download they are
    unwrapped into their plain counterparts (`fTTML` becomes TTML and `fVTT` becomes
    WebVTT), so you always end up with an editable text subtitle. Note that this is a
    one-way, source-only relationship: you can convert *from* these boxed formats, but no
    backend can produce them as an output target, so they can never be a conversion
    destination.

## What happens to a subtitle after download

A subtitle download does more than save a file. Once the bytes are on disk, unshackle runs
format-appropriate clean-up to produce a single, valid file:

- **Encoding**: non-boxed subtitles are forced to UTF-8, and stray `&lrm;` / `&rlm;`
  bidirectional entities are unescaped.
- **Segment merging**: when a WebVTT or boxed subtitle arrives as many DASH/HLS
  segments, the segments are stitched back into one continuous document with correct
  timing.
- **WebVTT sanitization**: merged WebVTT runs through a repair pipeline that clamps
  negative timestamps to `00:00:00.000`, removes stray cue identifiers (like `Q0`, `Q1`)
  that confuse downstream parsers, and merges multi-line cues that a service split into
  separate overlapping cues. By default the original styling and layout are preserved
  (see [`preserve_formatting`](#subtitle-configuration)).
- **TTML for muxing**: Matroska cannot carry TTML. If you do not request a specific
  output format, any plain TTML track is automatically converted to WebVTT before
  muxing (the closest lossless equivalent). Boxed `fTTML` is likewise unwrapped to TTML
  first.

You normally do not need to think about any of this. It just produces a clean file.

## Selecting and filtering subtitles

Subtitle selection happens through `dl` flags. The most important is language selection.

| Flag | Purpose |
| --- | --- |
| `-sl`, `--s-lang` | Language(s) wanted for subtitles. Defaults to `all`. A `-` prefix excludes, e.g. `all,-es`. |
| `--require-subs` | Require these languages to exist; if present, download **all** subtitles. Cannot be combined with `--s-lang`. |
| `-fs`, `--forced-subs` | Include forced subtitle tracks (excluded by default). |
| `-fsl`, `--forced-s-lang` | Language(s) wanted for forced subtitles; implies `-fs`. A `-` prefix excludes. |
| `--exact-lang` | Exact language matching, with no regional variants. |
| `-S`, `--subs-only` | Download only subtitle tracks. |
| `-ns`, `--no-subs` | Do not download subtitle tracks at all. |
| `--sub-format` | Output subtitle format / conversion target (see [conversion](#converting-subtitle-formats)). |
| `--skip-subtitle-errors` | Skip a subtitle that fails to download instead of aborting the whole title. |

### Choosing languages

By default (`--s-lang all`) unshackle keeps every subtitle language the service offers.
To narrow it down, pass a comma-separated list of language tags:

```shell title="English and Spanish subtitles only"
unshackle dl --s-lang en,es EXAMPLE 81234567
```

Language matching is fuzzy by default: `en` will match `en-US`, `en-GB`, and similar
variants. If a requested language is missing but others were found, unshackle continues
with whatever remains and tells you which languages it dropped.

For strict matching, where `es-419` matches only `es-419` and never `es-ES`, add
`--exact-lang`:

```shell title="Only Latin-American Spanish, no other Spanish variants"
unshackle dl --s-lang es-419 --exact-lang EXAMPLE 'https://www.example.com/...'
```

### Requiring languages before downloading

`--require-subs` is a gate rather than a filter. It downloads **all** available
subtitles, but only if every language you list is present; if any is missing the title
errors out. This is useful for batch jobs where a title without your must-have language
should fail loudly rather than silently produce a partial result.

```shell title="Fail unless both English and French subs exist, then grab everything"
unshackle dl --require-subs en,fr EXAMPLE B0ABCDEFGH
```

!!! warning "`--require-subs` and `--s-lang` are mutually exclusive"
    Because `--require-subs` forces downloading of all subtitle languages, it cannot be
    combined with `--s-lang`. Choose one or the other.

### Forced subtitles

Forced tracks carry only the important on-screen signs and foreign-dialogue lines meant
to display even when you are watching in your own language. They are **excluded by
default**. Add `-fs` / `--forced-subs` to include them:

```shell title="Include forced tracks alongside full subtitles"
unshackle dl --s-lang en --forced-subs EXAMPLE 81234567
```

`-fs` keeps forced tracks in every selected language. If you only want forced tracks
in certain languages, use `-fsl` / `--forced-s-lang` instead. It implies `-fs`, so
you do not need both:

```shell title="All full subtitles, but only the English forced track"
unshackle dl --s-lang all --forced-s-lang en EXAMPLE 81234567
```

If you pass both flags, `-fsl` wins and keeps only forced tracks in its languages.
It also understands `orig` (the title's original language) and `all` (every forced
track, same as plain `-fs`).

## Track types: forced, SDH, and CC

Every subtitle track carries flags describing what kind of captions it holds. These
flags drive selection, sort order, output filenames, and the Matroska track flags.

- **Forced**: only signs and foreign dialogue; meant to play when the audio language
  matches. Off by default; enable with `--forced-subs`.
- **SDH** (Subtitles for the Deaf and Hard-of-Hearing; also called HOH): a full
  transcript that includes both dialogue and non-speech sound cues.
- **CC** (Closed Captions): captions originating from an EIA-608/708 broadcast stream,
  often uppercase with `>>>` speaker markers and sound cues.

!!! note "SDH and CC are treated together for sorting and stripping"
    unshackle orders subtitles from fewest to most captions: **Forced → Normal → SDH/CC**.
    For the purposes of hearing-impaired handling, SDH and CC tracks are grouped as the
    "most captions" variant.

    By default this type order is the primary sort, so all forced tracks come first, then
    all normal tracks, then all SDH/CC tracks, each block sorted by language. To keep each
    language next to its own variants instead, set
    [`subtitle.group_by`](../reference/configuration/download.md#subtitle) to `language`. Use
    [`subtitle.type_priority`](../reference/configuration/download.md#subtitle) to change the
    Forced → Normal → SDH/CC order itself.

A single track cannot be both CC and SDH, and a forced track cannot also be flagged
CC or SDH. These combinations are rejected as invalid.

### Closed captions embedded in video

Some services do not ship subtitles as separate tracks but instead embed EIA-608/708
captions inside the video stream. After a video track is downloaded and decrypted,
unshackle probes it for these captions and, when found, extracts them into a SubRip
(`SRT`) subtitle flagged as CC. This runs automatically unless you disabled subtitles
(`--no-subs`) or video (`--no-video` / `--video-only` semantics), and it requires the
CCExtractor tool on your `PATH`.

## Automatic SDH stripping

When a language has an SDH track but **no** plain (non-SDH, non-forced) track, unshackle
can generate a clean non-SDH version for you by stripping the hearing-impaired cues:
sound effects, speaker labels, music notes, and similar. This is on by default and
controlled by the [`strip_sdh`](#subtitle-configuration) config key.

The check is deliberately loose: if a close-enough plain track already exists (for
example an `en-GB` non-SDH track when the SDH track is `en-US`), no stripped copy is
created. When a stripped copy *is* created, the original SDH track is kept as well, so
you end up with both.

The stripping engine itself is chosen by the [`sdh_method`](#subtitle-configuration)
config key. The default (`auto`) prefers the `subby` cleaner for SRT, falls back to
SubtitleEdit when it is installed, and otherwise uses the built-in `filter-subs`
approach.

!!! note "Why `convert_before_strip` exists"
    `subby`'s SDH stripper only operates on SRT. Handed any other codec it returns
    silently without stripping anything, not an error but a no-op that leaves the SDH
    cues in place. This is the whole reason for [`convert_before_strip`](#subtitle-configuration):
    non-SubtitleEdit engines need the subtitle converted to SRT first, or the
    hearing-impaired cues survive the strip untouched.

## Converting subtitle formats

By default unshackle keeps each subtitle in its downloaded format (only auto-converting
TTML to WebVTT for muxing, as noted above). To force a specific output format, use
`--sub-format`:

```shell title="Convert every subtitle to SRT"
unshackle dl --s-lang en,es --sub-format srt EXAMPLE 81234567
```

`--sub-format` accepts the common format names and aliases, including `srt`, `vtt`,
`ssa`, `ass`, and `ttml`. Pass `original` to explicitly keep the source format and skip
all conversion:

```shell title="Keep whatever the service delivered"
unshackle dl --sub-format original EXAMPLE 'https://www.example.com/...'
```

Conversion only runs when the source format differs from the target, so
`--sub-format srt` on a track that is already SRT is a no-op.

!!! warning "Down-converting styled subtitles is lossy"
    Converting styled SSA/ASS subtitles to SRT throws away positioning, colours, and
    italics. unshackle never does this automatically. It only happens when you
    explicitly ask for it via `--sub-format`. If you want to preserve styling, convert
    to a format that supports it (or keep `original`).

    Note that this automatic protection guards **only the default muxed track**: there the
    conversion is skipped and the styled original is kept. It does **not** cover sidecars.
    A `sidecar_format: srt` will still lossily flatten styled subtitles, dropping
    positioning, colours, and styling, and a per-download `--sub-format srt` forces the
    muxed track to convert as well. To keep raw styled sidecars, set
    [`sidecar_format: original`](#subtitle-configuration).

### How the conversion backend is chosen

Under the hood unshackle picks the highest-fidelity converter that supports the
source→target pair. Which converter runs depends on the
[`conversion_method`](#subtitle-configuration) config key (default `auto`) and the
formats involved:

| Backend | Best at | Requires |
| --- | --- | --- |
| `subtitleedit` | Highest fidelity; preserves positioning and italics | SubtitleEdit installed |
| `subby` | Native SRT output with clean-up | - |
| `pysubs2` | Best fidelity for styled SSA/ASS | - (pure Python, always available) |
| `pycaption` | Last-resort fallback; flattens styling | - |

With `conversion_method: auto`, unshackle ranks these automatically per conversion.
Setting it to a specific value pins that backend as the first choice, falling back to
others only if the pin cannot handle the pair.

#### What each backend actually preserves

The table below comes from round-tripping a subtitle that carries italics, bold,
underline, positioning, and colour through every backend. Each cell lists the
styling that survives; `—` means the backend cannot handle that pair.

| Conversion | `subtitleedit` | `pysubs2` | `subby` | `pycaption` |
| --- | --- | --- | --- | --- |
| WebVTT → SRT | italic, bold, underline, position | italic, underline | italic, position | *(strips all)* |
| WebVTT → ASS | italic, bold, underline, position | italic, bold, underline | — | — |
| WebVTT → TTML | italic, bold, underline, position | italic, bold, underline | position | *(strips all)* |
| ASS/SSA → SRT | all (+ colour) | italic, underline | — | — |
| ASS/SSA → TTML | position only | italic, bold, underline | — | — |
| ASS/SSA → VTT | position only | italic, underline | — | — |

Reading the table:

- **Keep the original**: leaving `--sub-format` unset (or set to `original`) never
  round-trips the file, so every style survives. Only convert when your player cannot
  read the source format.
- **`subtitleedit`** (SubtitleEdit / `seconv`): the best choice for anything → SRT. It
  is the only backend that carries colour, and it embeds `{\an8}` tags to preserve
  positioning. When writing TTML or WebVTT from ASS it flattens inline styling to plain
  text and keeps only positioning, so avoid it there.
- **`pysubs2`**: keeps inline italic/underline on every pair, and bold except when
  writing SRT or WebVTT. It never carries positioning or colour. SSA/ASS is its native
  model, which makes it the best pick for SSA↔ASS.
- **`subby`**: reads only WebVTT/fVTT/SAMI (never ASS) and is tuned for → SRT, where it
  uniquely converts WebVTT cue settings into `{\an8}` positioning. Its
  `CommonIssuesFixer` may also drop near-duplicate cues.
- **`pycaption`**: strips all styling; last-resort fallback only.

!!! tip "What `auto` picks"
    `auto` prefers `subtitleedit` first when it is installed, then falls back to `subby`
    for → SRT and `pysubs2` otherwise. The one exception comes from the table above:
    for ASS/SSA → TTML or WebVTT it picks `pysubs2` even when SubtitleEdit is
    installed, since SubtitleEdit flattens inline styling on those pairs. Most installs
    do not ship SubtitleEdit, so in practice `auto` means `subby` (→ SRT) or `pysubs2`.
    Install [SubtitleEdit / `seconv`](#subtitle-configuration) if you need colour or the
    highest → SRT fidelity.

!!! note "Config wins over a service's preference"
    A service may set a `preferred_conversion_method` on its own tracks (for example when
    it ships subtitles that a particular backend handles best). An explicit
    `conversion_method` in `unshackle.yaml` always overrides that per-track preference, so
    your config takes precedence. This only matters when a service ships a non-default
    preference; leave `conversion_method` at `auto` to let the service's hint stand.

## Muxed vs. sidecar output

By default subtitles are muxed **into** the final `.mkv`. You can instead (or
additionally) write them as separate sidecar files next to the video, controlled by the
[`output_mode`](#subtitle-configuration) config key:

=== "mux (default)"

    ```yaml title="unshackle.yaml"
    subtitle:
      output_mode: mux
    ```

    Subtitles are embedded in the Matroska file. Nothing is written separately.

=== "sidecar"

    ```yaml title="unshackle.yaml"
    subtitle:
      output_mode: sidecar
      sidecar_format: srt
    ```

    Subtitles are written as standalone files and **not** muxed into the video (when
    there is a video or audio track to sit beside).

=== "both"

    ```yaml title="unshackle.yaml"
    subtitle:
      output_mode: both
      sidecar_format: srt
    ```

    Subtitles are muxed into the video **and** written as sidecar files.

Sidecar files are named after the output file with the language and flags encoded in the
name, so players and media managers can match them automatically:

```text
Show.S01E01.1080p.WEB.en.srt
Show.S01E01.1080p.WEB.en.forced.srt
Show.S01E01.1080p.WEB.en.sdh.srt
```

The pattern is `{base}.{language}[.forced][.sdh].{extension}`. The sidecar file format
is set by [`sidecar_format`](#subtitle-configuration); use a format name like `srt` or
`vtt`, or `original` to keep each subtitle in its downloaded format.

## Fonts for styled subtitles

Styled SSA/ASS subtitles reference specific fonts by name. When unshackle muxes such a
subtitle it scans it for the fonts it uses and attaches the matching font files to the
Matroska output so the styling renders correctly on playback. If a referenced font is
not available locally, unshackle logs the missing font names and suggests installing the
relevant font packages.

## Subtitle configuration

All of the behaviour above can be defaulted in the `subtitle` block of your
[configuration file](../getting-started/configuration-file.md):

```yaml title="unshackle.yaml"
subtitle:
  strip_sdh: true
  preserve_formatting: true
  conversion_method: auto
  sdh_method: auto
  convert_before_strip: true
  output_mode: mux
  sidecar_format: srt
```

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `strip_sdh` | `true` | `true` / `false` | Auto-generate a stripped non-SDH copy when a language has only an SDH track. |
| `preserve_formatting` | `true` | `true` / `false` | Keep original WebVTT styling and layout during sanitization. When `false`, WebVTT is re-encoded more aggressively (merging identical cues, dropping empty ones). |
| `conversion_method` | `auto` | `auto`, `subby`, `pysubs2`, `subtitleedit`, `pycaption` | Which conversion backend to prefer. `auto` ranks them per conversion. |
| `sdh_method` | `auto` | `auto`, `subby`, `subtitleedit`, `filter-subs` | Engine used to strip hearing-impaired cues. |
| `convert_before_strip` | `true` | `true` / `false` | Convert a subtitle to SRT before stripping SDH (for non-SubtitleEdit engines). |
| `output_mode` | `mux` | `mux`, `sidecar`, `both` | Whether subtitles are embedded, written as sidecars, or both. |
| `sidecar_format` | `srt` | a format name (e.g. `srt`, `vtt`) or `original` | Format used for sidecar files. |
| `group_by` | `type` | `type`, `language` | Whether all tracks of one type stay together, or each language stays next to its own variants. |
| `language_priority` | *(unset)* | a list of languages | Languages to sort to the top. The rest keep the default order. Nothing is removed. |

!!! tip "SubtitleEdit is optional but recommended"
    Several high-fidelity paths (the best-quality conversions and the SubtitleEdit SDH
    stripper) only activate when the SubtitleEdit CLI is installed and on your `PATH`.
    See [Installation](../getting-started/installation.md) for how to add it. Without it,
    unshackle falls back to its built-in Python backends.

---

## Developer reference

!!! note "Audience"
    This section is for people extending unshackle or writing services. Everyday users
    can stop here.

The subtitle model lives in `unshackle.core.tracks.Subtitle`. A few internals worth
knowing when working with subtitle tracks in code:

- **Flags and validation**: `Subtitle(codec=..., cc=..., sdh=..., forced=...)`. A track
  cannot be both `cc` and `sdh`, and `forced` cannot be combined with either; both raise
  `ValueError` at construction.
- **`convert(codec, *, forced=False)`**: converts the downloaded file in place through
  the backend chain in `subtitle_convert.run_conversion`. `forced=True` (what
  `--sub-format` sets) is the only way lossy styled down-converts such as
  ASS→SRT are permitted. Raises `NotImplementedError` if no backend supports the pair and
  `RuntimeError` if all attempts fail.
- **`strip_hearing_impaired()`**: removes SDH cues using the engine selected by
  `config.subtitle["sdh_method"]`.
- **`reverse_rtl()`**: fixes right-to-left sentence-ending positioning via SubtitleEdit
  (requires the binary).
- **`parse(data, codec)`**: the central parser, returning a `pycaption.CaptionSet`; it
  handles the boxed DASH formats, SAMI, and the WebVTT repair helpers.
- **`extract_fonts(text)`**: returns the set of font names referenced by an ASS/SSA
  subtitle, used to decide which fonts to attach at mux time.

The conversion backends (`SubtitleEditBackend`, `SubbyBackend`, `Pysubs2Backend`,
`PycaptionBackend`) each implement a small protocol (`is_available()`, `can_convert()`,
`rank()`, `convert()`) and are tried in rank order, lowest rank first. See
[the Tracks API reference](../reference/api/tracks.md) for the full model.
