from __future__ import annotations

import logging
import math
import subprocess
import time
from base64 import b64encode
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.session import AnySession
from unshackle.core.titles.music import song_metadata
from unshackle.core.utils.collections import as_list
from unshackle.core.utils.subprocess import log_tool_run

try:
    from PIL import Image
except ImportError:  # pragma: no cover - optional artwork enhancement
    Image = None

try:
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import (
        APIC,
        COMM,
        TALB,
        TCOM,
        TCON,
        TCOP,
        TDRC,
        TIT2,
        TPE1,
        TPE2,
        TPOS,
        TRCK,
        TSRC,
        TXXX,
        USLT,
        ID3NoHeaderError,
    )
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE
except ImportError:  # pragma: no cover - optional tagging dependency
    FLAC = Picture = None  # type: ignore[assignment, misc]
    MP3 = MP4 = MP4Cover = OggOpus = OggVorbis = WAVE = None  # type: ignore[assignment, misc]
    ID3NoHeaderError = Exception  # type: ignore[assignment, misc]
    APIC = COMM = TALB = TCOM = TCON = TCOP = TDRC = TIT2 = TPE1 = TPE2 = TPOS = TRCK = TSRC = TXXX = None  # type: ignore[assignment, misc]
    USLT = None  # type: ignore[assignment, misc]


log = logging.getLogger("MUSIC_TAGGER")

TagValues = dict[str, str | list[str]]
MAX_COVER_BYTES = 2**24 - 1  # a FLAC metadata block length is 24 bits


@dataclass
class MusicMetadataResult:
    written: bool = False
    artwork_embedded: bool = False
    skipped: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "artwork_embedded": self.artwork_embedded,
            "skipped": self.skipped,
            "reason": self.reason,
        }


def write_music_metadata(
    path: Path, song: Any, *, session: AnySession | None = None, source_md5: str = ""
) -> MusicMetadataResult:
    """Write normalized music metadata for FLAC, Ogg, Opus, MP3, WAV, and M4A/MP4 audio files.

    Opus files get no ``REPLAYGAIN_*`` tags, because RFC 7845 section 5.2.1 says an Opus file should not carry them.
    """
    path = Path(path)
    extension = path.suffix.lower()
    if extension not in {".flac", ".mp3", ".wav", ".m4a", ".mp4", ".ogg", ".opus"}:
        return MusicMetadataResult(skipped=True, reason=f"Unsupported music metadata container: {extension}")

    if extension == ".flac" and FLAC is None:
        return MusicMetadataResult(skipped=True, reason="install mutagen to write FLAC tags")
    if extension in {".mp3", ".wav"} and MP3 is None:
        return MusicMetadataResult(skipped=True, reason="install mutagen to write MP3 tags")
    if extension in {".m4a", ".mp4"} and MP4 is None:
        return MusicMetadataResult(skipped=True, reason="install mutagen to write MP4/M4A tags")
    if extension in {".ogg", ".opus"} and OggVorbis is None:
        return MusicMetadataResult(skipped=True, reason="install mutagen to write Ogg tags")

    tags = build_tag_values(song, source_md5=source_md5)
    artwork_url = first_text(getattr(song, "artwork_url", None), song_metadata(song).get("artwork_url"))
    cover_data, mime_type = download_cover(session, artwork_url)
    if not cover_data:
        cover_data, mime_type = attachment_cover(song)
    cover_data, mime_type = usable_cover(cover_data, mime_type, path.name)

    if extension == ".flac":
        write_flac_tags(path, tags, cover_data, mime_type)
    elif extension in {".ogg", ".opus"}:
        if extension == ".opus":
            tags = {key: value for key, value in tags.items() if not key.startswith("REPLAYGAIN_")}
        write_ogg_tags(path, tags, cover_data, mime_type, OggOpus if extension == ".opus" else OggVorbis)
    elif extension in {".mp3", ".wav"}:
        write_mp3_tags(path, tags, cover_data, mime_type, WAVE if extension == ".wav" else MP3)
    else:
        write_mp4_tags(path, tags, cover_data, mime_type)

    return MusicMetadataResult(written=True, artwork_embedded=bool(cover_data))


