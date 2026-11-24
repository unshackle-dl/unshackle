from threading import Event
from typing import TYPE_CHECKING, TypeVar, Union

if TYPE_CHECKING:
    from unshackle.core.tracks import Audio, Subtitle, Track, Video

DOWNLOAD_CANCELLED = Event()
DOWNLOAD_LICENCE_ONLY = Event()

DEFAULT_SAMPLE = (120, 180)
DOWNLOAD_ALL_DRM = Event()


class DownloadCancelled(Exception):
    """A track stopped because ``DOWNLOAD_CANCELLED`` was set, not because it itself failed.

    The downloader raises this instead of returning short. A short return sends the track on
    to its merge or decrypt step, which then fails on the segments the cancel left behind.
    That fault hides the sibling failure that is the real error.
    """


DRM_SORT_MAP = ["ClearKey", "Widevine"]
LANGUAGE_MAX_DISTANCE = 5
LANGUAGE_EXACT_DISTANCE = 0
VIDEO_CODEC_MAP = {"AVC": "H.264", "HEVC": "H.265", "VVC": "H.266"}
DYNAMIC_RANGE_MAP = {
    "HDR10": "HDR",
    "HDR10+": "HDR10P",
    "Dolby Vision": "DV",
    "HDR10 / HDR10+": "HDR10P",
    "HDR10 / HDR10": "HDR",
    "HDR Vivid": "HDRVivid",
}
AUDIO_CODEC_MAP = {
    "E-AC-3": "DDP",
    "DD+": "DDP",
    "AC-3": "DD",
    "DTS-UHD": "DTS-X",
    "DTS LBR": "DTS-LBR",
    "MPEG-H 3D Audio": "MPEG-H",
    "MPEG Audio Layer 3": "MP3",
}

SPACED_AUDIO_CODECS = {"DTS-X"}

context_settings = dict(
    help_option_names=["-?", "-h", "--help"],
    max_content_width=116,
)

TrackT = TypeVar("TrackT", bound="Track")

AnyTrack = Union["Video", "Audio", "Subtitle"]
