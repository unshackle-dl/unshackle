# Subtitle Processing Configuration

This document covers subtitle processing and formatting options.

## subtitle (dict)

Control subtitle conversion and SDH (hearing-impaired) stripping behavior.

- `conversion_method`: How to convert subtitles between formats. Default: `auto`.
  - `auto`: Smart routing - use subby for WebVTT/SAMI, pycaption for others.
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

- `preserve_formatting`: Keep original subtitle tags and positioning during conversion. When true, skips pycaption processing for WebVTT files to keep tags like `<i>`, `<b>`, and positioning intact. Default: `true`.

- `output_mode`: Controls how subtitles are included in the output. Default: `mux`.
  - `mux`: Embed subtitles in the MKV container only.
  - `sidecar`: Save subtitles as separate files only (not muxed into the container).
  - `both`: Embed subtitles in the MKV container and save as sidecar files.

- `sidecar_format`: Format for sidecar subtitle files (used when `output_mode` is `sidecar` or `both`). Default: `srt`.
  - `srt`: SubRip format.
  - `vtt`: WebVTT format.
  - `ass`: Advanced SubStation Alpha format.
  - `original`: Keep the subtitle in its current format without conversion.

Example:

```yaml
subtitle:
  conversion_method: auto
  sdh_method: auto
  strip_sdh: true
  convert_before_strip: true
  preserve_formatting: true
  output_mode: mux
  sidecar_format: srt
```

---
