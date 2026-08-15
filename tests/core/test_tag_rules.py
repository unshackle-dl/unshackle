"""Tests for the tag_rules engine behind conditional release-group tags."""

from __future__ import annotations

from typing import Any

import pytest

from unshackle.core.utils.tag_rules import evaluate_tag_rules

CONTEXT: dict[str, Any] = {
    "tag": "NOGRP",
    "title_type": "movie",
    "quality": "2160p",
    "resolution": "2160",
    "hdr": "DV",
    "lang_tag": "SUBBED",
    "edition": "",
}


def test_first_match_wins() -> None:
    rules = [
        {"when": {"quality": "2160p"}, "tag": "FIRST"},
        {"when": {"title_type": "movie"}, "tag": "SECOND"},
    ]
    assert evaluate_tag_rules(rules, CONTEXT) == "FIRST"


def test_list_matches_any_entry() -> None:
    rules = [{"when": {"hdr": ["HDR10P", "DV"]}, "tag": "UHDGROUP"}]
    assert evaluate_tag_rules(rules, CONTEXT) == "UHDGROUP"


def test_list_with_no_matching_entry() -> None:
    rules = [{"when": {"hdr": ["HDR10P", "HLG"]}, "tag": "UHDGROUP"}]
    assert evaluate_tag_rules(rules, CONTEXT) is None


def test_case_insensitive() -> None:
    rules = [{"when": {"lang_tag": "subbed", "quality": "2160P"}, "tag": "SUBMOVIES"}]
    assert evaluate_tag_rules(rules, CONTEXT) == "SUBMOVIES"


def test_conditions_are_anded() -> None:
    rules = [{"when": {"title_type": "movie", "quality": "1080p"}, "tag": "NOPE"}]
    assert evaluate_tag_rules(rules, CONTEXT) is None


def test_unknown_key_skips_rule_but_later_rules_match(caplog: pytest.LogCaptureFixture) -> None:
    rules = [
        {"when": {"bogus": "value"}, "tag": "SKIPPED"},
        {"when": {"title_type": "movie"}, "tag": "MATCHED"},
    ]
    assert evaluate_tag_rules(rules, CONTEXT) == "MATCHED"
    assert "bogus" in caplog.text


@pytest.mark.parametrize(
    "rule",
    [
        {"when": {"title_type": "movie"}},
        {"tag": "NOCONDITIONS"},
        {"when": {}, "tag": "EMPTY"},
    ],
)
def test_malformed_rule_skipped(rule: dict[str, Any]) -> None:
    assert evaluate_tag_rules([rule], CONTEXT) is None


def test_no_rules_and_no_match() -> None:
    assert evaluate_tag_rules([], CONTEXT) is None
    assert evaluate_tag_rules([{"when": {"quality": "720p"}, "tag": "SD"}], CONTEXT) is None


def test_empty_context_value_matches_empty_condition() -> None:
    rules = [{"when": {"edition": ""}, "tag": "PLAIN"}]
    assert evaluate_tag_rules(rules, CONTEXT) == "PLAIN"


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (">=2160", True),
        (">=4320", False),
        ("<=2160", True),
        ("<1080", False),
        (">1080", True),
        ("==2160", True),
        ("=2160", True),
        ("!=1080", True),
        ("!=2160", False),
        (">= 2160", True),
    ],
)
def test_numeric_operators_extract_first_number(condition: str, expected: bool) -> None:
    """The actual value is "2160p", so the suffix must not defeat the comparison."""
    rules = [{"when": {"quality": condition}, "tag": "MATCHED"}]
    assert (evaluate_tag_rules(rules, CONTEXT) == "MATCHED") is expected


def test_string_inequality() -> None:
    assert evaluate_tag_rules([{"when": {"hdr": "!=DV"}, "tag": "NODV"}], CONTEXT) is None
    assert evaluate_tag_rules([{"when": {"hdr": "!=HDR10P"}, "tag": "NOHDR10P"}], CONTEXT) == "NOHDR10P"


def test_string_equality_operator_is_case_insensitive() -> None:
    assert evaluate_tag_rules([{"when": {"lang_tag": "==subbed"}, "tag": "SUBGRP"}], CONTEXT) == "SUBGRP"


def test_plain_equality_unchanged() -> None:
    assert evaluate_tag_rules([{"when": {"quality": "2160p"}, "tag": "UHD"}], CONTEXT) == "UHD"
    assert evaluate_tag_rules([{"when": {"quality": "2160"}, "tag": "UHD"}], CONTEXT) is None


def test_numeric_operator_against_value_without_a_number() -> None:
    rules = [{"when": {"hdr": ">=1080"}, "tag": "NOPE"}]
    assert evaluate_tag_rules(rules, CONTEXT) is None


def test_ordering_operator_with_non_numeric_operand(caplog: pytest.LogCaptureFixture) -> None:
    rules = [{"when": {"hdr": ">DV"}, "tag": "NOPE"}]
    assert evaluate_tag_rules(rules, CONTEXT) is None
    assert "numeric operand" in caplog.text


def test_operators_inside_lists() -> None:
    rules = [{"when": {"quality": ["<720", ">=2160"]}, "tag": "MATCHED"}]
    assert evaluate_tag_rules(rules, CONTEXT) == "MATCHED"
    rules = [{"when": {"quality": ["<720", "1080p"]}, "tag": "NOPE"}]
    assert evaluate_tag_rules(rules, CONTEXT) is None


