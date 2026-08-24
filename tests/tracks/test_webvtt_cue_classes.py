from unshackle.core.tracks.subtitle import Subtitle


def test_strip_webvtt_cue_classes():
    text = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:04.000\n"
        "<c.bg_transparent>Mor, da vi blev gift,</c>\n"
        "<c>kom Ben</c> <c.a.b>og jeg</c.a.b> til dig.\n"
        "<i>Keep</i> <b>these</b> and <code>this</code>\n"
        "<i.loud>class</i> <b.x>on</b> <u.y>styling</u>\n"
        "<v.z Bob>voice</v> <lang ja>jp</lang> <ruby>\u6f22<rt>\u304b\u3093</rt></ruby>\n"
        "kara <00:00:01.500>timing <00:01.500>short\n"
    )
    out = Subtitle.strip_webvtt_cue_classes(text)
    assert "bg_transparent" not in out and "</c>" not in out and "<c>" not in out
    assert "Mor, da vi blev gift," in out
    assert "<i>Keep</i> <b>these</b> and <code>this</code>" in out
    # class list dropped, the styling tag itself kept
    assert "<i>class</i> <b>on</b> <u>styling</u>" in out
    # WebVTT-only tags go, their text stays
    assert "voice jp \u6f22\u304b\u3093" in out
    assert "kara timing short" in out


def test_decode_entities(tmp_path):
    from unshackle.core.tracks.subtitle_convert import decode_entities

    out = tmp_path / "a.srt"
    out.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\nROBERT &amp; RA'ED &nbsp;x &#39;q&#39;\nTom &not sure, AT&T\n",
        encoding="utf8",
    )
    decode_entities(out)
    text = out.read_text(encoding="utf8")
    assert "ROBERT & RA'ED \xa0x 'q'" in text
    # no-semicolon legacy forms stay as written
    assert "Tom &not sure, AT&T" in text


def test_filter_subs_converts_before_stripping(tmp_path, monkeypatch):
    """``sdh_method: filter-subs`` used to hand subtitle-filter a .vtt and raise OSError."""
    from unshackle.core import binaries
    from unshackle.core.config import config

    monkeypatch.setattr(binaries, "SubtitleEdit", None)
    monkeypatch.setattr(config, "subtitle", {"sdh_method": "filter-subs"}, raising=False)

    path = tmp_path / "sub.vtt"
    path.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n[MUSIC PLAYING]\n\n"
        "00:00:04.000 --> 00:00:06.000\nPlain dialogue stays.\n",
        encoding="utf8",
    )
    sub = Subtitle(url="https://example.test/x", language="en", codec=Subtitle.Codec.WebVTT)
    sub.path = path
    sub.strip_hearing_impaired()

    assert sub.codec == Subtitle.Codec.SubRip
    text = sub.path.read_text(encoding="utf8")
    assert "MUSIC PLAYING" not in text
    assert "Plain dialogue stays." in text
