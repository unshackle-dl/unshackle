from __future__ import annotations

import pytest

from unshackle.core.utilities import is_close_match, is_exact_match


@pytest.mark.parametrize(
    "needle,haystack,expected",
    [
        ("en", ["en"], True),
        ("fr", ["en", "de"], False),
        ("es", ["es-419"], True),
        ("es", ["es-ES"], True),
        ("es-419", ["es"], True),
        ("en", ["en-US"], True),
        ("en-US", ["en-GB"], True),
        ("EN", ["en"], True),
        ("en", ["EN"], True),
        ("ja", ["ko"], False),
        ("fil", ["en", "fr", "de"], False),
        ("en", [], False),
        ("en", [None, "en"], True),
        ("en", [None], False),
        ("zh", ["zh-Hans"], True),
        ("zh-CN", ["zh-Hans"], True),
        ("zh-Hans", ["zh-CN"], True),
        ("zh-TW", ["zh-Hant"], True),
        ("zh-Hant", ["zh-TW"], True),
        ("zh", ["zh-Hant"], False),
        ("zh-Hans", ["zh-Hant"], False),
        ("zh-CN", ["zh-TW"], False),
        ("zh-HK", ["zh-Hant"], True),
        ("zh", ["cmn"], True),
        ("cmn", ["zh"], True),
        ("zh", ["yue"], False),
        ("yue", ["zh-HK"], False),
        ("fil", ["tl"], True),
        ("tl", ["fil"], True),
        ("fil", ["tgl"], True),
        ("tgl", ["fil"], True),
        ("fil", ["fil-PH"], True),
        ("tl", ["fil-PH"], True),
    ],
)
def test_is_close_match(needle, haystack, expected):
    assert is_close_match(needle, haystack) is expected


@pytest.mark.parametrize(
    "needle,haystack,expected",
    [
        ("es", ["es-419"], False),
        ("es-419", ["es"], False),
        ("es-419", ["es-419"], True),
        ("en-US", ["en-GB"], False),
        ("en-US", ["en-US"], True),
        ("en", ["en"], True),
        ("EN", ["en"], True),
        ("fr", ["de"], False),
        ("fil", ["en"], False),
        ("en", [], False),
        ("zh", ["zh-Hans"], True),
        ("zh-CN", ["zh-Hans"], True),
        ("zh-TW", ["zh-Hant"], True),
        ("zh", ["cmn"], True),
        ("zh-HK", ["zh-Hant"], False),
        ("zh-Hans", ["zh-Hant"], False),
        ("zh-CN", ["zh-TW"], False),
        ("fil", ["tl"], True),
        ("tl", ["fil"], True),
        ("fil", ["tgl"], True),
        ("fil", ["fil-PH"], True),
        ("tl", ["fil-PH"], True),
    ],
)
def test_is_exact_match(needle, haystack, expected):
    assert is_exact_match(needle, haystack) is expected
