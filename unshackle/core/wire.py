"""The wire form of a title, its tracks, their DRM and attachments: what crosses a process edge.

Two forms cross it. The REST form is the JSON of the REST API and ``--remote``. The export form is the
``x-unshackle`` block of an export file. Each encoder sits next to its decoder. The REST form also carries
display labels, licence data and service data, and the export form only constructor values, so each
form keeps its own field names, field order and value formats. The title and a PSSH have one decoder for
both forms, and the export form of an attachment is the REST form of a URL attachment. Of the Track
constructor fields, only the name, edition and id decode the same way: the REST form tolerates a peer of
another version, and the export form refuses a malformed value.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import date as date_
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

import click
from langcodes import Language
from pyplayready.system.pssh import PSSH as PlayReadyPSSH
from pywidevine.pssh import PSSH as WidevinePSSH

from unshackle.core.config import config
from unshackle.core.constants import AUDIO_CODEC_MAP, DYNAMIC_RANGE_MAP, VIDEO_CODEC_MAP
from unshackle.core.drm import WHOLE_FILE_DRM, BlowfishStripe, ClearKeyCENC, ClearKeyCTR, PlayReady, Widevine
from unshackle.core.titles import Episode, Movie, Song, Title_T
from unshackle.core.titles.availability import availability_metadata
from unshackle.core.titles.music import MUSIC_DATA_KEYS, SONG_FIELDS, song_metadata
from unshackle.core.titles.title import rename
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video
from unshackle.core.tracks.attachment import Attachment
from unshackle.core.tracks.track import Track
from unshackle.core.utilities import original_tag

# the encoders run on the server and the decoders on the client: each keeps the logger it had
server_log = logging.getLogger("api")
client_log = logging.getLogger("remote_service")

EPISODE_PLACEHOLDER_NAME = re.compile(r"Episode ?#?\d+", re.IGNORECASE)
MANIFEST_DATA_KEYS = ("hls", "dash", "ism")
ATTACHMENT_INLINE_LIMIT = 20 * 1024 * 1024


def enum_name(value: Any) -> str:
    """Return an enum-like value's .name, falling back to str()."""
    return value.name if hasattr(value, "name") else str(value)


def enum_get(enum_cls: type[Enum], name: str | None, default: Any = None) -> Any:
    """Safely get an enum value by name."""
    if not name:
        return default
    try:
        return enum_cls[name]
    except KeyError:
        return default


def descriptor_name(track: Any) -> str | None:
    """Manifest descriptor (HLS/DASH/URL) name for a track, or None."""
    descriptor = getattr(track, "descriptor", None)
    return enum_name(descriptor) if descriptor else None


def episode_flags(title: Episode) -> dict[str, Any]:
    """The optional Episode keys both forms write only when set, so the JSON of a title without them stays the same."""
    flags: dict[str, Any] = {}
    if title.absolute is not None:
        flags["absolute"] = title.absolute
    if getattr(title, "daily", None) is not None:
        flags["daily"] = title.daily
    if title.sport:
        flags["sport"] = True
    return flags


