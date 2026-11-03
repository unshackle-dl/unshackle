"""DASH.resolve_representation must continue a track into a later Period without crossing codec, size or language."""

from __future__ import annotations

from typing import Any

import pytest

from tests.core.test_manifest_corpus import CORPUS, INDEX, KNOWN_FAILURES, parse
from unshackle.core.manifests import DASH
from unshackle.core.utils.xml import load_xml

RENUMBERED = b"""<MPD type="static" mediaPresentationDuration="PT20S">
  <Period id="0" duration="PT10S">
    <AdaptationSet id="0" contentType="text" lang="en">
      <Representation id="0" bandwidth="1000" codecs="wvtt" mimeType="application/mp4"/>
    </AdaptationSet>
    <AdaptationSet id="1" contentType="video">
      <Representation id="1" bandwidth="2000000" codecs="avc1" width="720" height="540"/>
      <Representation id="2" bandwidth="500000" codecs="avc1" width="640" height="480"/>
    </AdaptationSet>
    <AdaptationSet id="2" contentType="audio" lang="en">
      <Representation id="3" bandwidth="128000" codecs="mp4a.40.2" audioSamplingRate="48000"/>
    </AdaptationSet>
  </Period>
  <Period id="1" duration="PT10S">
    <AdaptationSet id="0" contentType="audio" lang="en">
      <Representation id="0" bandwidth="129000" codecs="mp4a.40.2" audioSamplingRate="48000"/>
    </AdaptationSet>
    <AdaptationSet id="1" contentType="video">
      <Representation id="1" bandwidth="480000" codecs="avc1" width="640" height="480"/>
      <Representation id="2" bandwidth="1900000" codecs="avc1" width="720" height="540"/>
      <Representation id="3" bandwidth="3000000" codecs="avc1" width="720" height="540"/>
    </AdaptationSet>
  </Period>
</MPD>"""


def video(rep_id: str, bandwidth: str, codecs: str = "avc1.640028") -> str:
    return f'<Representation id="{rep_id}" bandwidth="{bandwidth}" codecs="{codecs}" width="1920" height="1080" mimeType="video/mp4"/>'


def audio(rep_id: str) -> str:
    return f'<Representation id="{rep_id}" bandwidth="128000" codecs="mp4a.40.2" audioSamplingRate="48000" mimeType="audio/mp4"/>'


def two_periods(first: str, second: str) -> bytes:
    return f'<MPD><Period id="0">{first}</Period><Period id="1">{second}</Period></MPD>'.encode()


def resolve(mpd: bytes, set_index: int, rep_id: str) -> tuple[str | None, str | None, str | None] | None:
    p0, p1 = load_xml(mpd).findall("Period")
    found = DASH.resolve_representation(p1, rep_id, p0.findall("AdaptationSet")[set_index])
    if found is None:
        return None
    adaptation_set, rep = found
    return adaptation_set.get("lang"), rep.get("id"), DASH.get_attr("codecs", adaptation_set, rep)


def test_renumbered_audio_continues_into_audio_not_a_video_rep_with_its_id():
    assert resolve(RENUMBERED, 2, "3") == ("en", "0", "mp4a.40.2")


def test_renumbered_video_continues_into_the_same_size():
    assert resolve(RENUMBERED, 1, "1") == (None, "2", "avc1")
    assert resolve(RENUMBERED, 1, "2") == (None, "1", "avc1")


def test_track_missing_from_the_period_is_not_matched_to_another_type():
    assert resolve(RENUMBERED, 0, "0") is None


def test_untyped_sets_that_swap_order_keep_audio_on_audio():
    mpd = two_periods(
        f"<AdaptationSet>{video('v', '5000000')}</AdaptationSet><AdaptationSet>{audio('a')}</AdaptationSet>",
        f"<AdaptationSet>{audio('a')}</AdaptationSet><AdaptationSet>{video('v', '5000000')}</AdaptationSet>",
    )
    assert resolve(mpd, 1, "a") == (None, "a", "mp4a.40.2")


def test_codec_missing_from_the_period_is_skipped_not_swapped():
    mpd = two_periods(
        f'<AdaptationSet contentType="video">{video("avc", "5000000")}</AdaptationSet>'
        f'<AdaptationSet contentType="video">{video("hevc", "3000000", "hvc1.2.4.L150")}</AdaptationSet>',
        f'<AdaptationSet contentType="video">{video("avc", "5000000")}</AdaptationSet>',
    )
    assert resolve(mpd, 1, "hevc") is None


