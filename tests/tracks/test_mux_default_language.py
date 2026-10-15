from __future__ import annotations

from typing import Optional

import pytest

from unshackle.core.tracks import Audio, Tracks


@pytest.mark.parametrize(
    ("languages", "preferred", "expected"),
    [
        (["en-GB", "en-US"], "en-US", 1),
        # matched against every track at once, so the string-equal tag wins over a bare en
        (["en", "en-US"], "en-US", 1),
        (["en-GB", "fr"], "en-US", None),
        (["fr", "en-GB"], "en", 1),
        (["en-US"], None, None),
    ],
)
def test_first_match_index(languages: list[str], preferred: Optional[str], expected: Optional[int]) -> None:
    tracks = [
        Audio(id_=f"a{i}", url=f"https://x/{i}", language=lang, codec=Audio.Codec.AAC, bitrate=1)
        for i, lang in enumerate(languages)
    ]
    assert Tracks.first_match_index(tracks, preferred) == expected