def serialize_title(title: Title_T) -> dict[str, Any]:
    """Convert a title object to JSON-serializable dict."""
    title_language = str(title.language) if hasattr(title, "language") and title.language else None
    # Optional display metadata a service may provide: a synopsis (title.description) and a
    # release/air date or poster image URL stashed in title.data. Surfaced so a client can show
    # a richer listing without re-fetching the page.
    description = getattr(title, "description", None) or None
    _data = getattr(title, "data", None)
    availability = availability_metadata(_data.get("availability")) if isinstance(_data, dict) else None
    date = _data.get("date") if isinstance(_data, dict) else None
    cover_url = _data.get("cover_url") if isinstance(_data, dict) else None

    is_episode = isinstance(title, Episode)
    is_song = isinstance(title, Song)
    episode_part = title.part if isinstance(title, Episode) else None
    if isinstance(title, Episode):
        # no part suffix here: build_title rebuilds the Episode from this dict, so a suffixed
        # name plus the structural `part` below would render the part twice in the filename
        name = title.name if title.name else f"Episode {title.number:02d}"
    else:
        name = str(title.name) if hasattr(title, "name") else str(title)

    result = {
        "type": "episode" if is_episode else "movie" if isinstance(title, Movie) else "song" if is_song else "other",
        "name": name,
        "id": str(title.id) if hasattr(title, "id") else None,
        "language": title_language,
        "description": description,
        "date": date,
        "cover_url": cover_url,
    }
    # "other" titles carry no year; only Episode/Movie/Song do.
    if isinstance(title, (Episode, Movie, Song)):
        result["year"] = title.year
    if isinstance(title, Episode):
        result["series_title"] = str(title.title)
        result["season"] = title.season
        result["number"] = title.number
        if episode_part is not None:
            result["part"] = episode_part
        if title.air_date is not None:
            result["air_date"] = (
                title.air_date.isoformat() if isinstance(title.air_date, date_) else str(title.air_date)
            )
        result.update(episode_flags(title))
    if is_song:
        for field in SONG_FIELDS:
            result[field] = getattr(title, field, None)
        metadata = song_metadata(title)
        result["music_data"] = {key: metadata[key] for key in MUSIC_DATA_KEYS if metadata.get(key) not in (None, "")}
    if isinstance(title, (Episode, Movie)) and getattr(title, "anime", None) is not None:
        result["anime"] = title.anime

    if availability is not None:
        result["availability"] = availability
    return result


def title_to_meta(title: Title_T) -> dict[str, Any]:
    """The export form of a title: the fields an import needs to rebuild it for output naming.

    build_title decodes it, as it decodes the REST form.
    """
    meta: dict[str, Any] = {
        "id": str(title.id),
        "language": str(title.language) if getattr(title, "language", None) else None,
    }
    if isinstance(title, Episode):
        meta.update(
            type="episode",
            series_title=title.title,
            season=title.season,
            number=title.number,
            name=title.name,
            year=title.year,
            air_date=title.air_date.isoformat() if isinstance(title.air_date, date_) else title.air_date,
        )
        if title.part is not None:
            meta["part"] = title.part
        meta.update(episode_flags(title))
    elif isinstance(title, Movie):
        meta.update(type="movie", name=title.name, year=title.year)
    else:
        meta.update(type="movie", name=str(title))
    if getattr(title, "anime", None) is not None:
        meta["anime"] = title.anime
    return meta


def episode_name(name: str | None, series_title: str | None) -> str | None:
    """Drop a placeholder episode name the way ``Episode.__init__`` does.

    The server sends "Episode 04" for an unnamed episode, so the same filter has to run
    again here, or the placeholder reaches the output file name.
    """
    if not name:
        return None
    name = name.strip()
    if EPISODE_PLACEHOLDER_NAME.match(name):
        return None
    if series_title and name.lower() == series_title.lower():
        return None
    return name


def parse_air_date(value: Any) -> Any:
    """ISO date string back to a date, as ``Episode.__init__`` does; anything else passes through."""
    if isinstance(value, str):
        try:
            return date_.fromisoformat(value[:10])
        except ValueError:
            return value
    return value


def apply_title_fields(title: Title_T, info: dict[str, Any]) -> None:
    """Copy the serialized metadata fields onto a local title.

    A service can change its title inside ``get_tracks`` (a corrected original language,
    a real episode name, an air date), so the client re-applies the server's view of the
    title after every call that returns one. Season, number and part are set only by
    ``build_title``, as dl can renumber them after the listing.
    """
    if info.get("language"):
        title.language = Language.get(info["language"])
    if info.get("description") is not None and isinstance(title, (Episode, Movie)):
        title.description = info["description"]
    if info.get("year") is not None:
        title.year = info["year"]

    if isinstance(title, Episode):
        if info.get("series_title"):
            title.title = info["series_title"]
        if "name" in info:
            title.name = episode_name(info["name"], title.title)
        if info.get("absolute") is not None:
            title.absolute = info["absolute"]
        if info.get("air_date") is not None:
            title.air_date = parse_air_date(info["air_date"])
    elif info.get("name"):
        title.name = info["name"]

    for key in ("anime", "daily", "sport", *(SONG_FIELDS if isinstance(title, Song) else ())):
        if info.get(key) is not None:
            setattr(title, key, info[key])

    extra = {k: info[k] for k in ("date", "cover_url") if info.get(k) is not None}
    availability = availability_metadata(info.get("availability"))
    if availability is not None:
        extra["availability"] = availability
    music_data = info.get("music_data")
    if isinstance(music_data, dict):
        extra.update({key: music_data[key] for key in MUSIC_DATA_KEYS if key in music_data})
    if extra:
        if isinstance(title.data, dict):
            title.data.update(extra)
        else:
            title.data = extra


