from __future__ import annotations

from abc import abstractmethod
from typing import Any, Optional, Union

from langcodes import Language
from pymediainfo import MediaInfo

from unshackle.core.config import config
from unshackle.core.constants import AUDIO_CODEC_MAP, DYNAMIC_RANGE_MAP, VIDEO_CODEC_MAP
from unshackle.core.tracks import Tracks


class Title:
    def __init__(
        self, id_: Any, service: type, language: Optional[Union[str, Language]] = None, data: Optional[Any] = None
    ) -> None:
        """
        Media Title from a Service.

        Parameters:
            id_: An identifier for this specific title. It must be unique. Can be of any
                value.
            service: Service class that this title is from.
            language: The original recorded language for the title. If that information
                is not available, this should not be set to anything.
            data: Arbitrary storage for the title. Often used to store extra metadata
                information, IDs, URIs, and so on.
        """
        if not id_:  # includes 0, false, and similar values, this is intended
            raise ValueError("A unique ID must be provided")
        if hasattr(id_, "__len__") and len(id_) < 4:
            raise ValueError("The unique ID is not large enough, clash likely.")

        if not service:
            raise ValueError("Service class must be provided")
        if not isinstance(service, type):
            raise TypeError(f"Expected service to be a Class (type), not {service!r}")

        if language is not None:
            if isinstance(language, str):
                language = Language.get(language)
            elif not isinstance(language, Language):
                raise TypeError(f"Expected language to be a {Language} or str, not {language!r}")

        self.id = id_
        self.service = service
        self.language = language
        self.data = data

        self.tracks = Tracks()

    def __eq__(self, other: Title) -> bool:
        return self.id == other.id

    def _build_base_template_context(self, media_info: MediaInfo, show_service: bool = True) -> dict:
        """Build base template context dictionary from MediaInfo.

        Extracts video, audio, HDR, HFR, and multi-language information shared
        across all title types. Subclasses should call this and extend the
        returned dict with their specific fields (e.g., season/episode).
        """
        primary_video_track = next(iter(media_info.video_tracks), None)
        primary_audio_track = next(iter(media_info.audio_tracks), None)
        unique_audio_languages = len({x.language.split("-")[0] for x in media_info.audio_tracks if x.language})

        context: dict[str, Any] = {
            "source": self.service.__name__ if show_service else "",
            "tag": config.tag or "",
            "repack": "REPACK" if getattr(config, "repack", False) else "",
            "quality": "",
            "resolution": "",
            "audio": "",
            "audio_channels": "",
            "audio_full": "",
            "atmos": "",
            "dual": "",
            "multi": "",
            "video": "",
            "hdr": "",
            "hfr": "",
            "edition": "",
        }

        if self.tracks:
            first_track = next(iter(self.tracks), None)
            if first_track and first_track.edition:
                context["edition"] = " ".join(first_track.edition)

        if primary_video_track:
            width = getattr(primary_video_track, "width", primary_video_track.height)
            resolution = min(width, primary_video_track.height)
            try:
                dar = getattr(primary_video_track, "other_display_aspect_ratio", None) or []
                if dar and dar[0]:
                    aspect_ratio = [int(float(plane)) for plane in str(dar[0]).split(":")]
                    if len(aspect_ratio) == 1:
                        aspect_ratio.append(1)
                    ratio = aspect_ratio[0] / aspect_ratio[1]
                    if ratio not in (16 / 9, 4 / 3, 9 / 16, 3 / 4):
                        resolution = int(max(width, primary_video_track.height) * (9 / 16))
            except Exception:
                pass

            scan_suffix = "i" if str(getattr(primary_video_track, "scan_type", "")).lower() == "interlaced" else "p"

            context.update(
                {
                    "quality": f"{resolution}{scan_suffix}",
                    "resolution": str(resolution),
                    "video": VIDEO_CODEC_MAP.get(primary_video_track.format, primary_video_track.format),
                }
            )

            hdr_format = primary_video_track.hdr_format_commercial
            trc = primary_video_track.transfer_characteristics or primary_video_track.transfer_characteristics_original
            if hdr_format:
                if (primary_video_track.hdr_format or "").startswith("Dolby Vision"):
                    context["hdr"] = "DV"
                    base_layer = DYNAMIC_RANGE_MAP.get(hdr_format)
                    if base_layer and base_layer != "DV":
                        context["hdr"] += f".{base_layer}"
                else:
                    context["hdr"] = DYNAMIC_RANGE_MAP.get(hdr_format, "")
            elif trc and "HLG" in trc:
                context["hdr"] = "HLG"
            else:
                context["hdr"] = ""

            frame_rate = float(primary_video_track.frame_rate) if primary_video_track.frame_rate else 0.0
            context["hfr"] = "HFR" if frame_rate > 30 else ""

        if primary_audio_track:
            codec = primary_audio_track.format
            channel_layout = primary_audio_track.channel_layout or primary_audio_track.channellayout_original

            if channel_layout:
                channels = float(sum({"LFE": 0.1}.get(position.upper(), 1) for position in channel_layout.split(" ")))
            else:
                channel_count = primary_audio_track.channel_s or primary_audio_track.channels or 0
                channels = float(channel_count)

            features = primary_audio_track.format_additionalfeatures or ""

            context.update(
                {
                    "audio": AUDIO_CODEC_MAP.get(codec, codec),
                    "audio_channels": f"{channels:.1f}",
                    "audio_full": f"{AUDIO_CODEC_MAP.get(codec, codec)}{channels:.1f}",
                    "atmos": "Atmos" if ("JOC" in features or primary_audio_track.joc) else "",
                }
            )

        if unique_audio_languages == 2:
            context["dual"] = "DUAL"
            context["multi"] = ""
        elif unique_audio_languages > 2:
            context["dual"] = ""
            context["multi"] = "MULTi"
        else:
            context["dual"] = ""
            context["multi"] = ""

        return context

    @abstractmethod
    def get_filename(self, media_info: MediaInfo, folder: bool = False, show_service: bool = True) -> str:
        """
        Get a Filename for this Title with the provided Media Info.
        All filenames should be sanitized with the sanitize_filename() utility function.

        Parameters:
            media_info: MediaInfo object of the file this name will be used for.
            folder: This filename will be used as a folder name. Some changes may want to
                be made if this is the case.
            show_service: Show the service tag (e.g., iT, NF) in the filename.
        """


__all__ = ("Title",)
