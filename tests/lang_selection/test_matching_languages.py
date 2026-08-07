from __future__ import annotations

from typing import Sequence

import pytest

from unshackle.core.utilities import matching_languages

POOL = ["ar", "en", "en-GB", "en-US", "ja"]


@pytest.mark.parametrize(
    ("language", "available", "exact", "expected"),
    [
        ("en", POOL, False, {"en", "en-GB", "en-US"}),
        # CLDR rates en/en-US at distance 0, so exact mode leans on the RFC 4647 string preference
        ("en", POOL, True, {"en"}),
        ("en-GB", POOL, True, {"en-GB"}),
        # no string-equal tag, so exact mode falls back to the distance match
        ("zh", ["ar", "cmn", "ja"], True, {"cmn"}),
        ("en", ["ar", "ja"], False, set()),
        ("en", [], False, set()),
        ("en", ["ar", None, "en"], True, {"en"}),
        # unparseable tags (config typos) must match nothing, never raise
        ("engrish", POOL, False, set()),
        ("engrish", POOL, True, set()),
        ("", POOL, False, set()),
        ("", POOL, True, set()),
        ("f", POOL, False, set()),
    ],
)
def test_matching_languages(language: str, available: Sequence[str], exact: bool, expected: set[str]) -> None:
    assert matching_languages(language, available, exact) == expected