def build_title(info: dict[str, Any], service_tag: str, fallback_id: str) -> Title_T:
    """Rebuild a title from its REST form or its export form, under a stand-in class named for the service."""
    svc_class = type(service_tag, (), {})
    lang = Language.get(info["language"]) if info.get("language") else None
    title_type = info.get("type")
    title: Title_T
    if title_type == "episode":
        title = Episode(
            id_=info.get("id", fallback_id),
            service=svc_class,
            title=info.get("series_title", "Unknown"),
            season=info.get("season", 0),
            number=info.get("number", 0),
            part=info.get("part"),
            language=lang,
        )
    elif title_type == "movie":
        title = Movie(
            id_=info.get("id", fallback_id),
            service=svc_class,
            name=info.get("name") or "Unknown",
            language=lang,
        )
    elif title_type == "song":
        title = Song(
            id_=info.get("id", fallback_id),
            service=svc_class,
            name=info.get("name") or "Unknown",
            artist=info.get("artist") or "Unknown",
            album=info.get("album") or "Unknown",
            track=info.get("track") or 1,
            disc=info.get("disc") or 1,
            language=lang,
            album_artist=info.get("album_artist"),
            release_type=info.get("release_type") or "album",
            total_tracks=info.get("total_tracks"),
            total_discs=info.get("total_discs"),
            genre=info.get("genre"),
            explicit=info.get("explicit"),
            isrc=info.get("isrc"),
            upc=info.get("upc"),
            copyright=info.get("copyright"),
            label=info.get("label"),
            lyrics=info.get("lyrics"),
            artwork_url=info.get("artwork_url"),
            synced_lyrics=info.get("synced_lyrics"),
            lyrics_suffix=info.get("lyrics_suffix"),
        )
    else:
        raise click.ClickException(
            f"The remote server sent an unknown title type {title_type!r}; unshackle versions differ"
        )
    apply_title_fields(title, info)
    return title


def serialize_drm(drm_list: Any) -> list[dict[str, Any]] | None:
    """Serialise DRM objects to JSON-serializable list."""
    if not drm_list:
        return None

    if not isinstance(drm_list, list):
        drm_list = [drm_list]

    result = []
    for drm in drm_list:
        drm_info = {}
        drm_class = drm.__class__.__name__
        drm_info["type"] = drm_class.lower()
        if isinstance(drm, WHOLE_FILE_DRM) and drm.key is not None:
            drm_info["key"] = drm.key.hex()
        if isinstance(drm, ClearKeyCTR):
            drm_info.update(iv=drm.iv.hex(), magic=drm.magic.hex(), skip=drm.skip)

        # pywidevine exposes dumps(); pyplayready's PSSH does not, so PlayReady falls back
        # to the base64 it was built from. Without it the client drops every PlayReady
        # entry and licenses the title with the wrong CDM.
        pssh_obj = getattr(drm, "_pssh", None)
        pssh_b64 = (getattr(drm, "data", None) or {}).get("pssh_b64")
        if pssh_obj is not None and hasattr(pssh_obj, "dumps"):
            try:
                drm_info["pssh"] = pssh_obj.dumps()
            except (ValueError, TypeError, KeyError):
                server_log.warning(
                    "Failed to serialize PSSH for DRM type=%s pssh_type=%s",
                    drm_class,
                    type(pssh_obj).__name__,
                    exc_info=True,
                )
        if not drm_info.get("pssh") and pssh_b64:
            drm_info["pssh"] = pssh_b64

        if hasattr(drm, "kids") and drm.kids:
            drm_info["kids"] = [str(kid) for kid in drm.kids]

        if hasattr(drm, "content_keys") and drm.content_keys:
            drm_info["content_keys"] = {str(k): v for k, v in drm.content_keys.items()}

        # Get license URL - essential for remote licensing
        if hasattr(drm, "license_url") and drm.license_url:
            drm_info["license_url"] = str(drm.license_url)
        elif hasattr(drm, "_license_url") and drm._license_url:
            drm_info["license_url"] = str(drm._license_url)

        result.append(drm_info)

    return result if result else None


