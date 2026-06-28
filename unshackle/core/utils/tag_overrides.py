"""Group tag override rule engine for output filename templates.

Lets the group ``tag`` (the ``-{tag}`` suffix) be swapped for a different value
when a release matches a set of conditions — for example tagging Russian-only
releases with a different group name, or using a dedicated tag for a given
service or dynamic range.

Rules are evaluated against the already-computed filename context, so any value
present in the template (``lang_tag``, ``source``, ``hdr``, ``resolution``,
``video`` …) can be used as a condition, in addition to the selected track
languages.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from langcodes import Language

from unshackle.core.utilities import is_close_match

log = logging.getLogger(__name__)

# Context fields a rule may match directly. Values are compared
# case-insensitively; a list value matches if ANY entry matches.
_STRING_FIELDS = (
    "lang_tag",
    "source",
    "quality",
    "resolution",
    "video",
    "hdr",
    "hfr",
    "audio",  # audio codec (e.g. EAC3)
    "audio_channels",
    "atmos",
    "dual",
    "multi",
    "edition",
    "repack",
)


def evaluate_tag_override(
    rules: list[dict[str, Any]],
    context: dict[str, Any],
    audio_languages: Sequence[Language],
    subtitle_languages: Sequence[Language],
) -> Optional[str]:
    """Evaluate group-tag override rules against the filename context.

    Rules are evaluated in order; the first matching rule's ``tag`` is returned,
    replacing the default group tag. Returns ``None`` when no rule matches.

    Args:
        rules: List of rule dicts from config, each with conditions and a ``tag``.
        context: The filename template context (``lang_tag``, ``source``, ``hdr`` …).
        audio_languages: Languages of the selected audio tracks.
        subtitle_languages: Languages of the selected subtitle tracks.

    Returns:
        The overriding tag from the first matching rule, or ``None`` if none match.
    """
    for rule in rules:
        tag = rule.get("tag")
        if not tag:
            log.warning("Tag override rule missing 'tag' field, skipping: %s", rule)
            continue

        if _rule_matches(rule, context, audio_languages, subtitle_languages):
            log.debug("Tag override rule matched: %s -> %s", rule, tag)
            return str(tag)

    return None


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else [value]


def _string_matches(expected: Any, actual: Any) -> bool:
    """Case-insensitive match; a list of expected values matches if any one does."""
    actual_norm = str(actual or "").lower()
    return any(str(item).lower() == actual_norm for item in _as_list(expected))


def _rule_matches(
    rule: dict[str, Any],
    context: dict[str, Any],
    audio_languages: Sequence[Language],
    subtitle_languages: Sequence[Language],
) -> bool:
    """Check if all conditions in a rule are satisfied (AND logic)."""
    has_condition = False

    for field in _STRING_FIELDS:
        expected = rule.get(field)
        if expected is not None:
            has_condition = True
            if not _string_matches(expected, context.get(field, "")):
                return False

    res_min = rule.get("resolution_min")
    res_max = rule.get("resolution_max")
    if res_min is not None or res_max is not None:
        has_condition = True
        try:
            resolution = int(context.get("resolution") or 0)
        except (TypeError, ValueError):
            return False
        if res_min is not None and resolution < int(res_min):
            return False
        if res_max is not None and resolution > int(res_max):
            return False

    audio_lang = rule.get("audio_lang")
    if audio_lang is not None:
        has_condition = True
        if not is_close_match(audio_lang, list(audio_languages)):
            return False

    subs_lang = rule.get("subs_lang")
    if subs_lang is not None:
        has_condition = True
        if not is_close_match(subs_lang, list(subtitle_languages)):
            return False

    audio_count_min = rule.get("audio_count_min")
    if audio_count_min is not None:
        has_condition = True
        try:
            min_count = int(audio_count_min)
        except (TypeError, ValueError):
            return False
        distinct = {str(lang).split("-")[0].lower() for lang in audio_languages if lang}
        if len(distinct) < min_count:
            return False

    if not has_condition:
        log.warning("Tag override rule has no conditions, skipping: %s", rule)
        return False

    return True
