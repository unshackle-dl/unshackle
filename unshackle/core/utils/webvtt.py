import re
from typing import Optional

# Timing line: optional hours, verbatim settings tail.
_TIMING = re.compile(r"^((?:\d+:)?\d{2}:\d{2}\.\d{3})[ \t]+-->[ \t]+((?:\d+:)?\d{2}:\d{2}\.\d{3})(.*)$")


def _timestamp_ms(ts: str) -> int:
    hms, ms = ts.rsplit(".", 1)
    parts = [int(p) for p in hms.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(ms)


def merge_segmented_webvtt(vtt_raw: str, segment_durations: Optional[list[int]] = None, timescale: int = 1) -> str:
    """
    Merge concatenated Segmented WebVTT documents into a single document.

    Strips the per-segment WEBVTT and X-TIMESTAMP-MAP headers and splices cues
    repeated across a segment boundary (identical payload, <=1ms gap or overlap).
    Timing lines, cue settings and payload are kept verbatim, so inline formatting
    (span tags, entities, ASS-style overrides) survives untouched.

    No timestamp offset is applied: segments are expected to carry absolute cue
    times already (same guard as shaka-player and N_m3u8DL-RE; re-offsetting
    double-shifts such streams). segment_durations/timescale are accepted for
    call-site compatibility and only matter if offsetting is ever reintroduced.

    Unique STYLE/REGION blocks are kept (players that don't support them ignore
    them); cue identifiers and NOTE blocks are dropped; cues with a malformed
    timing line are skipped (downstream sanitizers catch worse).
    """
    # each kept cue: [start, end, settings, payload, normalized payload for dedupe]
    cues: list[list[str]] = []
    headers: list[str] = []  # unique STYLE/REGION blocks, first-seen order
    prev: Optional[list[str]] = None
    prev_end_ms = 0

    for block in re.split(r"\n[ \t]*\n", vtt_raw.replace("\r\n", "\n").replace("\r", "\n")):
        lines = [
            line
            for line in block.split("\n")
            if not (line.startswith("WEBVTT") or line.lstrip().startswith("X-TIMESTAMP-MAP"))
        ]
        timing_i = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_i is None:  # NOTE or other metadata block
            first = next((line.strip() for line in lines if line.strip()), "")
            if first.startswith(("STYLE", "REGION")):
                header = "\n".join(lines).strip("\n")
                if header not in headers:  # segments repeat the same block
                    headers.append(header)
            continue
        timing = _TIMING.match(lines[timing_i].strip())
        if not timing:
            continue
        start, end, settings = timing.groups()
        payload = [line for line in lines[timing_i + 1 :] if line.strip()]
        if not payload:
            continue

        normalized = "\n".join(line.strip() for line in payload)
        if prev is not None and _timestamp_ms(start) - prev_end_ms <= 1 and normalized == prev[4]:
            prev[1] = end  # splice: extend the kept cue, drop the duplicate
            prev_end_ms = _timestamp_ms(end)
            continue

        prev = [start, end, settings, "\n".join(payload), normalized]
        prev_end_ms = _timestamp_ms(end)
        cues.append(prev)

    blocks = headers + [f"{c[0]} --> {c[1]}{c[2]}\n{c[3]}" for c in cues]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"