def reconstruct_drm(drm_list: list | None) -> list:
    """Reconstruct DRM objects from the REST form."""
    if not drm_list:
        return []
    result: list[Any] = []
    for drm_info in drm_list:
        drm_type = drm_info.get("type", "")
        pssh_str = drm_info.get("pssh")
        if not pssh_str and drm_type not in ("blowfishstripe", "clearkeyctr"):
            continue
        try:
            if drm_type == "blowfishstripe":
                result.append(BlowfishStripe(drm_info["key"]))
            elif drm_type == "clearkeyctr":
                result.append(
                    ClearKeyCTR(
                        None, drm_info["iv"], drm_info["magic"], drm_info.get("skip", 0), key=drm_info.get("key")
                    )
                )
            elif drm_type in ("widevine", "playready"):
                result.append(pssh_drm(drm_type, pssh_str, drm_info.get("kids")))
        # a dropped entry can leave the track with no DRM, so never skip silently
        except Exception as e:
            client_log.warning(f"Skipping unparseable {drm_type} DRM entry from server: {e!r}")
            continue
    return result


def pssh_drm(system: str, pssh_b64: str, kids: list[str] | None) -> Widevine | PlayReady:
    """A Widevine or PlayReady DRM rebuilt from its base64 PSSH, as both forms carry it (system in lower case).

    kids repeats the PSSH KIDs, so kids[0] is not the track's own KID; only a PSSH without KIDs needs it.
    """
    if system == "playready":
        return PlayReady(pssh=PlayReadyPSSH(base64.b64decode(pssh_b64)), pssh_b64=pssh_b64)
    wv_pssh = WidevinePSSH(pssh_b64)
    return Widevine(pssh=wv_pssh, kid=kids[0] if kids and not wv_pssh.key_ids else None)


def drm_to_dict(drm: Widevine | PlayReady | ClearKeyCENC) -> dict[str, Any]:
    """A DRM in the export form: its system, PSSH and KIDs.

    The export keeps the content keys once at title level, not here.
    """
    if isinstance(drm, ClearKeyCENC):
        data: dict[str, Any] = {
            "system": "ClearKeyCENC",
            "kids": [kid.hex for kid in drm.kids],
        }
        if drm.laurl:
            data["laurl"] = drm.laurl
        return data
    if isinstance(drm, Widevine):
        return {
            "system": "Widevine",
            "pssh_b64": drm.pssh.dumps(),
            "kids": [kid.hex for kid in drm.kids],
        }
    return {
        "system": "PlayReady",
        "pssh_b64": drm.pssh_b64,
        "kids": [kid.hex for kid in drm.kids],
    }


def drm_from_dict(data: dict[str, Any]) -> Widevine | PlayReady | ClearKeyCENC:
    """Reconstruct a Widevine/PlayReady/ClearKeyCENC DRM instance from its export form.

    Rebuilds the PSSH from the stored base64 (KIDs for ClearKey, which has no PSSH)
    and re-injects any saved content keys so the resulting object can decrypt without
    contacting a license server.
    """
    system = data.get("system")
    pssh_b64 = data.get("pssh_b64")
    kids = data.get("kids") or []
    content_keys = data.get("content_keys") or {}

    if system == "ClearKeyCENC":
        drm: Widevine | PlayReady | ClearKeyCENC = ClearKeyCENC(kids=kids, laurl=data.get("laurl"))
    elif not pssh_b64:
        raise ValueError("Cannot reconstruct DRM without a stored PSSH.")
    elif system in ("PlayReady", "Widevine"):
        drm = pssh_drm(system.lower(), pssh_b64, kids)
    else:
        raise ValueError(f"Unsupported DRM system for reconstruction: {system!r}")

    for kid_hex, key in content_keys.items():
        drm.content_keys[UUID(hex=kid_hex)] = key

    return drm


