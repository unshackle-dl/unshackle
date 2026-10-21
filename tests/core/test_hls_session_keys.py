"""Key selection helpers of the HLS parser: session-data keys, CDM filtering and key choice."""

from __future__ import annotations

import base64
import json
from typing import Optional

import m3u8
import pytest

from unshackle.core.cdm.detect import cdm_type_stub
from unshackle.core.manifests.hls import HLS

pytestmark = pytest.mark.unit

WV_URN = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
PR_URN = "urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95"
PR_NAME = "com.microsoft.playready"
KEY_URI = "data:text/plain;base64,AAAA"


def _master(value: str, data_id: str = "com.apple.hls.keys") -> m3u8.M3U8:
    text = (
        "#EXTM3U\n"
        f'#EXT-X-SESSION-DATA:DATA-ID="{data_id}",VALUE="{value}"\n'
        "#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=640x360\n"
        "video.m3u8\n"
    )
    return m3u8.loads(text)


def _b64(items: list[dict]) -> str:
    return base64.b64encode(json.dumps(items).encode()).decode()


def _item(method: str, keyformat: Optional[str] = None) -> dict:
    item = {"method": method, "uri": KEY_URI}
    if keyformat:
        item["keyformat"] = keyformat
    return item


def _key(method: str, keyformat: Optional[str] = None) -> m3u8.model.Key:
    return m3u8.model.Key(method=method, base_uri="", uri=KEY_URI, keyformat=keyformat)


def test_session_data_playready_urn_key_is_kept() -> None:
    keys = HLS.parse_session_data_keys(_master(_b64([_item("SAMPLE-AES-CTR", PR_URN)])))
    assert [k.keyformat for k in keys] == [PR_URN]


@pytest.mark.parametrize(
    ("method", "keyformat"),
    [
        ("SAMPLE-AES-CTR", WV_URN),
        ("SAMPLE-AES-CTR", PR_NAME),
        ("SAMPLE-AES-CTR", PR_URN.upper()),
        ("AES-128", None),
        ("ISO-23001-7", None),
    ],
)
def test_session_data_supported_keys_are_kept(method: str, keyformat: Optional[str]) -> None:
    keys = HLS.parse_session_data_keys(_master(_b64([_item(method, keyformat)])))
    assert [k.keyformat for k in keys] == [keyformat]


def test_session_data_unknown_keyformat_is_dropped() -> None:
    assert HLS.parse_session_data_keys(_master(_b64([_item("SAMPLE-AES", "com.example.drm")]))) == []


def test_session_data_plain_json_value_is_parsed() -> None:
    manifest = _master("placeholder")
    # a quoted attribute cannot hold raw JSON quotes, so set the value after parsing
    manifest.session_data[0].value = json.dumps([_item("SAMPLE-AES-CTR", WV_URN)])
    keys = HLS.parse_session_data_keys(manifest)
    assert len(keys) == 1


def test_session_data_value_not_json_yields_nothing() -> None:
    assert HLS.parse_session_data_keys(_master("not json at all")) == []


def test_session_data_other_data_id_is_ignored() -> None:
    manifest = _master(_b64([_item("SAMPLE-AES-CTR", WV_URN)]), data_id="com.example.other")
    assert HLS.parse_session_data_keys(manifest) == []


@pytest.mark.parametrize("playready_keyformat", [PR_URN, PR_NAME])
def test_filter_keys_follows_cdm(playready_keyformat: str) -> None:
    wv = _key("SAMPLE-AES-CTR", WV_URN)
    pr = _key("SAMPLE-AES-CTR", playready_keyformat)
    assert HLS.filter_keys_for_cdm([wv, pr], cdm_type_stub("widevine")) == [wv]
    assert HLS.filter_keys_for_cdm([wv, pr], cdm_type_stub("playready")) == [pr]


def test_filter_keys_preference_wins_over_cdm() -> None:
    wv = _key("SAMPLE-AES-CTR", WV_URN)
    pr = _key("SAMPLE-AES-CTR", PR_URN)
    assert HLS.filter_keys_for_cdm([wv, pr], cdm_type_stub("widevine"), drm_preference="playready") == [pr]
    assert HLS.filter_keys_for_cdm([wv, pr], cdm_type_stub("playready"), drm_preference="wv") == [wv]


def test_filter_keys_unsatisfied_preference_falls_back_to_cdm() -> None:
    wv = _key("SAMPLE-AES-CTR", WV_URN)
    assert HLS.filter_keys_for_cdm([wv], cdm_type_stub("widevine"), drm_preference="playready") == [wv]


def test_get_supported_key_none_method_returns_none() -> None:
    assert HLS.get_supported_key([_key("NONE")]) is None


def test_get_supported_key_unsupported_only_raises() -> None:
    with pytest.raises(NotImplementedError, match="SAMPLE-AES"):
        HLS.get_supported_key([_key("SAMPLE-AES", "com.example.drm")])
