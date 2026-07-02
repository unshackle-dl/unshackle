"""Language tag rule engine for output filename templates."""

from __future__ import annotations

import logging
from typing import Any, Sequence

from langcodes import Language

from unshackle.core.utilities import is_close_match

log = logging.getLogger(__name__)


def evaluate_language_tag(
    rules: list[dict[str, Any]],
    audio_languages: Sequence[Language],
    subtitle_languages: Sequence[Language],
) -> str:
    """Evaluate language tag rules against selected tracks.

    Rules are evaluated in order; the first matching rule's tag is returned.
    Returns empty string if no rules match.

    Args:
        rules: List of rule dicts from config, each with conditions and a ``tag``.
        audio_languages: Languages of the selected audio tracks.
        subtitle_languages: Languages of the selected subtitle tracks.

    Returns:
        The tag string from the first matching rule, or ``""`` if none match.
    """
    for rule in rules:
        tag = rule.get("tag")
        if not tag:
            log.warning("Language tag rule missing 'tag' field, skipping: %s", rule)
            continue

        if _rule_matches(rule, audio_languages, subtitle_languages):
            log.debug("Language tag rule matched: %s -> %s", rule, tag)
            return str(tag)

    return ""


def _rule_matches(
    rule: dict[str, Any],
    audio_languages: Sequence[Language],
    subtitle_languages: Sequence[Language],
) -> bool:
    """Check if all conditions in a rule are satisfied."""
    has_condition = False

    audio_lang = rule.get("audio")
    if audio_lang is not None:
        has_condition = True
        if not is_close_match(audio_lang, list(audio_languages)):
            return False

    subs_contain = rule.get("subs_contain")
    if subs_contain is not None:
        has_condition = True
        if not is_close_match(subs_contain, list(subtitle_languages)):
            return False

    subs_contain_all = rule.get("subs_contain_all")
    if subs_contain_all is not None:
        has_condition = True
        if not isinstance(subs_contain_all, list):
            subs_contain_all = [subs_contain_all]
        for lang in subs_contain_all:
            if not is_close_match(lang, list(subtitle_languages)):
                return False

    if not has_condition:
        log.warning("Language tag rule has no conditions, skipping: %s", rule)
        return False

    return True
