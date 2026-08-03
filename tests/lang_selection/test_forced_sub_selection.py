from __future__ import annotations

import pytest

from unshackle.core.utilities import keep_forced_subtitle


@pytest.mark.parametrize(
    "forced,language,forced_s_lang,exact,expected",
    [
        (False, "en", [], False, True),
        (False, "de", ["en"], False, True),
        (True, "en", [], False, False),
        (True, "en", ["en"], False, True),
        (True, "en-US", ["en"], False, True),
        (True, "de", ["en"], False, False),
        (True, "de", ["en", "de"], False, True),
        # CLDR rates paradigm-regional pairs (en/en-US) distance 0, so exact still matches
        (True, "en-US", ["en"], True, True),
        (True, "en", ["en"], True, True),
        (True, "es-419", ["es-ES"], True, False),
    ],
)
def test_keep_forced_subtitle(forced: bool, language: str, forced_s_lang: list[str], exact: bool, expected: bool):
    assert keep_forced_subtitle(forced, language, forced_s_lang, exact) is expected
