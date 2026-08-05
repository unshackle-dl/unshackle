from unshackle.core.tracks.subtitle import Subtitle


def test_strips_timestamp_map_keeps_cues():
    text = "WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:00:00:00.000\n\n00:00:22.690 --> 00:00:24.710\nHi\n"
    out = Subtitle.strip_webvtt_timestamp_map(text)
    assert "X-TIMESTAMP-MAP" not in out
    assert "00:00:22.690 --> 00:00:24.710" in out
    assert out.startswith("WEBVTT\n")


def test_strips_lowercase_timestamp_map():
    # SubtitleEdit honours the header case-insensitively, so the strip must too
    text = "WEBVTT\nx-timestamp-map=MPEGTS:900000,LOCAL:00:00:00.000\n\n00:00:22.690 --> 00:00:24.710\nHi\n"
    out = Subtitle.strip_webvtt_timestamp_map(text)
    assert "x-timestamp-map" not in out.lower()
    assert "00:00:22.690 --> 00:00:24.710" in out


def test_no_timestamp_map_is_untouched():
    text = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n"
    assert Subtitle.strip_webvtt_timestamp_map(text) == text
