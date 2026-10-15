from __future__ import annotations

from typing import Optional, Sequence

import pytest
from langcodes import Language

from unshackle.core.utilities import ServiceTag, resolve_sort_langs


@pytest.mark.parametrize(
    ("tokens", "original", "expected"),
    [
        (["orig", "en", "es"], "ja", ["ja", "en", "es"]),
        (["orig", "en", "es"], None, ["en", "es"]),
        # orig keeps the title's full tag; matching_languages handles its fallback
        (["orig"], Language.get("pt-BR"), ["pt-BR"]),
        (["orig"], "zh-Hant-TW", ["zh-Hant-TW"]),
        (["en", "orig"], "en", ["en"]),
        (["en", "es", "en", "fr"], None, ["en", "es", "fr"]),
        (["all", "best", "en"], "ja", ["all", "best", "en"]),
        ([], "ja", []),
    ],
)
def test_resolve_sort_langs(tokens: Sequence[str], original: Optional[str], expected: list[str]) -> None:
    assert resolve_sort_langs(tokens, original) == expected


def test_orig_is_a_service_tag_outside_exact_mode() -> None:
    assert isinstance(resolve_sort_langs(["orig"], "pt-BR")[0], ServiceTag)
    assert not isinstance(resolve_sort_langs(["orig"], "pt-BR", exact=True)[0], ServiceTag)
