from langcodes import Language

from unshackle.core.tracks import Audio, Tracks


def audio(lang: str, bitrate: int, id_: str) -> Audio:
    return Audio(url=f"http://x/{id_}", id_=id_, language=lang, codec=Audio.Codec.AAC, bitrate=bitrate, channels=2.0)


def test_naming_a_track_keeps_its_region() -> None:
    track = audio("fr-FR", 128000, "a1")
    assert track.name is None
    assert track.language.territory == "FR"
    assert Language.get("fr-FR").territory == "FR"


def test_a_regional_request_picks_its_own_variant_over_a_higher_bitrate_sibling() -> None:
    tracks = [audio("fr-FR", 128000, "a1"), audio("fr-CA", 192000, "a2")]
    assert [str(t.language) for t in Tracks.by_language(tracks, ["fr-FR"])] == ["fr-FR"]
