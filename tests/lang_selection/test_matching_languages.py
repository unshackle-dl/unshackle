from __future__ import annotations

from typing import Sequence

import pytest

from unshackle.core.utilities import matching_languages

POOL = ["ar", "en", "en-GB", "en-US", "ja"]


@pytest.mark.parametrize(
    ("language", "available", "exact", "expected"),
    [
        ("en", POOL, False, {"en", "en-GB", "en-US"}),
        ("en", POOL, True, {"en"}),
        ("en-GB", POOL, True, {"en-GB"}),
        ("zh", ["ar", "cmn", "ja"], True, {"cmn"}),
        ("en", ["ar", "ja"], False, set()),
        ("en", [], False, set()),
        ("en", ["ar", None, "en"], True, {"en"}),
        ("engrish", POOL, False, set()),
        ("engrish", POOL, True, set()),
        ("", POOL, False, set()),
        ("", POOL, True, set()),
        ("f", POOL, False, set()),
    ],
)
def test_matching_languages(language: str, available: Sequence[str], exact: bool, expected: set[str]) -> None:
    assert matching_languages(language, available, exact) == expected
