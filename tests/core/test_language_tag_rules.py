"""Rules accept both the scalar and the list form of every condition."""

import pytest
from langcodes import Language

from unshackle.core.utils.language_tags import evaluate_language_tag

JA = [Language.get("ja")]
EN_ES = [Language.get("en-US"), Language.get("es")]


@pytest.mark.parametrize("audio, subs", [("ja", "en"), (["ja"], ["en"])])
def test_scalar_and_list_forms_both_match(audio, subs) -> None:
    rules = [{"audio": audio, "subs_contain": subs, "tag": "SUBBED"}]
    assert evaluate_language_tag(rules, JA, EN_ES) == "SUBBED"


def test_a_list_matches_any_of_its_entries() -> None:
    rules = [{"audio": ["ko", "ja"], "tag": "ASIAN"}]
    assert evaluate_language_tag(rules, JA, []) == "ASIAN"


def test_subs_contain_all_needs_every_entry() -> None:
    rules = [{"subs_contain_all": ["en", "fr"], "tag": "MULTI"}]
    assert evaluate_language_tag(rules, JA, EN_ES) == ""


@pytest.mark.parametrize("bad", ["japanese", 1, [["ja"]], {"ja": 1}])
def test_an_invalid_rule_value_is_no_match_not_a_crash(bad, caplog) -> None:
    rules = [{"audio": bad, "tag": "BAD"}, {"audio": "ja", "tag": "OK"}]
    assert evaluate_language_tag(rules, JA, EN_ES) == "OK"
    assert "not a valid language tag" in caplog.text


def test_dubbed_separates_a_localized_dub_from_a_native_release() -> None:
    rules = [{"audio": "th", "dubbed": True, "tag": "THAIDUB"}, {"audio": "th", "tag": "THAI"}]
    th = [Language.get("th")]
    assert evaluate_language_tag(rules, th, [], {"dubbed": True}) == "THAIDUB"
    assert evaluate_language_tag(rules, th, [], {"dubbed": False}) == "THAI"


@pytest.mark.parametrize("state", ["dual", "multi", "dubbed"])
def test_every_state_condition_matches_both_ways(state) -> None:
    rules = [{state: True, "tag": "ON"}, {state: False, "tag": "OFF"}]
    assert evaluate_language_tag(rules, JA, [], {state: True}) == "ON"
    assert evaluate_language_tag(rules, JA, [], {state: False}) == "OFF"
    assert evaluate_language_tag(rules, JA, [], None) == "OFF"


def test_a_state_condition_combines_with_a_language() -> None:
    rules = [{"audio": "th", "dual": True, "tag": "THAIDUAL"}, {"dual": True, "tag": "DUAL"}]
    states = {"dual": True}
    assert evaluate_language_tag(rules, [Language.get("th")], [], states) == "THAIDUAL"
    assert evaluate_language_tag(rules, JA, [], states) == "DUAL"


@pytest.mark.parametrize("bad", ["yes", "false", 1])
def test_a_non_boolean_state_value_skips_the_rule(bad, caplog) -> None:
    rules = [{"dubbed": bad, "tag": "BAD"}, {"audio": "ja", "tag": "OK"}]
    assert evaluate_language_tag(rules, JA, [], {"dubbed": True}) == "OK"
    assert evaluate_language_tag(rules, JA, [], {"dubbed": False}) == "OK"
    assert "must be true or false" in caplog.text


@pytest.mark.parametrize("typo", ["dubed", "subs_contains"])
def test_a_typo_in_a_condition_name_skips_the_rule(typo, caplog) -> None:
    rules = [{"audio": "ja", typo: True, "tag": "BAD"}, {"audio": "ja", "tag": "OK"}]
    assert evaluate_language_tag(rules, JA, [], {"dubbed": True}) == "OK"
    assert f"unknown key(s) {typo}" in caplog.text


@pytest.mark.parametrize("rules", ["notalist", ["foo"], {"audio": "ja"}, [None]])
def test_a_malformed_rules_block_is_ignored_not_a_crash(rules) -> None:
    assert evaluate_language_tag(rules, JA, []) == ""