def build_tag_values(song: Any, *, source_md5: str = "") -> TagValues:
    metadata = song_metadata(song)
    track_number = string_tag(getattr(song, "track", None) or metadata.get("track_number"))
    track_total = string_tag(getattr(song, "total_tracks", None) or metadata.get("total_tracks"))
    disc_number = string_tag(getattr(song, "disc", None) or metadata.get("disc_number"))
    disc_total = string_tag(getattr(song, "total_discs", None) or metadata.get("total_discs"))
    explicit = as_bool(getattr(song, "explicit", None), metadata.get("explicit"), metadata.get("parental_warning"))

    tags: TagValues = {
        "TITLE": first_text(getattr(song, "name", None), metadata.get("title")),
        "ARTIST": first_text(getattr(song, "artist", None), metadata.get("artist"), metadata.get("performer")),
        "ALBUM": first_text(getattr(song, "album", None), metadata.get("album")),
        "ALBUMARTIST": first_text(getattr(song, "album_artist", None), metadata.get("album_artist")),
        "TRACKNUMBER": f"{track_number}/{track_total}" if track_number and track_total else track_number,
        "TRACKTOTAL": track_total,
        "DISCNUMBER": f"{disc_number}/{disc_total}" if disc_number and disc_total else disc_number,
        "DISCTOTAL": disc_total,
        "DATE": string_tag(metadata.get("release_date") or metadata.get("year") or getattr(song, "year", None)),
        "RELEASEDATE": string_tag(metadata.get("release_date")),
        "GENRE": first_text(getattr(song, "genre", None), metadata.get("genre")),
        "COMPOSER": first_text(metadata.get("composer")),
        "PERFORMER": first_text(metadata.get("performer")),
        "ISRC": string_tag(getattr(song, "isrc", None) or metadata.get("isrc")),
        "BARCODE": string_tag(getattr(song, "upc", None) or metadata.get("upc") or metadata.get("barcode")),
        "UPC": string_tag(getattr(song, "upc", None) or metadata.get("upc") or metadata.get("barcode")),
        "COPYRIGHT": string_tag(getattr(song, "copyright", None) or metadata.get("copyright")),
        "LABEL": first_text(getattr(song, "label", None), metadata.get("label")),
        "COMMENT": first_text(metadata.get("comment")),
        "LYRICS": first_text(getattr(song, "lyrics", None), metadata.get("lyrics")),
        "SOURCE": first_text(metadata.get("source"), metadata.get("service")),
        "ENCODEDBY": "Unshackle",
        "UNSHACKLE_SOURCE_MD5": source_md5,
    }
    artists = metadata.get("artists")
    if isinstance(artists, list) and len(artists) > 1:
        tags["ARTISTS"] = [text for text in map(first_text, artists) if text]
    for key, fmt in (
        ("track_gain", "{:.2f} dB"),
        ("track_peak", "{:.6f}"),
        ("album_gain", "{:.2f} dB"),
        ("album_peak", "{:.6f}"),
    ):
        try:
            value = float(metadata[f"replaygain_{key}"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            tags[f"REPLAYGAIN_{key.upper()}"] = fmt.format(value)
    if explicit:
        tags["EXPLICIT"] = "1"
        tags["ITUNESADVISORY"] = "1"
    if config.tag and config.tag_group_name:
        tags["GROUP"] = config.tag

    service = first_text(metadata.get("service"), metadata.get("source"))
    if service:
        prefix = service.upper().replace(" ", "_").replace("-", "_")
        if metadata.get("track_id"):
            tags[f"{prefix}_TRACK_ID"] = string_tag(metadata.get("track_id"))
        if metadata.get("album_id"):
            tags[f"{prefix}_ALBUM_ID"] = string_tag(metadata.get("album_id"))
        if metadata.get("track_url"):
            tags[f"{prefix}_TRACK_URL"] = string_tag(metadata.get("track_url"))
        if metadata.get("album_url"):
            tags[f"{prefix}_ALBUM_URL"] = string_tag(metadata.get("album_url"))

    return {key: value for key, value in tags.items() if value}


def write_flac_tags(path: Path, tags: TagValues, cover_data: bytes | None, mime_type: str) -> None:
    audio = FLAC(path)
    for key, value in tags.items():
        audio[key] = as_list(value)
    picture = build_picture(cover_data, mime_type)
    if picture is not None:
        audio.clear_pictures()
        audio.add_picture(picture)
    audio.save()


def build_picture(cover_data: bytes | None, mime_type: str) -> Any | None:
    """Build a FLAC Picture block, which Ogg reuses base64-encoded."""
    if not cover_data or Picture is None:
        return None
    picture = Picture()
    picture.type = 3
    picture.mime = mime_type or "image/jpeg"
    picture.desc = "Cover"
    picture.data = cover_data
    if Image is not None:
        try:
            with Image.open(BytesIO(cover_data)) as image:
                picture.width, picture.height = image.size
                picture.depth = len(image.getbands()) * 8
        except Exception:
            pass
    return picture


def write_ogg_tags(path: Path, tags: TagValues, cover_data: bytes | None, mime_type: str, reader: Any = None) -> None:
    audio = (reader or OggVorbis)(path)
    for key, value in tags.items():
        audio[key] = as_list(value)
    picture = build_picture(cover_data, mime_type)
    if picture is not None:
        # Ogg carries no picture block of its own, so the FLAC block goes in base64
        audio["METADATA_BLOCK_PICTURE"] = [b64encode(picture.write()).decode("ascii")]
    audio.save()


def write_mp3_tags(path: Path, tags: TagValues, cover_data: bytes | None, mime_type: str, reader: Any) -> None:
    """Write ID3 frames; ``reader`` is mutagen's MP3 or WAVE, both of which carry ID3."""
    try:
        audio = reader(path)
    except ID3NoHeaderError:
        audio = reader(path)
        audio.add_tags()
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None

    frame_map = {
        "TITLE": ("TIT2", TIT2),
        "ARTIST": ("TPE1", TPE1),
        "ALBUM": ("TALB", TALB),
        "ALBUMARTIST": ("TPE2", TPE2),
        "TRACKNUMBER": ("TRCK", TRCK),
        "DISCNUMBER": ("TPOS", TPOS),
        "DATE": ("TDRC", TDRC),
        "GENRE": ("TCON", TCON),
        "COMPOSER": ("TCOM", TCOM),
        "ISRC": ("TSRC", TSRC),
        "COPYRIGHT": ("TCOP", TCOP),
    }
    for key, (frame_id, frame_class) in frame_map.items():
        value = tags.get(key)
        if value and frame_class is not None:
            audio.tags.setall(frame_id, [frame_class(encoding=3, text=as_list(value))])
    if tags.get("COMMENT") and COMM is not None:
        audio.tags.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=[tags["COMMENT"]])])
    if tags.get("LYRICS") and USLT is not None:
        audio.tags.setall("USLT", [USLT(encoding=3, lang="eng", desc="", text=tags["LYRICS"])])
    if cover_data and APIC is not None:
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime=mime_type or "image/jpeg", type=3, desc="Cover", data=cover_data))

    custom_keys = set(tags) - set(frame_map) - {"COMMENT", "LYRICS"}
    for key in sorted(custom_keys):
        if TXXX is not None and tags[key]:
            audio.tags.delall(f"TXXX:{key}")
            audio.tags.add(TXXX(encoding=3, desc=key, text=as_list(tags[key])))
    audio.save()


