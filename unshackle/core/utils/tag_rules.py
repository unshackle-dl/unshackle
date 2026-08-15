"""Release-group tag rule engine for output filename templates."""

from __future__ import annotations

import logging
import operator
import re
from collections.abc import Mapping
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    "!=": operator.ne,
    "==": operator.eq,
    ">": operator.gt,
    "<": operator.lt,
    "=": operator.eq,
}
_ORDERING = (">=", "<=", ">", "<")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def evaluate_tag_rules(rules: Any, context: dict[str, Any]) -> Optional[str]:
    """Evaluate tag rules against a filename template context.

    Rules are evaluated in order; the first matching rule's tag is returned.
    Conditions are AND-ed, compared case-insensitively, and a list matches if
    any entry matches. A value may lead with a comparison operator, e.g.
    ">=2160" or "!=DV".

    Args:
        rules: Rules straight from the user's YAML, so any shape is possible.
            Each valid rule is a mapping with a ``when`` mapping and a ``tag``.
        context: The already-built filename template context.

    Returns:
        The tag string from the first matching rule, or ``None`` if none match.
    """
    if not isinstance(rules, list):
        log.warning("tag_rules must be a list of rules, ignoring: %r", rules)
        return None

    for rule in rules:
        if not isinstance(rule, Mapping):
            log.warning("Tag rule must be a mapping of 'when' and 'tag', skipping: %r", rule)
            continue

        when = rule.get("when")
        tag = rule.get("tag")
        if when is not None and not isinstance(when, Mapping):
            log.warning("Tag rule 'when' must be a mapping of conditions, skipping: %s", rule)
            continue
        if not when or not tag:
            log.warning("Tag rule needs both 'when' conditions and a 'tag', skipping: %s", rule)
            continue
        if isinstance(tag, bool) or any(_has_bool(value) for value in when.values()):
            log.warning("Tag rule has a YAML boolean, quote the value to use it as text, skipping: %s", rule)
            continue
        if not isinstance(tag, (str, int, float)):
            log.warning("Tag rule 'tag' must be text or a number, skipping: %s", rule)
            continue

        unknown = [key for key in when if key not in context]
        if unknown:
            log.warning("Tag rule has unknown 'when' key(s) %s, skipping: %s", ", ".join(sorted(unknown)), rule)
            continue

        if all(_matches(expected, context[key]) for key, expected in when.items()):
            log.debug("Tag rule matched: %s -> %s", rule, tag)
            return str(tag)

    return None


def _has_bool(expected: Any) -> bool:
    """Check for a YAML boolean, which is an unquoted word the user meant as text."""
    values = expected if isinstance(expected, list) else [expected]
    return any(isinstance(x, bool) for x in values)


def _matches(expected: Any, actual: Any) -> bool:
    """Check one condition, where a list matches on any entry."""
    values = expected if isinstance(expected, list) else [expected]
    return any(_matches_one(x, actual) for x in values)


def _matches_one(expected: Any, actual: Any) -> bool:
    """Check a single value, which may carry a leading comparison operator."""
    text = "" if expected is None else str(expected)
    for op in sorted(_OPS, key=len, reverse=True):
        if text.startswith(op):
            return _compare(op, text[len(op) :].strip(), actual)
    return str(actual or "").lower() == text.lower()


def _compare(op: str, operand: str, actual: Any) -> bool:
    """Compare against an operand, numerically when the operand is a number."""
    if not operand:
        log.warning("Tag rule condition '%s' has no value to compare against, condition failed", op)
        return False

    try:
        wanted = float(operand)
    except ValueError:
        if op in _ORDERING:
            log.warning("Tag rule condition '%s%s' needs a numeric operand, condition failed", op, operand)
            return False
        return _OPS[op](str(actual or "").lower(), operand.lower())

    found = _NUMBER.search(str(actual or ""))
    if not found:
        return op == "!="
    return _OPS[op](float(found.group()), wanted)
