"""Language tag rule engine for output filename templates."""

from __future__ import annotations

import logging
from collections.abc import Mapping as MappingABC
from typing import Any, Mapping, Optional, Sequence

from langcodes import Language
from langcodes.tag_parser import LanguageTagError

from unshackle.core.utilities import is_close_match

log = logging.getLogger(__name__)

# Conditions that test a filename variable of the same name rather than a language.
STATE_CONDITIONS = ("dual", "multi", "dubbed")
LANGUAGE_CONDITIONS = ("audio", "subs_contain", "subs_contain_all")


def evaluate_language_tag(
    rules: list[dict[str, Any]],
    audio_languages: Sequence[Language],
    subtitle_languages: Sequence[Language],
    states: Optional[Mapping[str, bool]] = None,
) -> str:
    """Evaluate language tag rules against selected tracks.

    This function evaluates the rules in order. It returns the tag of the first rule
    that matches, or an empty string if no rule matches.

    Args:
        rules: List of rule dicts from config, each with conditions and a ``tag``.
        audio_languages: Languages of the selected audio tracks.
        subtitle_languages: Languages of the selected subtitle tracks.
        states: Whether each of ``dual``, ``multi`` and ``dubbed`` is set for this release,
            the same state as the filename variable of that name.

    Returns:
        The tag string from the first rule that matches, or ``""`` if none match.
    """
    if not isinstance(rules, list):
        log.warning("Language tag rules must be a list of rules, ignoring: %r", rules)
        return ""

    for rule in rules:
        if not isinstance(rule, MappingABC):
            log.warning("Language tag rule must be a mapping of conditions and a 'tag', skipping: %r", rule)
            continue

        tag = rule.get("tag")
        if not tag:
            log.warning("Language tag rule missing 'tag' field, skipping: %s", rule)
            continue

        if rule_matches(rule, audio_languages, subtitle_languages, states):
            log.debug("Language tag rule matched: %s -> %s", rule, tag)
            return str(tag)

    return ""


def rule_matches(
    rule: dict[str, Any],
    audio_languages: Sequence[Language],
    subtitle_languages: Sequence[Language],
    states: Optional[Mapping[str, bool]] = None,
) -> bool:
    """Return True when every condition in the rule matches the tracks."""
    unknown = [key for key in rule if key not in (*LANGUAGE_CONDITIONS, *STATE_CONDITIONS, "tag")]
    if unknown:
        log.warning("Language tag rule has unknown key(s) %s, skipping: %s", ", ".join(sorted(unknown)), rule)
        return False

    has_condition = False

    audio_lang = rule.get("audio")
    if audio_lang is not None:
        has_condition = True
        if not any(_matches(lang, audio_languages) for lang in _as_list(audio_lang)):
            return False

    subs_contain = rule.get("subs_contain")
    if subs_contain is not None:
        has_condition = True
        if not any(_matches(lang, subtitle_languages) for lang in _as_list(subs_contain)):
            return False

    subs_contain_all = rule.get("subs_contain_all")
    if subs_contain_all is not None:
        has_condition = True
        for lang in _as_list(subs_contain_all):
            if not _matches(lang, subtitle_languages):
                return False

    for name in STATE_CONDITIONS:
        wanted = rule.get(name)
        if wanted is not None:
            has_condition = True
            if not isinstance(wanted, bool):
                log.warning("Language tag rule condition %r must be true or false, skipping: %s", name, rule)
                return False
            if wanted is not bool((states or {}).get(name)):
                return False

    if not has_condition:
        log.warning("Language tag rule has no conditions, skipping: %s", rule)
        return False

    return True


def _as_list(value: Any) -> list[Any]:
    """Return the rule value as a list, so a scalar and a one-item list behave the same."""
    return value if isinstance(value, list) else [value]


def _matches(lang: Any, languages: Sequence[Language]) -> bool:
    """Return True when the rule language closely matches one of the track languages.

    A rule value that is not a valid language tag counts as no match. The filename is built
    after the mux, so a typo in the config must not discard a finished download.
    """
    try:
        return is_close_match(lang, list(languages))
    except LanguageTagError as e:
        log.warning("Language tag rule value %r is not a valid language tag, treating as no match: %s", lang, e)
        return False
