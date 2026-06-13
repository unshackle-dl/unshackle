from __future__ import annotations

import pytest

from unshackle.core.utilities import find_missing_langs


@pytest.mark.parametrize(
    "requested,available,expected",
    [
        (["en"], ["en"], []),
        (["en", "ja"], ["en", "ja", "fr"], []),
        (["en", "fil"], ["en"], ["fil"]),
        (["en", "ko", "ja"], ["en"], ["ko", "ja"]),
        (["fil"], ["en", "ja"], ["fil"]),
        (["fil", "ko"], ["en"], ["fil", "ko"]),
        (["es"], ["es-419"], []),
        (["es-419"], ["es"], []),
        (["en"], ["en-US"], []),
        (["all"], [], []),
        (["best"], [], []),
        (["orig"], [], []),
        (["all", "en"], ["en"], []),
        (["best", "fil"], ["en"], ["fil"]),
        ([], ["en"], []),
        (["en"], [], ["en"]),
        (["en"], [None, "en"], []),
        (["en"], [None], ["en"]),
        (["zh"], ["zh-Hans"], []),
        (["zh-CN"], ["zh-Hans"], []),
        (["zh-Hans"], ["zh-CN"], []),
        (["zh-TW"], ["zh-Hant"], []),
        (["zh-Hant"], ["zh-TW"], []),
        (["zh"], ["zh-Hant"], ["zh"]),
        (["zh-Hans"], ["zh-Hant"], ["zh-Hans"]),
        (["zh-CN"], ["zh-TW"], ["zh-CN"]),
        (["zh-HK"], ["zh-Hant"], []),
        (["zh"], ["cmn"], []),
        (["cmn"], ["zh"], []),
        (["zh"], ["yue"], ["zh"]),
        (["yue"], ["zh-HK"], ["yue"]),
        (["fil"], ["tl"], []),
        (["tl"], ["fil"], []),
        (["fil"], ["tgl"], []),
        (["tgl"], ["fil"], []),
        (["fil"], ["fil-PH"], []),
        (["tl"], ["fil-PH"], []),
    ],
)
def test_close_match(requested, available, expected):
    assert find_missing_langs(requested, available, exact=False) == expected


@pytest.mark.parametrize(
    "requested,available,expected",
    [
        (["es"], ["es-419"], ["es"]),
        (["es-419"], ["es"], ["es-419"]),
        (["en"], ["en-US"], []),
        (["en-US"], ["en"], []),
        (["en"], ["en-GB"], ["en"]),
        (["en-US"], ["en-GB"], ["en-US"]),
        (["en-US"], ["en-US"], []),
        (["en-US", "en-GB"], ["en-US"], ["en-GB"]),
        (["en"], ["en"], []),
        (["all", "es-419"], ["es"], ["es-419"]),
        (["zh"], ["zh-Hans"], []),
        (["zh-CN"], ["zh-Hans"], []),
        (["zh-TW"], ["zh-Hant"], []),
        (["zh"], ["cmn"], []),
        (["zh-HK"], ["zh-Hant"], ["zh-HK"]),
        (["zh-Hans"], ["zh-Hant"], ["zh-Hans"]),
        (["zh-CN"], ["zh-TW"], ["zh-CN"]),
        (["fil"], ["tl"], []),
        (["tl"], ["fil"], []),
        (["fil"], ["tgl"], []),
        (["fil"], ["fil-PH"], []),
        (["tl"], ["fil-PH"], []),
    ],
)
def test_exact_match(requested, available, expected):
    assert find_missing_langs(requested, available, exact=True) == expected


def test_order_preserved():
    assert find_missing_langs(["ja", "ko", "fr"], ["en"], exact=False) == ["ja", "ko", "fr"]


def test_duplicates_in_request():
    assert find_missing_langs(["fil", "fil", "en"], ["en"], exact=False) == ["fil", "fil"]


def test_zh_catalogue_simplified_only_misses_traditional():
    assert find_missing_langs(["zh-Hant", "zh-TW"], ["en", "zh-Hans"], exact=False) == ["zh-Hant", "zh-TW"]


def test_mixed_zh_fil_request():
    assert find_missing_langs(["en", "zh-Hans", "fil"], ["en", "tl"], exact=False) == ["zh-Hans"]


def test_zh_cn_request_with_tw_catalogue():
    assert find_missing_langs(["zh-CN"], ["zh-TW"], exact=False) == ["zh-CN"]
    assert find_missing_langs(["zh-CN"], ["zh-TW"], exact=True) == ["zh-CN"]