def serialize_track_data(track: Any) -> dict[str, Any] | None:
    """The JSON-safe part of a service-set ``track.data``.

    This drops the manifest parsers' own keys: the client rebuilds those from the
    ``manifests`` payload, and they hold live lxml and m3u8 objects that JSON cannot carry.
    """
    data = getattr(track, "data", None)
    if not isinstance(data, dict):
        return None
    safe: dict[str, Any] = {}
    for key, value in data.items():
        if key in MANIFEST_DATA_KEYS:
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        safe[key] = value
    return safe or None


def serialize_track_common(track: Any) -> dict[str, Any]:
    """The Track fields every track type shares, a superset of ``Track.to_dict()``."""
    return {
        "id": str(track.id),
        "language": str(track.language) if track.language else None,
        "is_original_lang": bool(getattr(track, "is_original_lang", False)),
        "name": getattr(track, "name", None),
        "track_name": track.get_track_name(),
        "needs_repack": bool(getattr(track, "needs_repack", False)),
        "edition": list(getattr(track, "edition", None) or []),
        "note": getattr(track, "note", None),
        "descriptor": descriptor_name(track),
        "drm": serialize_drm(track.drm) if getattr(track, "drm", None) else None,
        "drm_preference": getattr(track, "drm_preference", None),
        "data": serialize_track_data(track),
    }


def serialize_tracks(tracks: Tracks, title: Any, include_url: bool = False) -> dict[str, list[dict[str, Any]]]:
    """The ``video``, ``audio`` and ``subtitles`` lists of a title's tracks, video and audio highest bitrate first."""
    video_tracks = sorted(tracks.videos, key=lambda t: t.bitrate or 0, reverse=True)
    audio_tracks = sorted(tracks.audio, key=lambda t: t.bitrate or 0, reverse=True)
    original_ids = original_audio_ids(audio_tracks, title)
    return {
        "video": [serialize_video_track(t, include_url=include_url) for t in video_tracks],
        "audio": [
            serialize_audio_track(t, include_url=include_url, is_original=t.id in original_ids) for t in audio_tracks
        ],
        "subtitles": [serialize_subtitle_track(t, include_url=include_url) for t in tracks.subtitles],
    }


def original_audio_ids(tracks: list[Audio], title: Title_T) -> set:
    """Return the ids of the audio tracks 'orig' resolves to, empty when the title has no language.

    This defers to Tracks.by_language through original_tag, as 'orig' does, so the flag agrees
    with the downloader: the title's own variant first, else its close regional forms.
    """
    language = getattr(title, "language", None)
    if not language:
        return set()
    return {t.id for t in Tracks.by_language(tracks, [original_tag(language)])}


def serialize_video_track(track: Video, include_url: bool = False) -> dict[str, Any]:
    """Convert video track to JSON-serializable dict."""
    codec_name = enum_name(track.codec)
    codec_value = track.codec.value if track.codec else codec_name
    range_name = enum_name(track.range)

    result = {
        "id": str(track.id),
        "codec": codec_name,
        "codec_display": rename(VIDEO_CODEC_MAP.get(codec_name, codec_value)),
        "bitrate": int(track.bitrate / 1000) if track.bitrate else None,
        "bitrate_bps": track.bitrate or None,
        "width": track.width,
        "height": track.height,
        "resolution": f"{track.width}x{track.height}" if track.width and track.height else None,
        "fps": track.fps if track.fps else None,
        "range": range_name,
        "range_display": "+".join(rename(DYNAMIC_RANGE_MAP.get(p, p)) for p in track.range_label.split("+")),
        "scan_type": enum_name(track.scan_type) if track.scan_type else None,
        "closed_captions": list(getattr(track, "closed_captions", None) or []),
        "dv_compatible_bitstream": bool(getattr(track, "dv_compatible_bitstream", False)),
        "hdr10plus": bool(getattr(track, "hdr10plus", False)),
        "hdr10_base": bool(getattr(track, "hdr10_base", False)),
        "stereo": bool(getattr(track, "stereo", False)),
    }
    result.update(serialize_track_common(track))
    if include_url and hasattr(track, "url") and track.url:
        result["url"] = str(track.url)
    return result