def write_mp4_tags(path: Path, tags: TagValues, cover_data: bytes | None, mime_type: str) -> None:
    audio = MP4(path)
    if tags.get("TITLE"):
        audio["\xa9nam"] = [tags["TITLE"]]
    if tags.get("ARTIST"):
        audio["\xa9ART"] = [tags["ARTIST"]]
    if tags.get("ALBUM"):
        audio["\xa9alb"] = [tags["ALBUM"]]
    if tags.get("ALBUMARTIST"):
        audio["aART"] = [tags["ALBUMARTIST"]]
    if tags.get("DATE"):
        audio["\xa9day"] = [tags["DATE"]]
    if tags.get("GENRE"):
        audio["\xa9gen"] = [tags["GENRE"]]
    if tags.get("COMPOSER"):
        audio["\xa9wrt"] = [tags["COMPOSER"]]
    if tags.get("COMMENT"):
        audio["\xa9cmt"] = [tags["COMMENT"]]
    if tags.get("COPYRIGHT"):
        audio["cprt"] = [tags["COPYRIGHT"]]
    if tags.get("LYRICS"):
        audio["\xa9lyr"] = [tags["LYRICS"]]

    track_number, track_total = split_number_pair(str(tags.get("TRACKNUMBER", "")))
    if track_number:
        audio["trkn"] = [(track_number, track_total or 0)]
    disc_number, disc_total = split_number_pair(str(tags.get("DISCNUMBER", "")))
    if disc_number:
        audio["disk"] = [(disc_number, disc_total or 0)]

    if cover_data and MP4Cover is not None:
        image_format = MP4Cover.FORMAT_PNG if mime_type == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover_data, imageformat=image_format)]
    mapped_keys = {
        "TITLE",
        "ARTIST",
        "ALBUM",
        "ALBUMARTIST",
        "DATE",
        "GENRE",
        "COMPOSER",
        "COMMENT",
        "COPYRIGHT",
        "LYRICS",
        "TRACKNUMBER",
        "DISCNUMBER",
    }
    for key in sorted(set(tags) - mapped_keys):
        value = tags.get(key)
        if value:
            audio[f"----:com.apple.iTunes:{key}"] = [str(item).encode("utf-8") for item in as_list(value)]
    audio.save()


