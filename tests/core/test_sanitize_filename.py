"""Combining marks and Unicode normalisation in sanitize_filename.

Thai, Devanagari, Bengali and other scripts write vowels and tone marks as combining marks on
a base letter. With ``unicode_filenames`` those marks are part of the word and must survive.
In both modes, composed and decomposed spellings of a title must give the same name.
Combining characters are written as escapes so an editor that normalises the file cannot
change what a case tests.
"""

from __future__ import annotations

import pytest

from unshackle.core.config import config
from unshackle.core.utilities import sanitize_filename

THAI = "\u0e40\u0e1b\u0e47\u0e19\u0e15\u0e48\u0e2d"  # a Thai title with vowel and tone marks


@pytest.mark.parametrize(
    "name",
    [
        THAI,
        "\u0e01\u0e31\u0e49\u0e19",  # Thai: a tone mark stacked on an upper vowel
        "\u0939\u093f\u0902\u0926\u0940",  # Devanagari
        "\u09b0\u200d\u09cd\u09af\u09be\u09ac",  # Bengali: the virama follows a zero width joiner
        "\u1040\u102d\u102f\u1004\u103a\u1038",  # Burmese: a mark on digit zero, typed for the letter wa
        "\u0e19\u0e49\u0e33",  # Thai sara am, which NFKC would split into two characters
    ],
)
def test_unicode_filenames_keep_marks(name: str) -> None:
    assert sanitize_filename(name, unicode=True) == name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Decomposed and composed spellings give the same name.
        ("Cafe\u0301", "Caf\u00e9"),
        ("Caf\u00e9", "Caf\u00e9"),
        ("Show \u2764\ufe0f", "Show.\u2764\ufe0f"),
        ("Some\x07Title", "SomeTitle"),
    ],
)
def test_unicode_filenames_use_nfc_and_drop_control_characters(name: str, expected: str) -> None:
    assert sanitize_filename(name, unicode=True) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Cafe\u0301 Name", "Cafe.Name"),
        ("Caf\u00e9 Name", "Cafe.Name"),
        # Katakana with separate voicing marks transliterates like the composed spelling.
        ("\u30ab\u3099\u30f3\u30bf\u3099\u30e0", "gandamu"),
        ("\u30ac\u30f3\u30c0\u30e0", "gandamu"),
        ("Some\x07Title", "SomeTitle"),
    ],
)
def test_ascii_filenames_transliterate_both_spellings(name: str, expected: str) -> None:
    assert sanitize_filename(name, unicode=False) == expected


def test_config_option_decides_when_unicode_is_not_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "unicode_filenames", True)
    assert sanitize_filename(THAI) == THAI
    monkeypatch.setattr(config, "unicode_filenames", False)
    assert sanitize_filename(THAI).isascii()


def test_sanitising_a_name_again_does_not_change_it() -> None:
    # The removed "?" sits between the letter and its mark.
    once = sanitize_filename("e?\u0301", unicode=True)
    assert once == "\u00e9"
    assert sanitize_filename(once, unicode=True) == once
