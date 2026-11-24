from __future__ import annotations

from typing import Optional

import pytest

from unshackle.core.utilities import as_requested


@pytest.mark.parametrize(
    "tokens,orig_token,expected",
    [
        (["en"], "en", "orig"),
        (["en", "ja"], "en", "orig, ja"),
        (["en"], None, "en"),
        (["ja"], "en", "ja"),
        (["en", "ja"], None, "en, ja"),
        ([], "en", ""),
    ],
)
def test_as_requested(tokens: list[str], orig_token: Optional[str], expected: str) -> None:
    assert as_requested(tokens, orig_token) == expected