def serialize_audio_track(track: Audio, include_url: bool = False, is_original: bool = False) -> dict[str, Any]:
    """Convert audio track to JSON-serializable dict.

    Get is_original from original_audio_ids so the flag always agrees with the
    track 'orig' would download.
    """
    codec_name = enum_name(track.codec)
    codec_value = track.codec.value if track.codec else codec_name

    result = {
        "id": str(track.id),
        "codec": codec_name,
        "codec_display": rename(AUDIO_CODEC_MAP.get(codec_value, codec_value)),
        "bitrate": int(track.bitrate / 1000) if track.bitrate else None,
        "bitrate_bps": track.bitrate or None,
        "channels": track.channels if track.channels else None,
        "is_original": is_original,
        "atmos": track.atmos if hasattr(track, "atmos") else False,
        "joc": getattr(track, "joc", 0),
        "descriptive": track.descriptive if hasattr(track, "descriptive") else False,
    }
    result.update(serialize_track_common(track))
    if include_url and hasattr(track, "url") and track.url:
        result["url"] = str(track.url)
    return result


def serialize_subtitle_track(track: Subtitle, include_url: bool = False) -> dict[str, Any]:
    """Convert subtitle track to JSON-serializable dict."""
    result = {
        "id": str(track.id),
        "codec": enum_name(track.codec),
        "forced": track.forced if hasattr(track, "forced") else False,
        "sdh": track.sdh if hasattr(track, "sdh") else False,
        "cc": track.cc if hasattr(track, "cc") else False,
    }
    result.update(serialize_track_common(track))
    if include_url and hasattr(track, "url") and track.url:
        result["url"] = str(track.url)
    return result


def base_track_kwargs(data: dict[str, Any]) -> dict[str, Any]:
    """The Track constructor kwargs that both forms decode the same way."""
    return {"name": data.get("name"), "edition": data.get("edition") or None, "id_": data.get("id")}


def rest_track_kwargs(data: dict[str, Any]) -> dict[str, Any]:
    """The shared Track constructor kwargs of the REST form.

    An older server omits the newer keys, so each one falls back to the value a
    locally-built track would have had.
    """
    return {
        "url": data.get("url") or "https://placeholder",
        "language": Language.get(data.get("language") or "und"),
        "is_original_lang": bool(data.get("is_original_lang", data.get("is_original", False))),
        "descriptor": enum_get(Track.Descriptor, data.get("descriptor"), Track.Descriptor.URL),
        "needs_repack": bool(data.get("needs_repack", False)),
        "note": data["note"] if isinstance(data.get("note"), str) else None,
        **base_track_kwargs(data),
    }


def export_track_kwargs(data: dict[str, Any]) -> dict[str, Any]:
    """The shared Track constructor kwargs of the export form, as stored.

    A malformed value reaches the Track constructor, which refuses it, so the import skips that track.
    """
    return {
        "url": data["url"],
        "language": data.get("language") or "und",
        "is_original_lang": data.get("is_original_lang", False),
        "descriptor": Track.Descriptor[data.get("descriptor", "URL")],
        "needs_repack": data.get("needs_repack", False),
        "note": data.get("note"),
        **base_track_kwargs(data),
    }


def deserialize_bitrate(data: dict[str, Any]) -> int:
    """Exact bits/s when the server sent it, otherwise the kb/s figure scaled back up."""
    exact = data.get("bitrate_bps")
    if exact:
        return int(exact)
    return data["bitrate"] * 1000 if data.get("bitrate") else 0


