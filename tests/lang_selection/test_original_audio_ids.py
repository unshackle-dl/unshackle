from __future__ import annotations

from typing import Optional

import pytest

from unshackle.core.api.handlers import original_audio_ids


class FakeTrack:
    def __init__(self, language: str) -> None:
        self.id = language
        self.language = language


class FakeTitle:
    def __init__(self, language: Optional[str]) -> None:
        self.language = language


@pytest.mark.parametrize(
    "title_language,track_languages,expected",
    [
        # paradigm collapse: CLDR calls these the same language, RFC 4647 picks the specific one
        (["en"], ["en", "en-US"], ["en"]),
        (["en"], ["en-US", "en-GB"], ["en-US"]),
        (["pt"], ["pt-BR", "pt-PT"], ["pt-BR"]),
        (["es"], ["es-ES", "es-419"], ["es-ES"]),
        # non-paradigm regionals: exact mode drops them, so the fuzzy fallback catches
        # them and the flag still matches what a default 'orig' download selects
        (["es"], ["es-419"], ["es-419"]),
        (["fr"], ["fr-CA"], ["fr-CA"]),
        # alias
        (["zh"], ["cmn"], ["cmn"]),
        # nothing in the original language, and no language on the title at all
        (["en"], ["ja", "de"], []),
        ([None], ["en"], []),
    ],
)
def test_original_audio_ids(title_language: list, track_languages: list[str], expected: list[str]) -> None:
    tracks = [FakeTrack(t) for t in track_languages]
    ids = original_audio_ids(tracks, FakeTitle(title_language[0]))
    assert [t for t in track_languages if t in ids] == expected