def test_renumbered_sets_that_swap_order_keep_the_codec():
    mpd = two_periods(
        f'<AdaptationSet id="0" contentType="video">{video("0", "8000000", "hvc1.2.4.L150")}</AdaptationSet>'
        f'<AdaptationSet id="1" contentType="video">{video("1", "8000000", "dvh1.08.06")}</AdaptationSet>',
        f'<AdaptationSet id="0" contentType="video">{video("0", "8000000", "dvh1.08.06")}</AdaptationSet>'
        f'<AdaptationSet id="1" contentType="video">{video("1", "8000000", "hvc1.2.4.L150")}</AdaptationSet>',
    )
    assert resolve(mpd, 1, "1") == (None, "0", "dvh1.08.06")


@pytest.mark.parametrize("content", ["audio", "text"])
def test_language_missing_from_the_period_is_skipped_not_swapped(content: str):
    def rep(rep_id: str) -> str:
        return (
            audio(rep_id) if content == "audio" else f'<Representation id="{rep_id}" bandwidth="1000" codecs="wvtt"/>'
        )

    mpd = two_periods(
        f'<AdaptationSet id="1" contentType="{content}" lang="en">{rep("x")}</AdaptationSet>',
        f'<AdaptationSet id="2" contentType="{content}" lang="de">{rep("y")}</AdaptationSet>',
    )
    assert resolve(mpd, 0, "x") is None


def test_regional_tag_continues_into_the_same_language():
    mpd = two_periods(
        f'<AdaptationSet id="1" contentType="audio" lang="en">{audio("a")}</AdaptationSet>',
        f'<AdaptationSet id="5" contentType="audio" lang="en-US">{audio("b")}</AdaptationSet>',
    )
    assert resolve(mpd, 0, "a") == ("en-US", "b", "mp4a.40.2")


def test_same_identity_sets_that_swap_order_follow_the_rep_id():
    dialog = f'<AdaptationSet contentType="audio" lang="en">{audio("dialog")}</AdaptationSet>'
    described = f'<AdaptationSet contentType="audio" lang="en">{audio("ad")}</AdaptationSet>'
    assert resolve(two_periods(dialog + described, described + dialog), 0, "dialog") == ("en", "dialog", "mp4a.40.2")


def test_stable_ladder_follows_bandwidth_rank_when_every_rung_drifts():
    mpd = two_periods(
        f'<AdaptationSet id="1" contentType="video">{video("v1", "5000000")}{video("v2", "6000000")}</AdaptationSet>',
        f'<AdaptationSet id="1" contentType="video">{video("v1", "3000000")}{video("v2", "4500000")}</AdaptationSet>',
    )
    assert resolve(mpd, 0, "v1") == (None, "v1", "avc1.640028")
    assert resolve(mpd, 0, "v2") == (None, "v2", "avc1.640028")


def test_non_integer_bandwidth_does_not_raise():
    mpd = two_periods(
        f'<AdaptationSet id="1" contentType="video">{video("v1", "5000000")}</AdaptationSet>',
        f'<AdaptationSet id="1" contentType="video">{video("v1", "5e6")}</AdaptationSet>',
    )
    assert resolve(mpd, 0, "v1") == (None, "v1", "avc1.640028")


def multi_period(entry: dict[str, Any]) -> bool:
    periods = load_xml((CORPUS / entry["file"]).read_bytes()).findall("Period")
    return sum(DASH.is_content_period(p, []) for p in periods) > 1


@pytest.mark.parametrize(
    "entry",
    [e for e in INDEX if e["kind"] == "dash" and e["file"] not in KNOWN_FAILURES and multi_period(e)],
    ids=lambda e: e["file"],
)
def test_corpus_matches_never_cross_codec_or_size(entry: dict[str, Any]):
    shape = ("codecs", "width", "height")
    for track in parse(entry):
        data = track.data["dash"]
        want = tuple(DASH.get_attr(a, data["adaptation_set"], data["representation"]) for a in shape)
        for period in data["manifest"].findall("Period"):
            if period is data["period"] or not DASH.is_content_period(period, []):
                continue
            found = DASH.resolve_representation(period, data["representation_id"], data["adaptation_set"])
            if found is not None:
                assert tuple(DASH.get_attr(a, *found) for a in shape) == want
