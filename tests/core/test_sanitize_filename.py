"""Nonspacing marks in sanitize_filename.

Thai (and Devanagari, Khmer, ...) write vowels and tone marks as nonspacing marks (Mn) on a
base letter. With ``unicode_filenames`` those marks are part of the word and must survive,
while hidden marks with no letter to sit on are still removed.
"""

from __future__ import annotations

import pytest

from unshackle.core.utilities import sanitize_filename


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Thai vowel and tone marks stay on their letters ("เป็นต่อ" used to become "เปนตอ").
        ("เป็นต่อ 2026", "เป็นต่อ.2026"),
        ("บัญชีดำอาชญากรรมซ่อนเงื่อน", "บัญชีดำอาชญากรรมซ่อนเงื่อน"),
        # Stacked marks: a tone mark on top of an upper vowel.
        ("กั้น", "กั้น"),
        ("हिंदी", "हिंदी"),
        # A decomposed accent keeps its letter's accent.
        ("Café", "Café"),
        # Hidden marks with nothing to attach to are still removed.
        ("́Title", "Title"),
        ("Title ́x", "Title.x"),
        # A variation selector on an emoji is not a letter mark.
        ("Show ❤️", "Show.❤"),
        # Control characters are always removed.
        ("Some\x07Title", "SomeTitle"),
    ],
)
def test_unicode_filenames_keep_marks_on_letters(name: str, expected: str) -> None:
    assert sanitize_filename(name, unicode=True) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Café Name", "Cafe.Name"),
        ("Some\x07Title", "SomeTitle"),
    ],
)
def test_ascii_filenames_drop_all_marks(name: str, expected: str) -> None:
    assert sanitize_filename(name, unicode=False) == expected
