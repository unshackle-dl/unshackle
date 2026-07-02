from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from unshackle.core.config import config

try:
    from PIL import Image
except ImportError:  # pragma: no cover - optional artwork enhancement
    Image = None

try:
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import (APIC, COMM, TALB, TCOM, TCON, TCOP, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSRC, TXXX,
                             ID3NoHeaderError)
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
except ImportError:  # pragma: no cover - optional tagging dependency
    FLAC = Picture = None
    MP3 = MP4 = MP4Cover = None
    ID3NoHeaderError = Exception
    APIC = COMM = TALB = TCOM = TCON = TCOP = TDRC = TIT2 = TPE1 = TPE2 = TPOS = TRCK = TSRC = TXXX = None


log = logging.getLogger("MUSIC_TAGGER")


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


def write_music_metadata(path: Path, song: Any, *, session: Any = None, source_md5: str = "") -> MusicMetadataResult:
    """Write normalized music metadata for FLAC, MP3, and M4A/MP4 audio files."""
    path = Path(path)
    extension = path.suffix.lower()
    if extension not in {".flac", ".mp3", ".m4a", ".mp4"}:
        return MusicMetadataResult(skipped=True, reason=f"Unsupported music metadata container: {extension}")

    if extension == ".flac" and FLAC is None:
        return MusicMetadataResult(skipped=True, reason="install mutagen to write FLAC tags")
    if extension == ".mp3" and MP3 is None:
        return MusicMetadataResult(skipped=True, reason="install mutagen to write MP3 tags")
    if extension in {".m4a", ".mp4"} and MP4 is None:
        return MusicMetadataResult(skipped=True, reason="install mutagen to write MP4/M4A tags")

    tags = _build_tag_values(song, source_md5=source_md5)
    artwork_url = _first_text(getattr(song, "artwork_url", None), _metadata(song).get("artwork_url"))
    cover_data, mime_type = _download_cover(session, artwork_url)

    if extension == ".flac":
        _write_flac_tags(path, tags, cover_data, mime_type)
    elif extension == ".mp3":
        _write_mp3_tags(path, tags, cover_data, mime_type)
    else:
        _write_mp4_tags(path, tags, cover_data, mime_type)

    return MusicMetadataResult(written=True, artwork_embedded=bool(cover_data))


def _build_tag_values(song: Any, *, source_md5: str = "") -> dict[str, str]:
    metadata = _metadata(song)
    track_number = _string_tag(getattr(song, "track", None) or metadata.get("track_number"))
    track_total = _string_tag(getattr(song, "total_tracks", None) or metadata.get("total_tracks"))
    disc_number = _string_tag(getattr(song, "disc", None) or metadata.get("disc_number"))
    disc_total = _string_tag(getattr(song, "total_discs", None) or metadata.get("total_discs"))
    explicit = _as_bool(getattr(song, "explicit", None), metadata.get("explicit"), metadata.get("parental_warning"))

    tags = {
        "TITLE": _first_text(getattr(song, "name", None), metadata.get("title")),
        "ARTIST": _first_text(getattr(song, "artist", None), metadata.get("artist"), metadata.get("performer")),
        "ALBUM": _first_text(getattr(song, "album", None), metadata.get("album")),
        "ALBUMARTIST": _first_text(getattr(song, "album_artist", None), metadata.get("album_artist")),
        "TRACKNUMBER": f"{track_number}/{track_total}" if track_number and track_total else track_number,
        "TRACKTOTAL": track_total,
        "DISCNUMBER": f"{disc_number}/{disc_total}" if disc_number and disc_total else disc_number,
        "DISCTOTAL": disc_total,
        "DATE": _string_tag(metadata.get("release_date") or metadata.get("year") or getattr(song, "year", None)),
        "RELEASEDATE": _string_tag(metadata.get("release_date")),
        "GENRE": _first_text(getattr(song, "genre", None), metadata.get("genre")),
        "COMPOSER": _first_text(metadata.get("composer")),
        "PERFORMER": _first_text(metadata.get("performer")),
        "ISRC": _string_tag(getattr(song, "isrc", None) or metadata.get("isrc")),
        "BARCODE": _string_tag(getattr(song, "upc", None) or metadata.get("upc") or metadata.get("barcode")),
        "UPC": _string_tag(getattr(song, "upc", None) or metadata.get("upc") or metadata.get("barcode")),
        "COPYRIGHT": _string_tag(getattr(song, "copyright", None) or metadata.get("copyright")),
        "LABEL": _first_text(getattr(song, "label", None), metadata.get("label")),
        "COMMENT": _first_text(metadata.get("comment"), metadata.get("quality")),
        "SOURCE": _first_text(metadata.get("source"), metadata.get("service")),
        "ENCODEDBY": "Unshackle",
        "UNSHACKLE_SOURCE_MD5": source_md5,
    }
    if explicit:
        tags["EXPLICIT"] = "1"
        tags["ITUNESADVISORY"] = "1"
    if config.tag and config.tag_group_name:
        tags["GROUP"] = config.tag

    service = _first_text(metadata.get("service"), metadata.get("source"))
    if service:
        prefix = service.upper().replace(" ", "_").replace("-", "_")
        if metadata.get("track_id"):
            tags[f"{prefix}_TRACK_ID"] = _string_tag(metadata.get("track_id"))
        if metadata.get("album_id"):
            tags[f"{prefix}_ALBUM_ID"] = _string_tag(metadata.get("album_id"))
        if metadata.get("track_url"):
            tags[f"{prefix}_TRACK_URL"] = _string_tag(metadata.get("track_url"))
        if metadata.get("album_url"):
            tags[f"{prefix}_ALBUM_URL"] = _string_tag(metadata.get("album_url"))

    return {key: value for key, value in tags.items() if value}