def download_cover(session: AnySession | None, artwork_url: str) -> tuple[bytes | None, str]:
    if not session or not artwork_url:
        return None, ""
    try:
        with session.get(artwork_url, timeout=20) as response:
            response.raise_for_status()
            data = response.content
            if not data:
                return None, ""
            content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return data, mime_from_image(data, content_type or "image/jpeg")
    except Exception as error:
        log.debug("Music cover download failed for %s: %s", artwork_url, error)
        return None, ""


def attachment_cover(song: Any) -> tuple[bytes | None, str]:
    """Return the first downloaded image attachment of the Song, for a service that gives no artwork_url."""
    tracks = getattr(song, "tracks", None)
    for attachment in getattr(tracks, "attachments", None) or []:
        path = getattr(attachment, "path", None)
        if path and Path(path).exists() and str(getattr(attachment, "mime_type", "")).startswith("image/"):
            data = Path(path).read_bytes()
            return data, mime_from_image(data, attachment.mime_type)
    return None, ""


def usable_cover(cover_data: bytes | None, mime_type: str, name: str) -> tuple[bytes | None, str]:
    """Convert a WebP cover to JPEG, which every container takes, and drop one too large to embed."""
    if cover_data and mime_type == "image/webp":
        try:
            if not binaries.FFMPEG:
                raise OSError("FFmpeg not found")
            convert_start = time.monotonic()
            converted = subprocess.run(
                [
                    binaries.FFMPEG,
                    "-v",
                    "error",
                    "-i",
                    "pipe:",
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-c:v",
                    "mjpeg",
                    "pipe:",
                ],
                input=cover_data,
                capture_output=True,
            )
            log_tool_run(
                "cover webp to jpeg",
                "ffmpeg",
                converted.returncode,
                duration_ms=round((time.monotonic() - convert_start) * 1000, 1),
            )
            converted.check_returncode()
            cover_data, mime_type = converted.stdout, "image/jpeg"
        except (OSError, subprocess.CalledProcessError):
            log.warning(f"Skipped cover art for {name}: the WebP image could not be converted to JPEG")
            return None, ""
    if cover_data and len(cover_data) > MAX_COVER_BYTES:
        log.warning(f"Skipped cover art for {name}: {len(cover_data)} bytes is over the 16 MiB limit")
        return None, ""
    return cover_data, mime_type


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, dict):
            text = first_text(value.get("name"), value.get("title"), value.get("display_name"), value.get("value"))
            if text:
                return text
        elif isinstance(value, list):
            parts = [first_text(item) for item in value]
            text = ", ".join(part for part in parts if part)
            if text:
                return text
        else:
            text = str(value or "").strip()
            if text:
                return text
    return ""


def string_tag(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, bool):
        return "Explicit" if value else ""
    return str(value).strip()


def as_bool(*values: Any) -> bool:
    for value in values:
        if value in (None, "", [], {}):
            continue
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "explicit", "parental_warning"}:
            return True
        if text in {"0", "false", "no", "n", "clean"}:
            return False
    return False


def mime_from_image(data: bytes, fallback: str = "image/jpeg") -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "image/webp"
    return fallback


def split_number_pair(value: str) -> tuple[int, int]:
    if not value:
        return 0, 0
    first, _, second = str(value).partition("/")
    try:
        number = int(first)
    except ValueError:
        number = 0
    try:
        total = int(second) if second else 0
    except ValueError:
        total = 0
    return number, total


__all__ = ("MusicMetadataResult", "write_music_metadata")
