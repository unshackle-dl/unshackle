from types import SimpleNamespace

from langcodes import Language

from unshackle.core.utilities import apply_original_language


class FakeTracks(SimpleNamespace):
    def __iter__(self):
        return iter([*self.videos, *self.audio])


def track(lang, original=False):
    return SimpleNamespace(language=Language.get(lang) if lang else None, is_original_lang=original)


def title(language, videos=(), audio=()):
    return SimpleNamespace(
        language=Language.get(language) if language else None,
        tracks=FakeTracks(videos=list(videos), audio=list(audio)),
    )


def test_overrides_the_service_guess_and_its_videos():
    t = title("es", videos=[track("es"), track("es")])
    apply_original_language(t, Language.get("it"))
    assert str(t.language) == "it"
    assert [str(v.language) for v in t.tracks.videos] == ["it", "it"]


def test_videos_in_another_language_keep_it():
    t = title("es", videos=[track("es"), track("ja"), track(None)])
    apply_original_language(t, Language.get("it"))
    assert [v.language and str(v.language) for v in t.tracks.videos] == ["it", "ja", None]


def test_no_service_language_keeps_video_tags():
    t = title(None, videos=[track("en")])
    apply_original_language(t, Language.get("it"))
    assert str(t.language) == "it"
    assert str(t.tracks.videos[0].language) == "en"


def test_original_flag_moves_to_the_enrich_language():
    t = title("es", audio=[track("es-419", original=True), track("it-IT")])
    apply_original_language(t, Language.get("it"))
    assert [a.is_original_lang for a in t.tracks.audio] == [False, True]


def test_same_language_leaves_flags_alone():
    t = title("it", videos=[track(None, original=True)])
    apply_original_language(t, Language.get("it"))
    assert t.tracks.videos[0].is_original_lang is True
