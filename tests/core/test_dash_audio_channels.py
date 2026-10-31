import xml.etree.ElementTree as ET
from typing import Optional

import pytest

from unshackle.core.manifests.dash import DASH

COUNT = "urn:mpeg:dash:23003:3:audio_channel_configuration:2011"
CICP = "urn:mpeg:mpegB:cicp:ChannelConfiguration"
DOLBY = "tag:dolby.com,2014:dash:audio_channel_configuration:2011"
DOLBY_LEGACY = "urn:dolby:dash:audio_channel_configuration:2011"


def config(*descriptors: tuple[str, str], tag: str = "AdaptationSet") -> ET.Element:
    children = "".join(f'<AudioChannelConfiguration schemeIdUri="{s}" value="{v}"/>' for s, v in descriptors)
    return ET.fromstring(f"<{tag}>{children}</{tag}>")


@pytest.mark.parametrize(
    ("scheme", "value", "expected"),
    [
        (COUNT, "2", 2.0),
        (COUNT, "6", 5.1),
        (COUNT, "7", 6.1),
        (COUNT, "8", 7.1),
        ("urn:dts:dash:audio_channel_configuration:2012", "6", 5.1),
        (CICP, "2", 2.0),
        (CICP, "6", 5.1),
        (CICP, "7", 7.1),
        (CICP, "8", 7.1),
        (CICP, "12", 7.1),
        (CICP, "16", "5.1.4"),
        (CICP, "17", "5.1.6"),
        (CICP, "18", "7.1.6"),
        (CICP, "19", "7.1.4"),
        (CICP, "20", "9.1.4"),
        (DOLBY, "F801", 5.1),
        (DOLBY_LEGACY, "FA01", 7.1),
        # an all-digit mask is still a mask under a Dolby scheme
        (DOLBY, "4000", 1.0),
    ],
)
def test_a_channel_value_is_decoded_by_its_scheme(scheme: str, value: str, expected: object) -> None:
    assert DASH.get_audio_channels(config((scheme, value)), None) == expected


@pytest.mark.parametrize(
    ("scheme", "value"),
    [
        ("tag:dolby.com,2015:dash:audio_channel_configuration:2015", "000047"),
        ("urn:mpeg:dash:outputChannelPositionList:2012", "L R C LFE"),
        (CICP, "13"),
        (COUNT, "x"),
        (DOLBY, "zz"),
    ],
)
def test_an_unknown_scheme_or_value_gives_no_channels(scheme: str, value: str) -> None:
    assert DASH.get_audio_channels(config((scheme, value)), None) is None


def test_the_first_decodable_descriptor_wins() -> None:
    element = config(("urn:example:unknown", "9"), (DOLBY, "F801"), (CICP, "2"))
    assert DASH.get_audio_channels(element, None) == 5.1


@pytest.mark.parametrize(
    ("representation", "expected"), [(config((COUNT, "6"), tag="Representation"), 5.1), (None, 2.0)]
)
def test_the_representation_overrides_the_adaptation_set(representation: Optional[ET.Element], expected: float) -> None:
    assert DASH.get_audio_channels(config((COUNT, "2")), representation) == expected
