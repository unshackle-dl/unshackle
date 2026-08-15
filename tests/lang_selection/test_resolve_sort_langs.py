from __future__ import annotations

from typing import Optional, Sequence

import pytest
from langcodes import Language

from unshackle.core.utilities import resolve_sort_langs


@pytest.mark.parametrize(
    ("tokens", "original", "expected"),
    [
        (["orig", "en", "es"], "ja", ["ja", "en", "es"]),
        (["orig", "en", "es"], None, ["en", "es"]),
        (["orig"], Language.get("pt-BR"), ["pt-BR"]),
        (["en", "orig"], "en", ["en"]),
        (["en", "es", "en", "fr"], None, ["en", "es", "fr"]),
        (["all", "best", "en"], "ja", ["all", "best", "en"]),
        ([], "ja", []),
    ],
)
def test_resolve_sort_langs(tokens: Sequence[str], original: Optional[str], expected: list[str]) -> None:
    assert resolve_sort_langs(tokens, original) == expected
