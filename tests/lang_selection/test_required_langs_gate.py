"""The dl.result call site of missing_required_langs, and inputs the unit tests skip.

The gate in dl.result is not extractable without executing the whole download loop, so
its wiring is pinned the same way test_required_langs pins download_manager: against the
source. The value inputs cover what the helper's own tests leave out — real tracks carry
langcodes.Language objects, not strings.
"""

from __future__ import annotations

import inspect

from langcodes import Language

from unshackle.commands.dl import dl
from unshackle.core.utilities import missing_required_langs


def test_language_objects_satisfy_a_requirement():
    # track.language is a langcodes.Language, never a str, at the real call site
    assert missing_required_langs(["en"], [Language.get("en")]) == []
    assert missing_required_langs(["en"], [Language.get("en-US")]) == []
    assert missing_required_langs(["en"], [Language.get("fr")]) == ["en"]


def test_orig_accepts_a_language_object_title_language():
    # title.language is a langcodes.Language at the call site, never a str
    assert missing_required_langs(["orig"], ["ja"], Language.get("ja")) == []
    assert missing_required_langs(["orig"], ["en"], Language.get("ja")) == ["ja"]


def test_requirement_checks_the_catalogue_not_the_selection():
    # -l best,-fr --require-audio en: the gate answers "does en exist", so the
    # exclusion of fr from the *selection* must not make en count as missing
    assert missing_required_langs(["en"], ["en", "ko", "ja", "fr"]) == []
    # requiring a language the selection excludes is still satisfied by the catalogue
    assert missing_required_langs(["fr"], ["en", "ko", "ja", "fr"]) == []


class TestGateWiring:
    """Pin the dl.result gate's shape; a silent rewire is otherwise invisible to tests."""

    source = inspect.getsource(dl.result)

    def test_gate_is_disabled_with_the_track_kind(self):
        # --no-audio / -V must not fail on a require_audio the download cannot honour
        assert "require_audio if keep_audio else []" in self.source
        assert "require_video if keep_videos else []" in self.source

    def test_gate_counts_audio_embedded_in_kept_video(self):
        assert "embedded_audio_langs(title.tracks.videos, keep_videos)" in self.source

    def test_gate_honours_exact_lang(self):
        assert "missing_required_langs(required, available, title.language, exact=exact_lang)" in self.source

    def test_gate_reads_tracks_before_the_keep_flags_empty_them(self):
        # the availability lists must be built before `if not keep_videos:` clears
        # title.tracks.videos, or embedded audio could never satisfy a requirement
        gate = self.source.index("missing_required_langs(required")
        reset = self.source.index("if not keep_videos:")
        assert gate < reset

    def test_require_and_exclude_clash_is_rejected_per_flag(self):
        # --require-audio en with -l all,-en can never be satisfied as the user
        # meant it, so it must die at parse time, against each flag's own
        # exclusion list (-vl/-al cascade from -l only when unset)
        assert "cannot be required and excluded at the same time" in self.source
        for pair in (
            '("--require-audio", require_audio, audio_excl)',
            '("--require-video", require_video, video_excl)',
            '("--require-subs", require_subs, s_lang_excl)',
        ):
            assert pair in self.source