@pytest.mark.parametrize("rules", ["UHDGROUP", {"when": {"quality": "2160p"}, "tag": "UHD"}, None, 42])
def test_rules_that_are_not_a_list(rules: Any) -> None:
    """A mapping or string here iterates keys/characters, so it must be rejected outright."""
    assert evaluate_tag_rules(rules, CONTEXT) is None


@pytest.mark.parametrize("rule", [None, "UHDGROUP", 42, ["when", "tag"]])
def test_rule_entry_that_is_not_a_mapping_skipped(rule: Any) -> None:
    assert evaluate_tag_rules([rule], CONTEXT) is None
    assert evaluate_tag_rules([rule, {"when": {"title_type": "movie"}, "tag": "MATCHED"}], CONTEXT) == "MATCHED"


@pytest.mark.parametrize("when", [["title_type", "movie"], "title_type", 42])
def test_when_that_is_not_a_mapping_skipped(when: Any) -> None:
    assert evaluate_tag_rules([{"when": when, "tag": "NOPE"}], CONTEXT) is None


@pytest.mark.parametrize(
    "rule",
    [
        {"when": {"atmos": True}, "tag": "ATMOS"},
        {"when": {"atmos": [True, "Atmos"]}, "tag": "ATMOS"},
        {"when": {"title_type": "movie"}, "tag": True},
    ],
)
def test_yaml_boolean_skipped(rule: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    """`atmos: true` / `tag: yes` are YAML coercions the user meant as text."""
    assert evaluate_tag_rules([rule], CONTEXT) is None
    assert "boolean" in caplog.text


def test_numbers_stay_valid() -> None:
    assert evaluate_tag_rules([{"when": {"resolution": 2160}, "tag": "UHD"}], CONTEXT) == "UHD"
    assert evaluate_tag_rules([{"when": {"resolution": 1080}, "tag": "NOPE"}], CONTEXT) is None


def test_numeric_tag_becomes_text() -> None:
    assert evaluate_tag_rules([{"when": {"title_type": "movie"}, "tag": 123}], CONTEXT) == "123"


@pytest.mark.parametrize("tag", [["A", "B"], {"x": 1}, ("A",), {"A"}])
def test_non_scalar_tag_skipped(tag: Any, caplog: pytest.LogCaptureFixture) -> None:
    """A list or dict here used to reach the filename as its repr."""
    assert evaluate_tag_rules([{"when": {"title_type": "movie"}, "tag": tag}], CONTEXT) is None
    assert "must be text or a number" in caplog.text


@pytest.mark.parametrize("tag", ["", 0])
def test_empty_tag_skipped(tag: Any) -> None:
    assert evaluate_tag_rules([{"when": {"title_type": "movie"}, "tag": tag}], CONTEXT) is None


def test_skipped_rule_falls_through_to_a_later_one() -> None:
    rules = [
        {"when": {"atmos": True}, "tag": "SKIPPED"},
        {"when": {"title_type": "movie"}, "tag": "MATCHED"},
    ]
    assert evaluate_tag_rules(rules, CONTEXT) == "MATCHED"


def test_several_negations_in_a_list_always_match() -> None:
    """Any-entry matching makes a list of negations useless, which the docs warn about."""
    rules = [{"when": {"hdr": ["!=DV", "!=HLG"]}, "tag": "ALWAYS"}]
    assert evaluate_tag_rules(rules, CONTEXT) == "ALWAYS"


def test_leading_space_before_an_operator_is_literal() -> None:
    """Current behaviour: the operator is only found at the very start of the value."""
    rules = [{"when": {"quality": " >=2160"}, "tag": "NOPE"}]
    assert evaluate_tag_rules(rules, CONTEXT) is None


def test_inequality_holds_for_a_value_with_no_number() -> None:
    """ "DV" really is not equal to 2160, so != holds while the others fail."""
    assert evaluate_tag_rules([{"when": {"hdr": "!=2160"}, "tag": "MATCHED"}], CONTEXT) == "MATCHED"
    assert evaluate_tag_rules([{"when": {"hdr": ">=2160"}, "tag": "NOPE"}], CONTEXT) is None
    assert evaluate_tag_rules([{"when": {"hdr": "==2160"}, "tag": "NOPE"}], CONTEXT) is None


@pytest.mark.parametrize("condition", ["!=", ">=", "<", "=", "=="])
def test_bare_operator_fails(condition: str, caplog: pytest.LogCaptureFixture) -> None:
    assert evaluate_tag_rules([{"when": {"hdr": condition}, "tag": "NOPE"}], CONTEXT) is None
    assert "no value to compare against" in caplog.text


def test_composite_hdr_does_not_match_plain_dv() -> None:
    """DV titles carry a base-layer suffix, which the docs tell users to list explicitly."""
    context = {**CONTEXT, "hdr": "DV.HDR10P"}
    assert evaluate_tag_rules([{"when": {"hdr": "DV"}, "tag": "NOPE"}], context) is None
    assert evaluate_tag_rules([{"when": {"hdr": ["DV", "DV.HDR10P"]}, "tag": "DOLBY"}], context) == "DOLBY"
