"""Regression: exact-lang must distinguish a base language from its regional variant.

Reproduced with `--exact-lang --lang ar,ar-EG` against a streaming manifest that carries a
distinct `ar` ("Arabic") and `ar-EG` ("Arabic (Egypt)") audio track. CLDR tag_distance measures
intelligibility, not tag identity: it rates a base language and its "paradigm" regional variant
as the same language at distance 0 (ar/ar-EG, en/en-US, pt/pt-BR). Plain distance matching then
resolved both requested tags to the same track, dropping one variant and appending the other
twice, which crashed Tracks() reconstruction.

In exact mode by_language follows RFC 4647 Lookup: it prefers the most specific (string-equal)
tag, so each tag routes to its own track. It still falls back to the fuzzy match for pure aliases
(zh matches cmn) and the lenient base-to-regional case (en finds en-US when only en-US exists).
Distances above 0 (es/es-419, fr/fr-CA) were never collapsed and stay strict.
"""

from __future__ import annotations

import pytest

from unshackle.core.tracks import Audio
from unshackle.core.tracks.tracks import Tracks
from unshackle.core.utilities import is_exact_match

# Base language vs regional variant that CLDR rates at distance 0 (paradigm locales). These are
# the pairs that collapse under naive exact matching.
COLLAPSING_PAIRS = [
    ("ar", "ar-EG"), ("en", "en-US"), ("pt", "pt-BR"), ("fr", "fr-FR"),
    ("es", "es-ES"), ("de", "de-DE"), ("it", "it-IT"),
]
# Pairs CLDR rates at distance >0, which exact matching already separates. Never collapsed.
# Includes two-regional splits (no bare base) as services more commonly ship them.
DISTINCT_PAIRS = [
    ("es", "es-419"), ("fr", "fr-CA"), ("fr-FR", "fr-CA"), ("pt-PT", "pt-BR"), ("es-ES", "es-419"),
    ("en-US", "en-GB"), ("de-DE", "de-AT"), ("it-IT", "it-CH"), ("es-419", "es-AR"), ("no", "nn"),
]

# A full real-world audio language set for one title, the kind of mix a streaming manifest ships,
# which must resolve 1:1 with no collision or loss. Tags are drawn from published streaming
# language references (Warner Bros. Discovery / Netflix) and include regional and script variants.
REAL_WORLD_AUDIO_LANGS = [
    "ar", "ar-EG", "ar-SA", "bn-IN", "cmn-TW", "cs", "da", "de", "de-AT", "de-CH", "el", "en",
    "en-GB", "en-AU", "es-419", "es-AR", "es-ES", "fi", "fr-CA", "fr-FR", "he", "hi", "hu", "id",
    "it", "it-CH", "ja", "ko", "nl", "no", "pl", "pt-BR", "pt-PT", "ro", "ru", "sk", "sr",
    "sr-Latn", "sv", "ta", "th", "tr", "uk", "vi", "yue", "zh-Hans", "zh-Hant",
]


def _mk(*tags):
    return [Audio("https://x", language=t, id_=t) for t in tags]


def _select(tags, pool, exact=True):
    return [(t.id, str(t.language)) for t in Tracks.by_language(pool, tags, per_language=1, exact_match=exact)]


@pytest.mark.parametrize("base,reg", COLLAPSING_PAIRS)
def test_collapsing_pairs_are_distance_zero(base, reg):
    # Root cause: the regional variant exact-matches the base request (CLDR paradigm data).
    assert is_exact_match(base, [reg]) is True


@pytest.mark.parametrize("base,reg", DISTINCT_PAIRS)
def test_distinct_pairs_are_not_distance_zero(base, reg):
    # Contrast: these never collapse, which is why the bug was language-specific.
    assert is_exact_match(base, [reg]) is False


@pytest.mark.parametrize("base,reg", COLLAPSING_PAIRS)
def test_exact_lang_selects_distinct_collapsing_variants(base, reg):
    # Both tracks present + both tags requested: each tag must route to its own track.
    assert _select([base, reg], _mk(base, reg)) == [(base, base), (reg, reg)]


@pytest.mark.parametrize("base,reg", DISTINCT_PAIRS)
def test_exact_lang_distinct_regionals_still_work(base, reg):
    assert _select([base, reg], _mk(base, reg)) == [(base, base), (reg, reg)]


def test_exact_lang_lenient_base_to_regional_fallback():
    # Only the regional present; a base request must still find it when CLDR rates them equal.
    assert _select(["en"], _mk("en-US")) == [("en-US", "en-US")]
    # Alias with no string-equal track must still resolve (zh -> cmn).
    assert _select(["zh"], _mk("cmn")) == [("cmn", "cmn")]


def test_exact_lang_stays_strict_for_nonzero_distance():
    # `es` does NOT accept `es-419` in exact mode (distance 5), but fuzzy mode does.
    assert _select(["es"], _mk("es-419")) == []
    assert _select(["es"], _mk("es-419"), exact=False) == [("es-419", "es-419")]


@pytest.mark.parametrize("base,reg", COLLAPSING_PAIRS)
def test_overlapping_tags_never_duplicate_in_fuzzy_mode(base, reg):
    # Non-exact still collapses base/regional, but must dedupe rather than return a track twice.
    selected = Tracks.by_language(_mk(base, reg), [base, reg], per_language=1, exact_match=False)
    assert len({t.id for t in selected}) == len(selected)


def test_exact_lang_three_way_base_and_two_regionals():
    # fr (base, dist 0 to fr-FR) + fr-FR + fr-CA (dist 4): all three must resolve distinctly.
    assert _select(["fr", "fr-FR", "fr-CA"], _mk("fr", "fr-FR", "fr-CA")) == [
        ("fr", "fr"),
        ("fr-FR", "fr-FR"),
        ("fr-CA", "fr-CA"),
    ]
    # Base request with no bare-tag track falls back to its paradigm regional, without stealing
    # the sibling regional (fr -> fr-FR, not fr-CA).
    assert _select(["fr", "fr-CA"], _mk("fr-FR", "fr-CA")) == [("fr-FR", "fr-FR"), ("fr-CA", "fr-CA")]


@pytest.mark.parametrize("tag", REAL_WORLD_AUDIO_LANGS)
def test_full_language_set_each_tag_resolves_to_itself(tag):
    # Against a full real-world language pool, every exact request must return its own track, so
    # that no tag captures another's: ar-EG must resolve to ar-EG, never to ar.
    pool = _mk(*REAL_WORLD_AUDIO_LANGS)
    assert _select([tag], pool) == [(tag, tag)]


def test_full_language_set_selects_every_track_once():
    pool = _mk(*REAL_WORLD_AUDIO_LANGS)
    selected = Tracks.by_language(pool, REAL_WORLD_AUDIO_LANGS, per_language=1, exact_match=True)
    assert len(selected) == len(REAL_WORLD_AUDIO_LANGS)
    assert {t.id for t in selected} == set(REAL_WORLD_AUDIO_LANGS)