def deserialize_video(data: dict[str, Any]) -> Video:
    """A video track from its REST form."""
    return Video(
        **rest_track_kwargs(data),
        codec=enum_get(Video.Codec, data.get("codec")),
        range_=enum_get(Video.Range, data.get("range"), Video.Range.SDR),
        bitrate=deserialize_bitrate(data),
        width=data.get("width") or 0,
        height=data.get("height") or 0,
        fps=data.get("fps"),
        scan_type=enum_get(Video.ScanType, data.get("scan_type")),
        closed_captions=data.get("closed_captions") or None,
        dv_compatible_bitstream=bool(data.get("dv_compatible_bitstream", False)),
        hdr10plus=bool(data.get("hdr10plus", False)),
        hdr10_base=bool(data.get("hdr10_base", False)),
        stereo=bool(data.get("stereo", False)),
    )


def deserialize_audio(data: dict[str, Any]) -> Audio:
    """An audio track from its REST form."""
    joc = data.get("joc")
    return Audio(
        **rest_track_kwargs(data),
        codec=enum_get(Audio.Codec, data.get("codec")),
        bitrate=deserialize_bitrate(data),
        channels=data.get("channels"),
        joc=joc,
        extra={"atmos": True} if joc is None and data.get("atmos") else None,
        descriptive=data.get("descriptive", False),
    )


def deserialize_subtitle(data: dict[str, Any]) -> Subtitle:
    """A subtitle track from its REST form."""
    return Subtitle(
        **rest_track_kwargs(data),
        codec=enum_get(Subtitle.Codec, data.get("codec")),
        cc=data.get("cc", False),
        sdh=data.get("sdh", False),
        forced=data.get("forced", False),
    )


def build_tracks(data: dict[str, Any]) -> Tracks:
    """A title's tracks, their DRM and its attachments from the REST form."""
    tracks = Tracks()
    tracks.videos = [deserialize_video(v) for v in data.get("video", [])]
    tracks.audio = [deserialize_audio(a) for a in data.get("audio", [])]
    tracks.subtitles = [deserialize_subtitle(s) for s in data.get("subtitles", [])]

    for track_data, track_obj in [
        *zip(data.get("video", []), tracks.videos, strict=False),
        *zip(data.get("audio", []), tracks.audio, strict=False),
        *zip(data.get("subtitles", []), tracks.subtitles, strict=False),
    ]:
        drm_objs = reconstruct_drm(track_data.get("drm"))
        if drm_objs:
            track_obj.drm = drm_objs
        preference = track_data.get("drm_preference")
        if preference:
            try:
                track_obj.drm_preference = preference
            except ValueError:
                client_log.warning(
                    f"Ignoring unknown drm_preference {preference!r} from the server for track {track_obj.id}"
                )
    tracks.attachments = [a for a in map(build_attachment, data.get("attachments", [])) if a]
    return tracks


def track_to_dict(track: Track) -> dict[str, Any]:
    """A track in the export form: its class name and constructor values.

    DRM is not serialised here. The export writer attaches the licensed DRM + keys.
    """
    data: dict[str, Any] = {
        "type": track.__class__.__name__,
        "id": track.id,
        "url": track.url,
        "language": str(track.language),
        "is_original_lang": track.is_original_lang,
        "descriptor": track.descriptor.name,
        "needs_repack": track.needs_repack,
        "name": track.name,
        "edition": track.edition,
        "note": track.note,
    }
    if isinstance(track, Video):
        data.update(
            {
                "codec": track.codec.name if track.codec else None,
                "range": track.range.name if track.range else None,
                "bitrate": track.bitrate,
                "width": track.width,
                "height": track.height,
                "fps": str(track.fps) if track.fps else None,
                "scan_type": track.scan_type.name if track.scan_type else None,
                "dv_compatible_bitstream": track.dv_compatible_bitstream,
                "hdr10plus": track.hdr10plus,
                "hdr10_base": track.hdr10_base,
                "stereo": track.stereo,
            }
        )
    elif isinstance(track, Audio):
        data.update(
            {
                "codec": track.codec.name if track.codec else None,
                "bitrate": track.bitrate,
                "channels": track.channels,
                "joc": track.joc,
                "descriptive": track.descriptive,
            }
        )
    elif isinstance(track, Subtitle):
        data.update(
            {
                "codec": track.codec.name if track.codec else None,
                "cc": track.cc,
                "sdh": track.sdh,
                "forced": track.forced,
            }
        )
    return data


