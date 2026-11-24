"""``strip_hearing_impaired`` fallback path (``tracks/subtitle.py``).

Pins the filter-subs fallback's warn-and-continue branch: an invalid subtitle file must
log a warning and return, not crash (it previously raised AttributeError via a
nonexistent ``self.log``).
"""

from __future__ import annotations

import pytest

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.tracks.subtitle import Subtitle

SRT_SAMPLE = """1
00:00:01,000 --> 00:00:02,000
Hello
"""


@pytest.fixture()
def srt_track(tmp_path):
    path = tmp_path / "sub.srt"
    path.write_text(SRT_SAMPLE, encoding="utf-8")
    sub = Subtitle(url="https://example.test/x", language="en", codec=Subtitle.Codec.SubRip)
    sub.path = path
    return sub


def test_invalid_subtitle_warns_and_continues(srt_track, monkeypatch, caplog):
    import unshackle.core.tracks.subtitle as subtitle_mod

    monkeypatch.setattr(binaries, "SubtitleEdit", None)
    monkeypatch.setattr(config, "subtitle", {"sdh_method": "auto"}, raising=False)

    def broken(*args, **kwargs):
        raise RuntimeError("subby unavailable")

    monkeypatch.setattr(subtitle_mod, "CommonIssuesFixer", broken)

    def invalid(path):
        raise OSError(f"{path} is not valid subtitle file")

    monkeypatch.setattr(subtitle_mod, "Subtitles", invalid)

    with caplog.at_level("WARNING", logger="Subtitle"):
        srt_track.strip_hearing_impaired()

    assert any("Failed to strip SDH" in r.message for r in caplog.records)


def test_other_oserror_still_raises(srt_track, monkeypatch):
    import unshackle.core.tracks.subtitle as subtitle_mod

    monkeypatch.setattr(binaries, "SubtitleEdit", None)
    monkeypatch.setattr(config, "subtitle", {"sdh_method": "auto"}, raising=False)

    def broken(*args, **kwargs):
        raise RuntimeError("subby unavailable")

    monkeypatch.setattr(subtitle_mod, "CommonIssuesFixer", broken)

    def denied(path):
        raise PermissionError("permission denied")

    monkeypatch.setattr(subtitle_mod, "Subtitles", denied)

    with pytest.raises(PermissionError):
        srt_track.strip_hearing_impaired()
