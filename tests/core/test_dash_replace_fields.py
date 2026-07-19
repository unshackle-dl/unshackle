"""Regression tests for DASH.replace_fields template substitution.

Covers the fast-path short-circuits (no ``$``, no ``%``) and the printf-style
``$Field%fmt$`` padding, so plain and padded templates both resolve correctly.
"""

from unshackle.core.manifests.dash import DASH


def test_plain_fields_substituted() -> None:
    url = "seg-$RepresentationID$-$Number$-$Time$.mp4"
    out = DASH.replace_fields(url, RepresentationID="video=6M", Number=5, Time=20000)
    assert out == "seg-video=6M-5-20000.mp4"


def test_no_placeholder_returns_unchanged() -> None:
    url = "https://cdn.example.com/init.mp4"
    assert DASH.replace_fields(url, Number=1, Time=0) is url


def test_printf_padding_applied() -> None:
    # the '%05d' padding path must still fire when a '%' spec is present
    url = "seg-$Number%05d$.mp4"
    assert DASH.replace_fields(url, Number=7) == "seg-00007.mp4"


def test_printf_hex_padding_applied() -> None:
    url = "seg-$Number%08x$.m4s"
    assert DASH.replace_fields(url, Number=255) == "seg-000000ff.m4s"


def test_mixed_plain_and_padded_fields() -> None:
    url = "$RepresentationID$/seg-$Number%04d$.mp4"
    out = DASH.replace_fields(url, RepresentationID="v1", Number=42)
    assert out == "v1/seg-0042.mp4"
