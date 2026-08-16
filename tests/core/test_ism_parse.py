import pytest

from unshackle.core.manifests.ism import ISM
from unshackle.core.tracks import Audio

MANIFEST = """<?xml version="1.0"?>
<SmoothStreamingMedia MajorVersion="2" MinorVersion="0" Duration="60000000" TimeScale="10000000">
  <StreamIndex Type="audio" Name="audio" Language="en"
               Url="QualityLevels({Bitrate})/Fragments(audio={start_time})">
    <QualityLevel Index="0" Bitrate="128000" AudioTag="255" SamplingRate="48000"
                  Channels="2" BitsPerSample="16" CodecPrivateData="1190"/>
    <c t="0" d="20000000" r="2"/>
    <c/>
  </StreamIndex>
</SmoothStreamingMedia>
"""


def test_placeholder_spellings_repeat_and_audiotag_fallback() -> None:
    tracks = ISM.from_text(MANIFEST, "https://cdn.example/x.ism/manifest").to_tracks()
    (audio,) = tracks.audio
    segments = audio.data["ism"]["segments"]
    assert segments == [
        "https://cdn.example/x.ism/QualityLevels(128000)/Fragments(audio=0)",
        "https://cdn.example/x.ism/QualityLevels(128000)/Fragments(audio=20000000)",
        "https://cdn.example/x.ism/QualityLevels(128000)/Fragments(audio=40000000)",
    ]
    assert audio.codec == Audio.Codec.AAC  # AudioTag 255 means AACL


def test_duration_fallback_when_next_fragment_omits_time() -> None:
    # A d-less <c> whose successor also omits t must fall back to the manifest
    # duration (int(None) used to raise TypeError past the except clause).
    sparse = MANIFEST.replace('<c t="0" d="20000000" r="2"/>', '<c t="0"/>')
    tracks = ISM.from_text(sparse, "https://cdn.example/x.ism/manifest").to_tracks()
    assert tracks.audio[0].data["ism"]["segments"] == [
        "https://cdn.example/x.ism/QualityLevels(128000)/Fragments(audio=0)",
        "https://cdn.example/x.ism/QualityLevels(128000)/Fragments(audio=60000000)",
    ]


def test_missing_language_without_fallback_raises() -> None:
    # MS-SSTR Language is optional; video StreamIndexes commonly omit it.
    no_lang = MANIFEST.replace(' Language="en"', "")
    with pytest.raises(ValueError, match="fallback language"):
        ISM.from_text(no_lang, "https://cdn.example/x.ism/manifest").to_tracks()
    tracks = ISM.from_text(no_lang, "https://cdn.example/x.ism/manifest").to_tracks(language="en")
    assert str(tracks.audio[0].language) == "en"


def test_live_manifest_rejected() -> None:
    live = MANIFEST.replace('Duration="60000000"', 'Duration="60000000" IsLive="TRUE"', 1)
    with pytest.raises(ValueError, match="Live"):
        ISM.from_text(live, "https://cdn.example/x.ism/manifest").to_tracks()
