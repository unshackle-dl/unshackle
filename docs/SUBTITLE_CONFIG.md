# Subtitle Processing Configuration

This document covers subtitle processing and formatting options.

## subtitle (dict)

Control subtitle conversion and SDH (hearing-impaired) stripping behavior.

- `conversion_method`: How to convert subtitles between formats. Default: `pysubs2`.
  - `auto`: Use subby for WebVTT/SAMI, standard for others.
  - `subby`: Always use subby with CommonIssuesFixer.
  - `subtitleedit`: Prefer SubtitleEdit when available; otherwise fallback to standard conversion.
  - `pycaption`: Use only the pycaption library (no SubtitleEdit, no subby).
  - `pysubs2`: Use pysubs2 library (supports SRT, SSA, ASS, WebVTT, TTML, SAMI, MicroDVD, MPL2, TMP formats).

- `sdh_method`: How to strip SDH cues. Default: `auto`.
  - `auto`: Try subby for SRT first, then SubtitleEdit, then filter-subs.
  - `subby`: Use subby's SDHStripper. **Note:** Only works with SRT files; other formats will fall back to alternative methods.
  - `subtitleedit`: Use SubtitleEdit's RemoveTextForHI when available.
  - `filter-subs`: Use the subtitle-filter library.

- `strip_sdh`: Enable/disable automatic SDH (hearing-impaired) cue stripping. Default: `true`.

- `convert_before_strip`: When using `filter-subs` SDH method, automatically convert subtitles to SRT format first for better compatibility. Default: `true`.

- `preserve_formatting`: Keep original subtitle tags and positioning during conversion. Default: `true`.

Example:

```yaml
subtitle:
  conversion_method: pysubs2
  sdh_method: auto
  strip_sdh: true
  convert_before_strip: true
  preserve_formatting: true
```

---
