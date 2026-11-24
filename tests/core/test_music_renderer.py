import pytest

from unshackle.core.music.renderer import MusicRenderer


@pytest.mark.parametrize(
    ("label", "hires"),
    [
        ("FLAC 24-bit/96kHz", True),
        ("24-bit / 96 kHz", True),
        ("FLAC 16-bit/88.2kHz", True),
        ("96k", True),
        ("FLAC 16-bit/44.1kHz", False),
        ("Lossless", False),
        ("256 kb/s AAC", False),
        ("MP3 320 kb/s", False),
        ("320kbps", False),
        ("AAC 96 kbit/s", False),
    ],
)
def test_is_hires_quality_reads_sample_rate_not_bitrate(label: str, hires: bool) -> None:
    assert MusicRenderer.is_hires_quality(label) is hires
