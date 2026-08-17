"""Tests for the `-` exclusion prefix on the language flags (-l, -vl, -al, -sl, -fsl)."""

from __future__ import annotations

import pytest
from langcodes import Language

from unshackle.core.utilities import excluded_language_tags, partition_exclusions
from unshackle.core.utils.click_types import LANGUAGE_RANGE


@pytest.mark.parametrize(
    "tokens,expected",
    [
        (["all", "-es"], (["all"], ["es"])),
        (["-es"], ([], ["es"])),
        (["en", "ja"], (["en", "ja"], [])),
        (["-es", "-fr", "en"], (["en"], ["es", "fr"])),
        ([], ([], [])),
        (None, ([], [])),
    ],
)
def test_partition_splits_on_the_leading_dash(tokens, expected):
    assert partition_exclusions(tokens) == expected


def test_partition_keeps_the_written_order_and_drops_repeats():
    assert partition_exclusions(["ja", "en", "ja", "-es", "-es"]) == (["ja", "en"], ["es"])


@pytest.mark.parametrize("token", ["-", " - ", "", "  "])
def test_partition_skips_tokens_with_no_value(token):
    assert partition_exclusions(["en", token]) == (["en"], [])


def test_partition_trims_whitespace_around_a_token():
    assert partition_exclusions([" en ", "- es "]) == (["en"], ["es"])


def test_partition_reads_the_tokens_language_range_produces():
    assert partition_exclusions(LANGUAGE_RANGE.convert("all,-es;-fr")) == (["all"], ["es", "fr"])


def test_close_match_excludes_the_regional_variants():
    assert excluded_language_tags(["es"], ["es-419", "es-ES", "en-US"]) == {"es-419", "es-ES"}


def test_exact_match_prefers_the_string_equal_tag():
    # same string preference as selection: an exact 'es' only takes 'es' when it exists
    assert excluded_language_tags(["es"], ["es", "es-ES", "es-419"], exact=True) == {"es"}


def test_exact_match_falls_back_to_the_paradigm_variant():
    # no literal 'es' available, so 'es' resolves the way an exact '-sl es' selection would
    assert excluded_language_tags(["es"], ["es-ES", "es-419"], exact=True) == {"es-ES"}


def test_exact_match_keeps_an_unnamed_regional_variant():
    assert excluded_language_tags(["es-ES"], ["es-ES", "es-419"], exact=True) == {"es-ES"}


def test_language_objects_are_accepted():
    assert excluded_language_tags(["es"], [Language.get("es")]) == {"es"}


def test_an_untagged_track_is_never_excluded():
    assert excluded_language_tags(["es"], [None, "en-US"]) == set()


def test_no_exclusions_excludes_nothing():
    assert excluded_language_tags([], ["es-419", "en-US"]) == set()


def test_an_unmatched_language_is_kept():
    assert excluded_language_tags(["es", "fr"], ["en-US", "ja-JP"]) == set()