def track_from_dict(data: dict[str, Any]) -> Video | Audio | Subtitle:
    """Reconstruct the correct Track subclass from its export form.

    DRM is not reconstructed here: the import flow attaches the licensed DRM + content keys separately.
    """
    track_type = data.get("type")
    kinds: dict[str | None, type[Track]] = {"Video": Video, "Audio": Audio, "Subtitle": Subtitle}
    kind = kinds.get(track_type)
    if kind is None:
        raise ValueError(f"Cannot reconstruct unsupported track type: {track_type!r}")
    if kind is Video:
        return Video(
            **export_track_kwargs(data),
            codec=Video.Codec[data["codec"]] if data.get("codec") else None,
            range_=Video.Range[data["range"]] if data.get("range") else None,
            bitrate=data.get("bitrate"),
            width=data.get("width"),
            height=data.get("height"),
            fps=data.get("fps"),
            scan_type=Video.ScanType[data["scan_type"]] if data.get("scan_type") else None,
            dv_compatible_bitstream=data.get("dv_compatible_bitstream", False),
            hdr10plus=data.get("hdr10plus", False),
            hdr10_base=data.get("hdr10_base", False),
            stereo=data.get("stereo", False),
        )
    if kind is Audio:
        return Audio(
            **export_track_kwargs(data),
            codec=Audio.Codec[data["codec"]] if data.get("codec") else None,
            bitrate=data.get("bitrate"),
            channels=data.get("channels"),
            joc=data.get("joc"),
            descriptive=data.get("descriptive", False),
        )
    return Subtitle(
        **export_track_kwargs(data),
        codec=Subtitle.Codec[data["codec"]] if data.get("codec") else None,
        cc=data.get("cc", False),
        sdh=data.get("sdh", False),
        forced=data.get("forced", False),
    )


def attachment_to_dict(attachment: Attachment) -> dict[str, Any]:
    """An attachment in the export form, which is also the REST form of a URL-backed attachment."""
    return {
        "url": attachment.url,
        "name": attachment.name,
        "mime_type": attachment.mime_type,
        "description": attachment.description,
    }


def serialize_attachment(attachment: Any) -> dict[str, Any] | None:
    """One attachment as JSON, with a path-only attachment carried as base64 file bytes.

    A service can attach a file it already wrote to disk (a font, a poster). The client
    has no access to the server's filesystem, so those bytes travel inline.
    """
    result = attachment_to_dict(attachment)
    if attachment.url:
        return result

    path = getattr(attachment, "path", None)
    if not path:
        return None
    from unshackle.core.api.sanitize import sanitize_log

    path = Path(path)
    try:
        size = path.stat().st_size
        if size > ATTACHMENT_INLINE_LIMIT:
            server_log.warning(
                "Skipping attachment %s: %d bytes is over the %d byte inline limit",
                sanitize_log(path.name),
                size,
                ATTACHMENT_INLINE_LIMIT,
            )
            return None
        result["content"] = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as e:
        server_log.warning("Skipping attachment %s: %s", sanitize_log(path.name), e)
        return None
    result["file_name"] = path.name
    return result


def build_attachment(data: dict[str, Any]) -> Attachment | None:
    """Rebuild one attachment. A path-only attachment arrives as base64, which this writes to temp."""
    kwargs = {
        "name": data.get("name"),
        "mime_type": data.get("mime_type"),
        "description": data.get("description"),
    }
    if data.get("url"):
        return Attachment(url=data["url"], **kwargs)

    content = data.get("content")
    if not content:
        return None
    try:
        file_name = Path(str(data.get("file_name") or data.get("name") or "attachment")).name
        target = config.directories.temp / "remote_attachments" / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(content))
        return Attachment(path=target, **kwargs)
    except (OSError, ValueError) as e:
        client_log.warning(f"Skipping attachment {data.get('name')!r} sent by the server: {e}")
        return None
