"""
Subtitle conversion backend registry.

Routing is data-driven: each backend declares which (source -> target) codec pairs it can
read/write, whether it is available in the current environment, and a preference rank.
``resolve_backends`` filters the registry to the available backends that support the
requested pair and orders them by rank; ``run_conversion`` tries each in turn (a real
fallback chain) until one succeeds.

The public entry point stays ``Subtitle.convert`` / ``Subtitle.strip_hearing_impaired`` in
subtitle.py — this module only holds the selection + conversion logic so subtitle.py keeps
the codec enum, ``parse``, sanitizers and cue helpers (the collaborators backends reuse).
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional, Protocol

import pycaption
import pysubs2
from subby import CommonIssuesFixer, SAMIConverter, WebVTTConverter, WVTTConverter

from unshackle.core import binaries
from unshackle.core.tracks.subtitle import Subtitle
from unshackle.core.utils.subprocess import log_tool_run

log = logging.getLogger("subtitle")

Codec = Subtitle.Codec

# SubtitleEdit (and the cross-platform seconv port) /convert format names.
# Shared by SubtitleEditBackend, strip_hearing_impaired and reverse_rtl so the map lives once.
SUBTITLE_EDIT_FORMATS: dict[Codec, str] = {
    Codec.SubRip: "subrip",
    Codec.SubStationAlpha: "substationalpha",
    Codec.SubStationAlphav4: "advancedsubstationalpha",
    Codec.TimedTextMarkupLang: "timedtext1.0",
    Codec.WebVTT: "webvtt",
    Codec.SAMI: "sami",
    Codec.MicroDVD: "microdvd",
}

# pycaption can only WRITE these three formats.
PYCAPTION_WRITERS = {
    Codec.SubRip: pycaption.SRTWriter,
    Codec.TimedTextMarkupLang: pycaption.DFXPWriter,
    Codec.WebVTT: pycaption.WebVTTWriter,
}

# pysubs2 format identifiers per codec.
PYSUBS2_FORMATS: dict[Codec, str] = {
    Codec.SubRip: "srt",
    Codec.SubStationAlpha: "ssa",
    Codec.SubStationAlphav4: "ass",
    Codec.WebVTT: "vtt",
    Codec.TimedTextMarkupLang: "ttml",
    Codec.SAMI: "sami",
    Codec.MicroDVD: "microdvd",
    Codec.MPL2: "mpl2",
    Codec.TMP: "tmp",
}


def is_seconv(binary: object) -> bool:
    """True for the SubtitleEdit 5+ CLI (seconv); 4.x binaries need legacy /convert syntax."""
    name = str(binary).replace("\\", "/").rsplit("/", 1)[-1]
    return name.lower().rsplit(".", 1)[0] == "seconv"


def subtitleedit_args(
    binary: object,
    src: Path,
    fmt: str,
    *,
    output_folder: Optional[Path] = None,
    convert_colors: bool = False,
    remove_hi: bool = False,
    reverse_rtl: bool = False,
) -> list[str]:
    """
    Build a SubtitleEdit batch-convert command in 5.x (seconv ``--flags``) or 4.x
    (``/convert /flags``) syntax, per ``is_seconv``.

    Both name the output ``<input-stem>.<format-ext>``; ``output_folder`` steers placement
    (a bare ``--output-filename`` resolves against the cwd, not the input dir). Overwrite is
    always set so re-runs and in-place transforms (SDH/RTL) don't fail on an existing file.
    """
    if not is_seconv(binary):
        args = [str(binary), "/convert", str(src), fmt, "/encoding:utf8", "/overwrite"]
        if output_folder is not None:
            args.append(f"/outputfolder:{output_folder}")
        if convert_colors:
            args.append("/ConvertColorsToDialog")
        if remove_hi:
            args.append("/RemoveTextForHI")
        if reverse_rtl:
            args.append("/ReverseRtlStartEnd")
        return args

    args = [str(binary), str(src), fmt, "--encoding:utf-8", "--overwrite"]
    if output_folder is not None:
        args.append(f"--output-folder:{output_folder}")
    if convert_colors:
        args.append("--convert-colors-to-dialog")
    if remove_hi:
        args.append("--remove-text-for-hi")
    if reverse_rtl:
        args.append("--reverse-rtl-start-end")
    return args


# Styled SubStation formats flattened to SRT lose positioning/colours/italics.
# Never performed automatically — only when the user explicitly forces a target format.
LOSSY_DOWNCONVERTS: frozenset[tuple[Codec, Codec]] = frozenset(
    {
        (Codec.SubStationAlpha, Codec.SubRip),
        (Codec.SubStationAlphav4, Codec.SubRip),
    }
)


class SubtitleBackend(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def can_convert(self, source: Codec, target: Codec) -> bool: ...

    def rank(self, source: Codec, target: Codec) -> int: ...

    def convert(self, source: Codec, src: Path, target: Codec, out: Path) -> None:
        """Convert ``src`` (a ``source`` file) to ``target``, writing to ``out``. Raise on failure."""
        ...


class SubtitleEditBackend:
    """SubtitleEdit / seconv CLI. Highest fidelity (keeps positioning + italics) when present."""

    name = "subtitleedit"
    reads = frozenset(SUBTITLE_EDIT_FORMATS)
    writes = frozenset(SUBTITLE_EDIT_FORMATS)

    def is_available(self) -> bool:
        return bool(binaries.SubtitleEdit)

    def can_convert(self, source: Codec, target: Codec) -> bool:
        # Segmented box formats cannot be read by SubtitleEdit.
        if source in (Codec.fTTML, Codec.fVTT):
            return False
        return source in self.reads and target in self.writes

    def rank(self, source: Codec, target: Codec) -> int:
        return 0

    def convert(self, source: Codec, src: Path, target: Codec, out: Path) -> None:
        args = subtitleedit_args(
            binaries.SubtitleEdit,
            src,
            SUBTITLE_EDIT_FORMATS[target],
            output_folder=out.parent,
            convert_colors=(target == Codec.SubRip),
        )
        se_start = time.monotonic()
        subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log_tool_run(
            "SubtitleEdit convert",
            "SubtitleEdit",
            0,
            duration_ms=round((time.monotonic() - se_start) * 1000, 1),
            source=str(source),
            target=str(target),
        )
        # SE5 names the output <input-stem>.<format-ext>, which may differ from our target
        # suffix (e.g. timedtext1.0 -> .ttml). Normalise it onto `out`.
        if not out.exists():
            produced = next((p for p in src.parent.glob(f"{src.stem}.*") if p not in (src, out)), None)
            if produced is None:
                raise FileNotFoundError(f"SubtitleEdit produced no output for {src.name} -> {target.name}")
            produced.replace(out)


class Pysubs2Backend:
    """pysubs2 — pure Python, broad format support, best fidelity for SSA/ASS (native style model)."""

    name = "pysubs2"
    formats = frozenset(PYSUBS2_FORMATS)

    def is_available(self) -> bool:
        return True

    def can_convert(self, source: Codec, target: Codec) -> bool:
        return source in self.formats and target in self.formats

    def rank(self, source: Codec, target: Codec) -> int:
        # Preferred reader for styled SubStation sources; solid general fallback otherwise.
        return 1 if source in (Codec.SubStationAlpha, Codec.SubStationAlphav4) else 2

    def convert(self, source: Codec, src: Path, target: Codec, out: Path) -> None:
        subs = pysubs2.load(str(src), encoding="utf-8")
        subs.save(str(out), format_=PYSUBS2_FORMATS[target], encoding="utf-8")


class SubbyBackend:
    """subby — purpose-built for streaming subs. WebVTT/fVTT/SAMI -> SRT + CommonIssuesFixer cleanup."""

    name = "subby"
    reads = frozenset({Codec.WebVTT, Codec.fVTT, Codec.SAMI})
    # Native SRT output; non-SRT targets re-encoded from the SRT intermediate via pycaption.
    writes = frozenset({Codec.SubRip, Codec.TimedTextMarkupLang, Codec.WebVTT})
    converters = {
        Codec.WebVTT: WebVTTConverter,
        Codec.fVTT: WVTTConverter,
        Codec.SAMI: SAMIConverter,
    }

    def is_available(self) -> bool:
        return True

    def can_convert(self, source: Codec, target: Codec) -> bool:
        return source in self.reads and target in self.writes

    def rank(self, source: Codec, target: Codec) -> int:
        # Great for *->SRT (adds cleanup); the SRT intermediate is lossy for other targets.
        return 1 if target == Codec.SubRip else 5

    def convert(self, source: Codec, src: Path, target: Codec, out: Path) -> None:
        srt_subtitles = self.converters[source]().from_file(src)
        fixed_srt, _ = CommonIssuesFixer().from_srt(srt_subtitles)
        if target == Codec.SubRip:
            fixed_srt.save(out, encoding="utf8")
            return
        temp_srt = src.with_suffix(".temp.srt")
        fixed_srt.save(temp_srt, encoding="utf8")
        try:
            caption_set = Subtitle.parse(temp_srt.read_bytes(), Codec.SubRip)
            Subtitle.merge_same_cues(caption_set)
            out.write_text(PYCAPTION_WRITERS[target]().write(caption_set), encoding="utf8")
        finally:
            temp_srt.unlink(missing_ok=True)


class PycaptionBackend:
    """pycaption — last resort. Note: flattens positioning/italics (devine #39), so ranked last."""

    name = "pycaption"
    reads = frozenset({Codec.SubRip, Codec.TimedTextMarkupLang, Codec.WebVTT, Codec.SAMI, Codec.fTTML, Codec.fVTT})
    writes = frozenset(PYCAPTION_WRITERS)

    def is_available(self) -> bool:
        return True

    def can_convert(self, source: Codec, target: Codec) -> bool:
        return source in self.reads and target in self.writes

    def rank(self, source: Codec, target: Codec) -> int:
        return 9

    def convert(self, source: Codec, src: Path, target: Codec, out: Path) -> None:
        caption_set = Subtitle.parse(src.read_bytes(), source)
        Subtitle.merge_same_cues(caption_set)
        if target == Codec.WebVTT:
            Subtitle.filter_unwanted_cues(caption_set)
        out.write_text(PYCAPTION_WRITERS[target]().write(caption_set), encoding="utf8")


REGISTRY: list[SubtitleBackend] = [
    SubtitleEditBackend(),
    SubbyBackend(),
    Pysubs2Backend(),
    PycaptionBackend(),
]


def resolve_backends(source: Codec, target: Codec, *, pin: Optional[str] = None) -> list[SubtitleBackend]:
    """Available backends that support source->target, ordered by rank. A pin is tried first."""
    available = [b for b in REGISTRY if b.is_available() and b.can_convert(source, target)]
    if pin:
        pinned = [b for b in available if b.name == pin]
        rest = sorted((b for b in available if b.name != pin), key=lambda b: b.rank(source, target))
        return pinned + rest
    return sorted(available, key=lambda b: b.rank(source, target))


def finalize(sub: Subtitle, target: Codec, out: Path) -> Path:
    """Swap the track onto the converted file and fire the OnConverted callback."""
    original = sub.path
    if original and original.exists() and original != out:
        original.unlink()
    sub.path = out
    sub.codec = target
    if callable(sub.OnConverted):
        sub.OnConverted(target)
    return out


def run_conversion(sub: Subtitle, target: Codec, *, pin: Optional[str] = None, forced: bool = False) -> Path:
    """
    Convert ``sub`` to ``target`` using the best available backend, falling back through the
    capability chain on failure.

    ``forced`` is True only for explicit user requests (``--sub-format``); lossy downconverts
    (styled SubStation -> SRT) are skipped unless forced.
    """
    if sub.path is None or not sub.path.exists():
        raise ValueError("You must download the subtitle track first.")
    if sub.codec is None:
        raise ValueError("Subtitle has no codec to convert from.")
    source, src = sub.codec, sub.path

    if source == target:
        return src

    if (source, target) in LOSSY_DOWNCONVERTS and not forced:
        log.info(
            f"Keeping {source.name} subtitle as-is "
            f"(skipping lossy auto-conversion to {target.name}; pass --sub-format to force)"
        )
        return src

    chain = resolve_backends(source, target, pin=pin)
    if not chain:
        raise NotImplementedError(f"Cannot convert {source.name} to {target.name}.")

    out = src.with_suffix(f".{target.value.lower()}")
    last_exc: Optional[Exception] = None
    for backend in chain:
        try:
            backend.convert(source, src, target, out)
        except Exception as e:
            last_exc = e
            log.debug(f"Subtitle backend {backend.name} failed ({source.name}->{target.name}): {e}")
            continue
        return finalize(sub, target, out)

    raise RuntimeError(f"All subtitle backends failed for {source.name}->{target.name}") from last_exc
