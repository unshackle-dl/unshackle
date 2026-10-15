from __future__ import annotations

from typing import Sequence

import pytest

from unshackle.core.utilities import ServiceTag, matching_languages

POOL = ["ar", "en", "en-GB", "en-US", "ja"]


@pytest.mark.parametrize(
    ("language", "available", "exact", "expected"),
    [
        # a bare tag takes the variant it means, and every regional form only when that is missing
        ("en", POOL, False, {"en"}),
        ("en", ["en-US", "en-GB"], False, {"en-US"}),
        ("pt", ["pt-PT", "pt-BR"], False, {"pt-BR"}),
        ("es", ["es-419", "es-ES"], False, {"es-ES"}),
        ("es", ["es-419", "es-MX"], False, {"es-419", "es-MX"}),
        ("es", ["es-419", "es-MX"], True, set()),
        # CLDR rates en/en-US at distance 0, so exact mode leans on the RFC 4647 string preference
        ("en", POOL, True, {"en"}),
        ("en-GB", POOL, True, {"en-GB"}),
        # no string-equal tag, so exact mode falls back to the distance match
        ("zh", ["ar", "cmn", "ja"], True, {"cmn"}),
        # a region asks for that variant in either mode: never another region, a bare tag only
        # when it means the same variant
        ("en-US", POOL, False, {"en-US"}),
        ("en-US", ["en", "en-GB"], False, {"en"}),
        ("en-US", ["en-GB", "en-AU"], False, set()),
        ("es-419", ["es-419", "es-MX", "es-ES"], False, {"es-419"}),
        ("es-419", ["es-MX", "es-ES"], False, set()),
        ("es-419", ["es", "es-ES"], False, set()),
        ("pt-BR", ["pt", "pt-PT"], False, {"pt"}),
        ("pt-PT", ["pt", "pt-BR"], False, set()),
        # a script alone is not a region: zh-Hant means zh-TW, else any Traditional Chinese
        ("zh-Hant", ["zh-TW", "zh-HK", "zh-Hans"], False, {"zh-TW"}),
        ("zh-Hant", ["zh-HK", "zh-Hans"], False, {"zh-HK"}),
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


@pytest.mark.parametrize(
    ("title", "available", "expected"),
    [
        # the title's own variant beats the one a bare tag would mean
        ("fr-CA", ["fr-FR", "fr-CA"], {"fr-CA"}),
        ("en-GB", ["en-US", "en-GB"], {"en-GB"}),
        ("zh-HK", ["zh-TW", "zh-HK", "zh-CN"], {"zh-HK"}),
        ("sr-ME", ["sr", "sr-ME"], {"sr-ME"}),
        # without its own variant, it takes the close regional forms
        ("en-US", ["en-GB", "fr"], {"en-GB"}),
        ("zh-TW", ["zh-HK", "zh-CN"], {"zh-HK"}),
    ],
)
def test_a_service_tag_prefers_its_own_variant(title: str, available: Sequence[str], expected: set[str]) -> None:
    assert matching_languages(ServiceTag(title), available) == expected


def test_every_variant_returns_each_regional_form_of_a_bare_tag() -> None:
    assert matching_languages("es", ["es-419", "es-ES", "en"], every_variant=True) == {"es-419", "es-ES"}
    # a region still asks for one variant
    assert matching_languages("es-ES", ["es-419", "es-ES"], every_variant=True) == {"es-ES"}