def _write_flac_tags(path: Path, tags: dict[str, str], cover_data: Optional[bytes], mime_type: str) -> None:
    audio = FLAC(path)
    for key, value in tags.items():
        audio[key] = [value]
    if cover_data and Picture is not None:
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
        audio.clear_pictures()
        audio.add_picture(picture)
    audio.save()


def _write_mp3_tags(path: Path, tags: dict[str, str], cover_data: Optional[bytes], mime_type: str) -> None:
    try:
        audio = MP3(path)
    except ID3NoHeaderError:
        audio = MP3(path)
        audio.add_tags()
    if audio.tags is None:
        audio.add_tags()

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
            audio.tags.setall(frame_id, [frame_class(encoding=3, text=[value])])
    if tags.get("COMMENT") and COMM is not None:
        audio.tags.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=[tags["COMMENT"]])])
    if cover_data and APIC is not None:
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime=mime_type or "image/jpeg", type=3, desc="Cover", data=cover_data))

    custom_keys = set(tags) - set(frame_map) - {"COMMENT"}
    for key in sorted(custom_keys):
        if TXXX is not None and tags[key]:
            audio.tags.delall(f"TXXX:{key}")
            audio.tags.add(TXXX(encoding=3, desc=key, text=[tags[key]]))
    audio.save()


def _write_mp4_tags(path: Path, tags: dict[str, str], cover_data: Optional[bytes], mime_type: str) -> None:
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

    track_number, track_total = _split_number_pair(tags.get("TRACKNUMBER", ""))
    if track_number:
        audio["trkn"] = [(track_number, track_total or 0)]
    disc_number, disc_total = _split_number_pair(tags.get("DISCNUMBER", ""))
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
        "TRACKNUMBER",
        "DISCNUMBER",
    }
    for key in sorted(set(tags) - mapped_keys):
        value = tags.get(key)
        if value:
            audio[f"----:com.apple.iTunes:{key}"] = [str(value).encode("utf-8")]
    audio.save()


def _download_cover(session: Any, artwork_url: str) -> tuple[Optional[bytes], str]:
    if not session or not artwork_url:
        return None, ""
    try:
        with session.get(artwork_url, timeout=20) as response:
            response.raise_for_status()
            data = response.content
            if not data:
                return None, ""
            content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return data, _mime_from_image(data, content_type or "image/jpeg")
    except Exception as error:
        log.debug("Music cover download failed for %s: %s", artwork_url, error)
        return None, ""


def _metadata(song: Any) -> dict[str, Any]:
    data = getattr(song, "data", None)
    return data if isinstance(data, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, dict):
            text = _first_text(value.get("name"), value.get("title"), value.get("display_name"), value.get("value"))
            if text:
                return text
        elif isinstance(value, list):
            parts = [_first_text(item) for item in value]
            text = ", ".join(part for part in parts if part)
            if text:
                return text
        else:
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _string_tag(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, bool):
        return "Explicit" if value else ""
    return str(value).strip()


def _as_bool(*values: Any) -> bool:
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


def _mime_from_image(data: bytes, fallback: str = "image/jpeg") -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "image/webp"
    return fallback


def _split_number_pair(value: str) -> tuple[int, int]:
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
