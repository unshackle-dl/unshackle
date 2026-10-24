import xml.etree.ElementTree as ET

from unshackle.core.manifests.dash import DASH

CAPTION = '<Role schemeIdUri="urn:mpeg:dash:role:2011" value="caption"/>'
SDH = '<Accessibility schemeIdUri="urn:tva:metadata:cs:AudioPurposeCS:2007" value="2"/>'


def test_caption_role_is_closed_caption() -> None:
    element = ET.fromstring(f"<AdaptationSet>{CAPTION}</AdaptationSet>")
    assert DASH.is_closed_caption(element) is True
    assert DASH.is_sdh(element) is False


def test_caption_role_with_sdh_value_is_sdh_only() -> None:
    # a Subtitle cannot be both CC and SDH, so a set marked both used to fail the whole title
    element = ET.fromstring(f"<AdaptationSet>{CAPTION}{SDH}</AdaptationSet>")
    assert DASH.is_closed_caption(element) is False
    assert DASH.is_sdh(element) is True
