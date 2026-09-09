import xml.etree.ElementTree as ET

import pytest

from unshackle.core.manifests.dash import DASH


def adaptation_set(scheme: str, value: str) -> ET.Element:
    return ET.fromstring(f'<AdaptationSet><Accessibility schemeIdUri="{scheme}" value="{value}"/></AdaptationSet>')


@pytest.mark.parametrize(
    "scheme,value",
    [
        # "description" is the value the DASH role scheme defines. Stan writes it, and reading only
        # "descriptive" left every audio-description track unflagged.
        ("urn:mpeg:dash:role:2011", "description"),
        ("urn:mpeg:dash:role:2011", "descriptive"),
        ("urn:tva:metadata:cs:AudioPurposeCS:2007", "1"),
    ],
)
def test_descriptive_accessibility_values(scheme: str, value: str) -> None:
    assert DASH.is_descriptive(adaptation_set(scheme, value)) is True


@pytest.mark.parametrize(
    "scheme,value",
    [
        ("urn:mpeg:dash:role:2011", "main"),
        ("urn:mpeg:dash:role:2011", "caption"),
        ("urn:tva:metadata:cs:AudioPurposeCS:2007", "2"),
    ],
)
def test_non_descriptive_accessibility_values(scheme: str, value: str) -> None:
    assert DASH.is_descriptive(adaptation_set(scheme, value)) is False


def test_no_accessibility_element() -> None:
    assert DASH.is_descriptive(ET.fromstring("<AdaptationSet/>")) is False
