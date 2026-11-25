"""Pins the wire form of titles, tracks, DRM and attachments, in the REST form and the export form."""

from __future__ import annotations

import inspect
import json
import logging
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import click
import pytest
from pywidevine.pssh import PSSH as WidevinePSSH

from tests.core.test_zero_kid_fallback import _playready_pssh
from unshackle.commands.dl import dl
from unshackle.core import wire
from unshackle.core.config import config
from unshackle.core.drm import BlowfishStripe, ClearKeyCENC, PlayReady, Widevine
from unshackle.core.import_service import ImportService
from unshackle.core.titles import Episode, Movie, Song
from unshackle.core.titles.music import SONG_FIELDS
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video
from unshackle.core.tracks.attachment import Attachment
from unshackle.core.tracks.track import Track
from unshackle.core.wire import (
    apply_title_fields,
    attachment_to_dict,
    build_attachment,
    build_title,
    build_tracks,
    deserialize_audio,
    deserialize_subtitle,
    deserialize_video,
    drm_from_dict,
    drm_to_dict,
    reconstruct_drm,
    serialize_attachment,
    serialize_audio_track,
    serialize_drm,
    serialize_subtitle_track,
    serialize_title,
    serialize_tracks,
    serialize_video_track,
    title_to_meta,
    track_from_dict,
    track_to_dict,
)

pytestmark = pytest.mark.unit

VIDEO_KID = UUID("00112233-4455-6677-8899-aabbccddeeff")
AUDIO_KID = UUID("ffeeddcc-bbaa-9988-7766-554433221100")
VIDEO_KEY = "0123456789abcdef0123456789abcdef"
BLOWFISH_KEY = bytes(range(16))


class EXAMPLE:
    pass


def widevine(*kids: UUID, keys: bool = False) -> Widevine:
    drm = Widevine(pssh=WidevinePSSH.new(system_id=WidevinePSSH.SystemId.Widevine, key_ids=list(kids)))
    if keys:
        drm.content_keys[VIDEO_KID] = VIDEO_KEY
    return drm


def playready(*kids: UUID) -> PlayReady:
    pssh, pssh_b64 = _playready_pssh(*kids)
    return PlayReady(pssh=pssh, pssh_b64=pssh_b64)


def clearkey() -> ClearKeyCENC:
    return ClearKeyCENC(kids=[VIDEO_KID], laurl="https://licence.example/ck")


def episode(**extra: Any) -> Episode:
    fields: dict[str, Any] = dict(
        id_="ep-0001",
        service=EXAMPLE,
        title="Example Show",
        season=2,
        number=5,
        name="The Example",
        year=2024,
        language="en",
        description="An example episode.",
        air_date=date(2024, 3, 9),
        data={"date": "2024-03-09", "cover_url": "https://img.example/ep.jpg"},
    )
    fields.update(extra)
    return Episode(**fields)


def full_episode() -> Episode:
    ep = episode(part=2, absolute=17, sport=True)
    ep.anime = True
    ep.daily = False
    return ep


def movie() -> Movie:
    return Movie(
        id_="movie-0001",
        service=EXAMPLE,
        name="Example Film",
        year=2023,
        language="fr",
        description="An example film.",
        data={"cover_url": "https://img.example/film.jpg"},
    )


def song() -> Song:
    return Song(
        id_="song-0001",
        service=EXAMPLE,
        name="Example Song",
        artist="Example Artist",
        album="Example Album",
        track=3,
        disc=1,
        year=2022,
        language="en",
        data={"release_date": "2022-01-02", "composer": "Example Composer", "metadata": {"comment": "nested"}},
        album_artist="Example Band",
        release_type="single",
        total_tracks=12,
        total_discs=2,
        genre="Pop",
        explicit=False,
        isrc="EX0000000001",
        upc="000000000001",
        copyright="(c) Example",
        label="Example Records",
        lyrics="la la",
        artwork_url="https://img.example/song.jpg",
        synced_lyrics="[00:01.00]la",
        lyrics_suffix=".lrc",
    )


class OtherTitle:
    """A title of no known kind, as a service could hand back."""

    id = "other-0001"
    language = None
    name = "Other Thing"
    data = None

    def __str__(self) -> str:
        return "Other Thing"


def video(**extra: Any) -> Video:
    fields: dict[str, Any] = dict(
        url="https://cdn.example/video.mpd",
        language="en",
        is_original_lang=True,
        descriptor=Track.Descriptor.DASH,
        needs_repack=True,
        name="Main",
        edition=["IMAX"],
        note="High profile",
        codec=Video.Codec.HEVC,
        range_=Video.Range.DV,
        bitrate=8_123_456,
        width=3840,
        height=2160,
        fps="24000/1001",
        scan_type=Video.ScanType.PROGRESSIVE,
        closed_captions=[{"language": "en", "channel": "CC1"}],
        dv_compatible_bitstream=True,
        hdr10plus=True,
        hdr10_base=True,
        stereo=True,
        id_="video-0001",
    )
    fields.update(extra)
    track = Video(**fields)
    track.data["service_value"] = {"a": 1}
    track.data["dash"] = {"not": "json-safe here"}
    track.data["unsafe"] = object()
    return track


def audio(**extra: Any) -> Audio:
    fields: dict[str, Any] = dict(
        url="https://cdn.example/audio.mpd",
        language="en",
        descriptor=Track.Descriptor.HLS,
        codec=Audio.Codec.EC3,
        bitrate=640_000,
        channels="5.1",
        joc=16,
        descriptive=True,
        id_="audio-0001",
    )
    fields.update(extra)
    return Audio(**fields)


def subtitle(**extra: Any) -> Subtitle:
    fields: dict[str, Any] = dict(
        url="https://cdn.example/sub.vtt",
        language="pt-BR",
        descriptor=Track.Descriptor.URL,
        codec=Subtitle.Codec.WebVTT,
        cc=False,
        sdh=True,
        forced=False,
        id_="sub-0001",
    )
    fields.update(extra)
    return Subtitle(**fields)


def drm_fields(drm: Any) -> dict[str, Any]:
    """What a DRM object carries across the edge."""
    out: dict[str, Any] = {
        "class": type(drm).__name__,
        "kids": [kid.hex for kid in getattr(drm, "kids", None) or []],
        "content_keys": {kid.hex: key for kid, key in (getattr(drm, "content_keys", None) or {}).items()},
    }
    if isinstance(drm, Widevine):
        out["pssh"] = drm.pssh.dumps()
    if isinstance(drm, PlayReady):
        out["pssh"] = drm.pssh_b64
    if isinstance(drm, ClearKeyCENC):
        out["laurl"] = drm.laurl
    if isinstance(drm, BlowfishStripe):
        out["key"] = drm.key.hex()
    return out


def track_fields(track: Any) -> dict[str, Any]:
    """What a track carries across the edge."""
    out: dict[str, Any] = {
        "class": type(track).__name__,
        "id": track.id,
        "url": track.url,
        "language": str(track.language),
        "is_original_lang": track.is_original_lang,
        "descriptor": track.descriptor.name,
        "needs_repack": track.needs_repack,
        "name": track.name,
        "edition": track.edition,
        "note": track.note,
        "codec": track.codec.name if track.codec else None,
        "drm": [drm_fields(d) for d in track.drm or []],
        "drm_preference": track.drm_preference,
    }
    if isinstance(track, Video):
        out.update(
            range=track.range.name if track.range else None,
            bitrate=track.bitrate,
            width=track.width,
            height=track.height,
            fps=track.fps,
            scan_type=track.scan_type.name if track.scan_type else None,
            closed_captions=track.closed_captions,
            dv_compatible_bitstream=track.dv_compatible_bitstream,
            hdr10plus=track.hdr10plus,
            hdr10_base=track.hdr10_base,
            stereo=track.stereo,
        )
    elif isinstance(track, Audio):
        out.update(
            bitrate=track.bitrate,
            channels=track.channels,
            joc=track.joc,
            atmos=track.atmos,
            descriptive=track.descriptive,
        )
    elif isinstance(track, Subtitle):
        out.update(cc=track.cc, sdh=track.sdh, forced=track.forced)
    return out


def title_fields(title: Any) -> dict[str, Any]:
    """What a title carries across the edge."""
    out: dict[str, Any] = {
        "class": type(title).__name__,
        "service": title.service.__name__,
        "id": title.id,
        "language": str(title.language) if title.language else None,
        "year": title.year,
        "data": title.data,
    }
    if isinstance(title, Episode):
        out.update(
            title=title.title,
            season=title.season,
            number=title.number,
            name=title.name,
            part=title.part,
            description=title.description,
            air_date=title.air_date,
            absolute=title.absolute,
            sport=title.sport,
            anime=getattr(title, "anime", None),
            daily=getattr(title, "daily", None),
        )
    elif isinstance(title, Movie):
        out.update(name=title.name, description=title.description, anime=getattr(title, "anime", None))
    elif isinstance(title, Song):
        out.update(name=title.name, **{f: getattr(title, f) for f in SONG_FIELDS})
    return out


WIDEVINE_PSSH = "AAAAMnBzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAABISEAARIjNEVWZ3iJmqu8zd7v8="
KIDLESS_PSSH = "AAAAJXBzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAAAUiA2FiYw=="
KIDLESS_WITH_KID_PSSH = "AAAAN3Bzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAABcSEAARIjNEVWZ3iJmqu8zd7v8iA2FiYw=="
PLAYREADY_PSSH = _playready_pssh(VIDEO_KID, AUDIO_KID)[1]


def json_copy(value: Any) -> Any:
    """What the other side of the edge reads."""
    return json.loads(json.dumps(value))


def named(expected: dict[str, Any], track: Any) -> dict[str, Any]:
    """The expected REST dict with the track name the muxing config gives this track."""
    return {**expected, "track_name": track.get_track_name()}


def drm_track() -> Video:
    track = video()
    track.drm = [widevine(VIDEO_KID, keys=True), playready(VIDEO_KID, AUDIO_KID)]
    track.drm_preference = "pr"
    return track


REST_EPISODE = {
    "type": "episode",
    "name": "The Example",
    "id": "ep-0001",
    "language": "en",
    "description": "An example episode.",
    "date": "2024-03-09",
    "cover_url": "https://img.example/ep.jpg",
    "year": 2024,
    "series_title": "Example Show",
    "season": 2,
    "number": 5,
    "part": 2,
    "air_date": "2024-03-09",
    "absolute": 17,
    "daily": False,
    "sport": True,
    "anime": True,
}

REST_MOVIE = {
    "type": "movie",
    "name": "Example Film",
    "id": "movie-0001",
    "language": "fr",
    "description": "An example film.",
    "date": None,
    "cover_url": "https://img.example/film.jpg",
    "year": 2023,
}

REST_SONG = {
    "type": "song",
    "name": "Example Song",
    "id": "song-0001",
    "language": "en",
    "description": None,
    "date": None,
    "cover_url": None,
    "year": 2022,
    "artist": "Example Artist",
    "album": "Example Album",
    "track": 3,
    "disc": 1,
    "album_artist": "Example Band",
    "release_type": "single",
    "total_tracks": 12,
    "total_discs": 2,
    "genre": "Pop",
    "explicit": False,
    "isrc": "EX0000000001",
    "upc": "000000000001",
    "copyright": "(c) Example",
    "label": "Example Records",
    "lyrics": "la la",
    "synced_lyrics": "[00:01.00]la",
    "lyrics_suffix": ".lrc",
    "artwork_url": "https://img.example/song.jpg",
    "music_data": {"release_date": "2022-01-02", "composer": "Example Composer", "comment": "nested"},
}

META_EPISODE = {
    "id": "ep-0001",
    "language": "en",
    "type": "episode",
    "series_title": "Example Show",
    "season": 2,
    "number": 5,
    "name": "The Example",
    "year": 2024,
    "air_date": "2024-03-09",
    "part": 2,
    "absolute": 17,
    "daily": False,
    "sport": True,
    "anime": True,
}

REST_VIDEO = {
    "id": "video-0001",
    "codec": "HEVC",
    "codec_display": "H.265",
    "bitrate": 8123,
    "bitrate_bps": 8123456,
    "width": 3840,
    "height": 2160,
    "resolution": "3840x2160",
    "fps": 23.976023976023978,
    "range": "DV",
    "range_display": "DV+HDR10P",
    "scan_type": "PROGRESSIVE",
    "closed_captions": [{"language": "en", "channel": "CC1"}],
    "dv_compatible_bitstream": True,
    "hdr10plus": True,
    "hdr10_base": True,
    "stereo": True,
    "language": "en",
    "is_original_lang": True,
    "name": "Main",
    "track_name": "Main",
    "needs_repack": True,
    "edition": ["IMAX"],
    "note": "High profile",
    "descriptor": "DASH",
    "drm": [
        {
            "type": "widevine",
            "pssh": WIDEVINE_PSSH,
            "kids": ["00112233-4455-6677-8899-aabbccddeeff"],
            "content_keys": {"00112233-4455-6677-8899-aabbccddeeff": VIDEO_KEY},
        },
        {
            "type": "playready",
            "pssh": PLAYREADY_PSSH,
            "kids": ["00112233-4455-6677-8899-aabbccddeeff", "ffeeddcc-bbaa-9988-7766-554433221100"],
        },
    ],
    "drm_preference": "pr",
    "data": {"service_value": {"a": 1}},
    "url": "https://cdn.example/video.mpd",
}

REST_AUDIO = {
    "id": "audio-0001",
    "codec": "EC3",
    "codec_display": "DDP",
    "bitrate": 640,
    "bitrate_bps": 640000,
    "channels": 5.1,
    "is_original": True,
    "atmos": True,
    "joc": 16,
    "descriptive": True,
    "language": "en",
    "is_original_lang": False,
    "name": None,
    "track_name": "set per test",
    "needs_repack": False,
    "edition": [],
    "note": None,
    "descriptor": "HLS",
    "drm": None,
    "drm_preference": None,
    "data": None,
    "url": "https://cdn.example/audio.mpd",
}

REST_SUBTITLE = {
    "id": "sub-0001",
    "codec": "WebVTT",
    "forced": False,
    "sdh": True,
    "cc": False,
    "language": "pt-BR",
    "is_original_lang": False,
    "name": None,
    "track_name": "set per test",
    "needs_repack": False,
    "edition": [],
    "note": None,
    "descriptor": "URL",
    "drm": None,
    "drm_preference": None,
    "data": None,
}

EXPORT_VIDEO = {
    "type": "Video",
    "id": "video-0001",
    "url": "https://cdn.example/video.mpd",
    "language": "en",
    "is_original_lang": True,
    "descriptor": "DASH",
    "needs_repack": True,
    "name": "Main",
    "edition": ["IMAX"],
    "note": "High profile",
    "codec": "HEVC",
    "range": "DV",
    "bitrate": 8123456,
    "width": 3840,
    "height": 2160,
    "fps": "23.976023976023978",
    "scan_type": "PROGRESSIVE",
    "dv_compatible_bitstream": True,
    "hdr10plus": True,
    "hdr10_base": True,
    "stereo": True,
}

EXPORT_AUDIO = {
    "type": "Audio",
    "id": "audio-0001",
    "url": "https://cdn.example/audio.mpd",
    "language": "en",
    "is_original_lang": False,
    "descriptor": "HLS",
    "needs_repack": False,
    "name": None,
    "edition": [],
    "note": None,
    "codec": "EC3",
    "bitrate": 640000,
    "channels": 5.1,
    "joc": 16,
    "descriptive": True,
}

EXPORT_SUBTITLE = {
    "type": "Subtitle",
    "id": "sub-0001",
    "url": "https://cdn.example/sub.vtt",
    "language": "pt-BR",
    "is_original_lang": False,
    "descriptor": "URL",
    "needs_repack": False,
    "name": None,
    "edition": [],
    "note": None,
    "codec": "WebVTT",
    "cc": False,
    "sdh": True,
    "forced": False,
}

VIDEO_FIELDS = {
    "class": "Video",
    "id": "video-0001",
    "url": "https://cdn.example/video.mpd",
    "language": "en",
    "is_original_lang": True,
    "descriptor": "DASH",
    "needs_repack": True,
    "name": "Main",
    "edition": ["IMAX"],
    "note": "High profile",
    "codec": "HEVC",
    "drm": [],
    "drm_preference": None,
    "range": "DV",
    "bitrate": 8123456,
    "width": 3840,
    "height": 2160,
    "fps": 23.976023976023978,
    "scan_type": "PROGRESSIVE",
    "closed_captions": [{"language": "en", "channel": "CC1"}],
    "dv_compatible_bitstream": True,
    "hdr10plus": True,
    "hdr10_base": True,
    "stereo": True,
}

AUDIO_FIELDS = {
    "class": "Audio",
    "id": "audio-0001",
    "url": "https://cdn.example/audio.mpd",
    "language": "en",
    "is_original_lang": False,
    "descriptor": "HLS",
    "needs_repack": False,
    "name": None,
    "edition": [],
    "note": None,
    "codec": "EC3",
    "drm": [],
    "drm_preference": None,
    "bitrate": 640000,
    "channels": 5.1,
    "joc": 16,
    "atmos": True,
    "descriptive": True,
}

SUBTITLE_FIELDS = {
    "class": "Subtitle",
    "id": "sub-0001",
    "url": "https://cdn.example/sub.vtt",
    "language": "pt-BR",
    "is_original_lang": False,
    "descriptor": "URL",
    "needs_repack": False,
    "name": None,
    "edition": [],
    "note": None,
    "codec": "WebVTT",
    "drm": [],
    "drm_preference": None,
    "cc": False,
    "sdh": True,
    "forced": False,
}

WIDEVINE_FIELDS = {"class": "Widevine", "kids": [VIDEO_KID.hex], "content_keys": {}, "pssh": WIDEVINE_PSSH}
PLAYREADY_FIELDS = {
    "class": "PlayReady",
    "kids": [VIDEO_KID.hex, AUDIO_KID.hex],
    "content_keys": {},
    "pssh": PLAYREADY_PSSH,
}
CLEARKEY_FIELDS = {
    "class": "ClearKeyCENC",
    "kids": [VIDEO_KID.hex],
    "content_keys": {},
    "laurl": "https://licence.example/ck",
}

EPISODE_FIELDS = {
    "class": "Episode",
    "service": "EXAMPLE",
    "id": "ep-0001",
    "language": "en",
    "year": 2024,
    "data": {"date": "2024-03-09", "cover_url": "https://img.example/ep.jpg"},
    "title": "Example Show",
    "season": 2,
    "number": 5,
    "name": "The Example",
    "part": 2,
    "description": "An example episode.",
    "air_date": date(2024, 3, 9),
    "absolute": 17,
    "sport": True,
    "anime": True,
    "daily": False,
}


def test_rest_episode() -> None:
    assert serialize_title(full_episode()) == REST_EPISODE


def test_rest_episode_without_a_name_gets_a_placeholder_and_no_optional_keys() -> None:
    assert serialize_title(episode(name=None)) == {
        "type": "episode",
        "name": "Episode 05",
        "id": "ep-0001",
        "language": "en",
        "description": "An example episode.",
        "date": "2024-03-09",
        "cover_url": "https://img.example/ep.jpg",
        "year": 2024,
        "series_title": "Example Show",
        "season": 2,
        "number": 5,
        "air_date": "2024-03-09",
    }


def test_rest_movie() -> None:
    assert serialize_title(movie()) == REST_MOVIE


def test_rest_song() -> None:
    assert serialize_title(song()) == REST_SONG


def test_rest_title_of_no_known_kind() -> None:
    assert serialize_title(OtherTitle()) == {
        "type": "other",
        "name": "Other Thing",
        "id": "other-0001",
        "language": None,
        "description": None,
        "date": None,
        "cover_url": None,
    }


def test_rest_title_key_order_is_stable() -> None:
    assert list(serialize_title(full_episode())) == list(REST_EPISODE)
    assert list(serialize_title(song())) == list(REST_SONG)


def test_export_episode_meta() -> None:
    meta = dl.title_to_meta(full_episode())
    assert meta == META_EPISODE
    assert list(meta) == list(META_EPISODE)


def test_export_movie_meta() -> None:
    assert dl.title_to_meta(movie()) == {
        "id": "movie-0001",
        "language": "fr",
        "type": "movie",
        "name": "Example Film",
        "year": 2023,
    }


def test_export_meta_of_another_title_is_a_movie_named_by_str() -> None:
    assert dl.title_to_meta(song()) == {
        "id": "song-0001",
        "language": "en",
        "type": "movie",
        "name": "Example Artist - Example Album (2022) / 03. Example Song",
    }


def test_build_title_from_rest_episode() -> None:
    assert title_fields(build_title(json_copy(REST_EPISODE), "EXAMPLE", "fallback-id")) == EPISODE_FIELDS


def test_build_title_drops_the_placeholder_name() -> None:
    title = build_title(json_copy(serialize_title(episode(name=None))), "EXAMPLE", "fallback-id")
    assert title_fields(title) == {
        **EPISODE_FIELDS,
        "name": None,
        "part": None,
        "absolute": None,
        "sport": False,
        "anime": None,
        "daily": None,
    }


def test_build_title_from_rest_movie() -> None:
    assert title_fields(build_title(json_copy(REST_MOVIE), "EXAMPLE", "fallback-id")) == {
        "class": "Movie",
        "service": "EXAMPLE",
        "id": "movie-0001",
        "language": "fr",
        "year": 2023,
        "data": {"cover_url": "https://img.example/film.jpg"},
        "name": "Example Film",
        "description": "An example film.",
        "anime": None,
    }


def test_build_title_from_rest_song() -> None:
    fields = title_fields(build_title(json_copy(REST_SONG), "EXAMPLE", "fallback-id"))
    assert fields == {
        "class": "Song",
        "service": "EXAMPLE",
        "id": "song-0001",
        "language": "en",
        "year": 2022,
        "data": {"release_date": "2022-01-02", "composer": "Example Composer", "comment": "nested"},
        "name": "Example Song",
        **{key: REST_SONG[key] for key in SONG_FIELDS},
    }


def test_build_title_from_export_meta() -> None:
    title = build_title(json_copy(META_EPISODE), "EXAMPLE", "fallback-id")
    assert title_fields(title) == {**EPISODE_FIELDS, "data": None, "description": None}


def test_build_title_from_a_bare_episode_uses_the_fallbacks() -> None:
    assert title_fields(build_title({"type": "episode"}, "EXAMPLE", "fallback-id")) == {
        "class": "Episode",
        "service": "EXAMPLE",
        "id": "fallback-id",
        "language": None,
        "year": None,
        "data": None,
        "title": "Unknown",
        "season": 0,
        "number": 0,
        "name": None,
        "part": None,
        "description": None,
        "air_date": None,
        "absolute": None,
        "sport": False,
        "anime": None,
        "daily": None,
    }


def test_build_title_refuses_an_unknown_kind() -> None:
    with pytest.raises(click.ClickException, match="unknown title type 'other'"):
        build_title(json_copy(serialize_title(OtherTitle())), "EXAMPLE", "fallback-id")


def test_apply_title_fields_keeps_season_and_number() -> None:
    title = episode(name=None, description=None, data=None)
    apply_title_fields(
        title,
        {
            "language": "de",
            "description": "New",
            "year": 2030,
            "series_title": "Renamed",
            "name": "Episode 9",
            "absolute": 3,
            "air_date": "2030-01-02T00:00:00",
            "anime": True,
            "sport": True,
            "date": "d",
            "cover_url": "c",
            "season": 9,
        },
    )
    assert title_fields(title) == {
        **EPISODE_FIELDS,
        "language": "de",
        "year": 2030,
        "data": {"date": "d", "cover_url": "c"},
        "title": "Renamed",
        "name": None,
        "part": None,
        "description": "New",
        "air_date": date(2030, 1, 2),
        "absolute": 3,
        "sport": True,
        "anime": True,
        "daily": None,
    }


def test_rest_video() -> None:
    serialized = serialize_video_track(drm_track(), include_url=True)
    assert serialized == REST_VIDEO
    assert list(serialized) == list(REST_VIDEO)


def test_rest_audio() -> None:
    serialized = serialize_audio_track(audio(), include_url=True, is_original=True)
    assert serialized == named(REST_AUDIO, audio())
    assert list(serialized) == list(REST_AUDIO)


def test_rest_subtitle() -> None:
    serialized = serialize_subtitle_track(subtitle())
    assert serialized == named(REST_SUBTITLE, subtitle())
    assert list(serialized) == list(REST_SUBTITLE)


def test_rest_tracks_sort_by_bitrate_and_flag_the_original_audio() -> None:
    tracks = Tracks(
        [
            video(id_="v-low", bitrate=1000),
            video(id_="v-high", bitrate=9000),
            audio(id_="a-fr", language="fr", bitrate=1),
            audio(id_="a-en", language="en", bitrate=2),
            subtitle(),
        ]
    )
    lists = serialize_tracks(tracks, movie())
    assert list(lists) == ["video", "audio", "subtitles"]
    assert [(v["id"], v["bitrate_bps"]) for v in lists["video"]] == [("v-high", 9000), ("v-low", 1000)]
    assert [(a["id"], a["is_original"]) for a in lists["audio"]] == [("a-en", False), ("a-fr", True)]
    assert lists["subtitles"] == [named(REST_SUBTITLE, subtitle())]
    assert "url" not in lists["video"][0]


def test_export_video() -> None:
    assert video().to_dict() == EXPORT_VIDEO
    assert list(video().to_dict()) == list(EXPORT_VIDEO)


def test_export_audio() -> None:
    assert audio().to_dict() == EXPORT_AUDIO
    assert list(audio().to_dict()) == list(EXPORT_AUDIO)


def test_export_subtitle() -> None:
    assert subtitle().to_dict() == EXPORT_SUBTITLE
    assert list(subtitle().to_dict()) == list(EXPORT_SUBTITLE)


def test_deserialize_rest_tracks() -> None:
    assert track_fields(deserialize_video(json_copy(REST_VIDEO))) == VIDEO_FIELDS
    assert track_fields(deserialize_audio(json_copy(REST_AUDIO))) == AUDIO_FIELDS
    assert track_fields(deserialize_subtitle(json_copy(REST_SUBTITLE))) == {
        **SUBTITLE_FIELDS,
        "url": "https://placeholder",
    }


def test_deserialize_a_track_from_an_older_server() -> None:
    old_video = deserialize_video(
        {"id": "v-old", "codec": "AVC", "bitrate": 5000, "language": "en", "is_original": True}
    )
    assert track_fields(old_video) == {
        "class": "Video",
        "id": "v-old",
        "url": "https://placeholder",
        "language": "en",
        "is_original_lang": True,
        "descriptor": "URL",
        "needs_repack": False,
        "name": None,
        "edition": [],
        "note": None,
        "codec": "AVC",
        "drm": [],
        "drm_preference": None,
        "range": "SDR",
        "bitrate": 5000000,
        "width": None,
        "height": None,
        "fps": None,
        "scan_type": None,
        "closed_captions": [],
        "dv_compatible_bitstream": False,
        "hdr10plus": False,
        "hdr10_base": False,
        "stereo": False,
    }
    old_audio = deserialize_audio({"id": "a-old", "codec": "AAC", "atmos": True, "channels": 6})
    assert track_fields(old_audio) == {
        "class": "Audio",
        "id": "a-old",
        "url": "https://placeholder",
        "language": "und",
        "is_original_lang": False,
        "descriptor": "URL",
        "needs_repack": False,
        "name": None,
        "edition": [],
        "note": None,
        "codec": "AAC",
        "drm": [],
        "drm_preference": None,
        "bitrate": None,
        "channels": 6.0,
        "joc": None,
        "atmos": True,
        "descriptive": False,
    }
    odd_subtitle = deserialize_subtitle({"id": "s-old", "codec": "NOPE", "note": 5})
    assert track_fields(odd_subtitle) == {
        **SUBTITLE_FIELDS,
        "id": "s-old",
        "url": "https://placeholder",
        "language": "und",
        "codec": None,
        "sdh": False,
    }


def test_export_tracks_decode() -> None:
    assert track_fields(track_from_dict(json_copy(EXPORT_VIDEO))) == {**VIDEO_FIELDS, "closed_captions": []}
    assert track_fields(track_from_dict(json_copy(EXPORT_AUDIO))) == AUDIO_FIELDS
    assert track_fields(track_from_dict(json_copy(EXPORT_SUBTITLE))) == SUBTITLE_FIELDS


def test_export_track_of_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported track type: 'Chapter'"):
        track_from_dict({**EXPORT_VIDEO, "type": "Chapter"})


def test_build_tracks_attaches_drm_preference_and_attachments() -> None:
    lists = serialize_tracks(Tracks([drm_track(), audio(), subtitle()]), movie(), include_url=True)
    attachment = serialize_attachment(Attachment(url="https://img.example/p.jpg", name="P"))
    tracks = build_tracks(json_copy({**lists, "attachments": [attachment]}))
    assert track_fields(tracks.videos[0]) == {
        **VIDEO_FIELDS,
        "drm": [WIDEVINE_FIELDS, PLAYREADY_FIELDS],
        "drm_preference": "pr",
    }
    assert track_fields(tracks.audio[0]) == AUDIO_FIELDS
    assert track_fields(tracks.subtitles[0]) == SUBTITLE_FIELDS
    assert [(a.name, a.url) for a in tracks.attachments] == [("P", "https://img.example/p.jpg")]


def test_rest_drm_of_each_system() -> None:
    drm = [widevine(VIDEO_KID, keys=True), playready(VIDEO_KID, AUDIO_KID), BlowfishStripe(BLOWFISH_KEY), clearkey()]
    assert serialize_drm(drm) == [
        *REST_VIDEO["drm"],
        {"type": "blowfishstripe", "key": "000102030405060708090a0b0c0d0e0f"},
        {"type": "clearkeycenc", "kids": ["00112233-4455-6677-8899-aabbccddeeff"]},
    ]


def test_export_drm_of_each_system() -> None:
    assert widevine(VIDEO_KID).to_dict() == {
        "system": "Widevine",
        "pssh_b64": WIDEVINE_PSSH,
        "kids": [VIDEO_KID.hex],
    }
    assert playready(VIDEO_KID, AUDIO_KID).to_dict() == {
        "system": "PlayReady",
        "pssh_b64": PLAYREADY_PSSH,
        "kids": [VIDEO_KID.hex, AUDIO_KID.hex],
    }
    assert clearkey().to_dict() == {
        "system": "ClearKeyCENC",
        "kids": [VIDEO_KID.hex],
        "laurl": "https://licence.example/ck",
    }


def test_reconstruct_rest_drm() -> None:
    drm = [widevine(VIDEO_KID, keys=True), playready(VIDEO_KID, AUDIO_KID), BlowfishStripe(BLOWFISH_KEY), clearkey()]
    rebuilt = reconstruct_drm(json_copy(serialize_drm(drm)))
    assert [drm_fields(d) for d in rebuilt] == [
        WIDEVINE_FIELDS,
        PLAYREADY_FIELDS,
        {"class": "BlowfishStripe", "kids": [], "content_keys": {}, "key": "000102030405060708090a0b0c0d0e0f"},
    ]


def test_reconstruct_rest_drm_gives_a_kidless_pssh_the_first_kid() -> None:
    rebuilt = reconstruct_drm([{"type": "widevine", "pssh": KIDLESS_PSSH, "kids": [str(VIDEO_KID)]}])
    assert [drm_fields(d) for d in rebuilt] == [{**WIDEVINE_FIELDS, "pssh": KIDLESS_WITH_KID_PSSH}]


def test_reconstruct_rest_drm_skips_what_it_cannot_parse(caplog: pytest.LogCaptureFixture) -> None:
    entries = [{"type": "widevine"}, {"type": "playready", "pssh": "!!"}, {"type": "clearkeycenc", "kids": ["x"]}]
    with caplog.at_level(logging.WARNING):
        assert reconstruct_drm(entries) == []
    assert [(r.name, r.getMessage()) for r in caplog.records] == [
        (
            "remote_service",
            "Skipping unparseable playready DRM entry from server: InvalidPssh('Data must not be empty')",
        )
    ]


def test_export_drm_decode() -> None:
    rebuilt = [drm_from_dict(json_copy(d.to_dict())) for d in (widevine(VIDEO_KID), playready(VIDEO_KID, AUDIO_KID))]
    rebuilt.append(drm_from_dict(json_copy(clearkey().to_dict())))
    assert [drm_fields(d) for d in rebuilt] == [WIDEVINE_FIELDS, PLAYREADY_FIELDS, CLEARKEY_FIELDS]


def test_export_drm_decode_takes_the_stored_keys() -> None:
    data = {
        "system": "Widevine",
        "pssh_b64": KIDLESS_PSSH,
        "kids": [VIDEO_KID.hex],
        "content_keys": {VIDEO_KID.hex: VIDEO_KEY},
    }
    assert drm_fields(drm_from_dict(data)) == {
        **WIDEVINE_FIELDS,
        "content_keys": {VIDEO_KID.hex: VIDEO_KEY},
        "pssh": KIDLESS_WITH_KID_PSSH,
    }


@pytest.mark.parametrize(
    "data, message",
    [
        ({"system": "Widevine"}, "Cannot reconstruct DRM without a stored PSSH."),
        ({"system": "Nope", "pssh_b64": "x"}, "Unsupported DRM system for reconstruction: 'Nope'"),
    ],
)
def test_export_drm_decode_refuses(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        drm_from_dict(data)


def test_rest_attachment_by_url() -> None:
    attachment = Attachment(
        url="https://img.example/poster.jpg", name="Poster", mime_type="image/jpeg", description="Cover"
    )
    expected = {
        "url": "https://img.example/poster.jpg",
        "name": "Poster",
        "mime_type": "image/jpeg",
        "description": "Cover",
    }
    assert serialize_attachment(attachment) == expected
    assert attachment.to_dict() == expected


def test_rest_attachment_by_path_carries_the_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.directories, "temp", tmp_path / "temp")
    poster = tmp_path / "poster.jpg"
    poster.write_bytes(b"jpeg-bytes")
    serialized = serialize_attachment(Attachment(path=poster, name="Poster", description="Cover"))
    assert serialized == {
        "url": None,
        "name": "Poster",
        "mime_type": "image/jpeg",
        "description": "Cover",
        "content": "anBlZy1ieXRlcw==",
        "file_name": "poster.jpg",
    }
    rebuilt = build_attachment(json_copy(serialized))
    assert rebuilt is not None
    assert rebuilt.path == tmp_path / "temp" / "remote_attachments" / "poster.jpg"
    assert rebuilt.path.read_bytes() == b"jpeg-bytes"
    assert (rebuilt.url, rebuilt.name, rebuilt.mime_type, rebuilt.description) == (
        None,
        "Poster",
        "image/jpeg",
        "Cover",
    )


def test_build_attachment_by_url_and_with_nothing() -> None:
    rebuilt = build_attachment({"url": "https://img.example/p.jpg", "name": "P", "mime_type": "image/jpeg"})
    assert rebuilt is not None
    assert (rebuilt.path, rebuilt.url, rebuilt.name, rebuilt.mime_type) == (
        None,
        "https://img.example/p.jpg",
        "P",
        "image/jpeg",
    )
    assert build_attachment({"name": "x"}) is None


def test_rest_titles_round_trip() -> None:
    for title in (full_episode(), movie()):
        assert title_fields(build_title(json_copy(serialize_title(title)), "EXAMPLE", "fallback-id")) == title_fields(
            title
        )
    rebuilt = title_fields(build_title(json_copy(serialize_title(song())), "EXAMPLE", "fallback-id"))
    assert {**rebuilt, "data": None} == {**title_fields(song()), "data": None}


def test_export_meta_round_trips_the_fields_it_carries() -> None:
    episode_meta = title_fields(build_title(json_copy(title_to_meta(full_episode())), "EXAMPLE", "fallback-id"))
    assert episode_meta == {**title_fields(full_episode()), "data": None, "description": None}
    movie_meta = title_fields(build_title(json_copy(title_to_meta(movie())), "EXAMPLE", "fallback-id"))
    assert movie_meta == {**title_fields(movie()), "data": None, "description": None}


def test_rest_tracks_round_trip_with_their_drm() -> None:
    track = video()
    track.drm = [widevine(VIDEO_KID), playready(VIDEO_KID, AUDIO_KID)]
    track.drm_preference = "pr"
    lists = serialize_tracks(Tracks([track, audio(), subtitle()]), movie(), include_url=True)
    rebuilt = build_tracks(json_copy(lists))
    assert [track_fields(t) for t in (*rebuilt.videos, *rebuilt.audio, *rebuilt.subtitles)] == [
        track_fields(t) for t in (track, audio(), subtitle())
    ]


def test_export_tracks_round_trip_without_closed_captions() -> None:
    for track in (video(), audio(), subtitle()):
        expected = track_fields(track)
        if isinstance(track, Video):
            expected["closed_captions"] = []
        assert track_fields(track_from_dict(json_copy(track_to_dict(track)))) == expected


def test_both_track_forms_decode_the_shared_fields_the_same_way() -> None:
    shared = ("id", "url", "language", "is_original_lang", "descriptor", "needs_repack", "name", "edition", "note")
    forms: list[tuple[Any, Any, Any]] = [
        (video(), serialize_video_track, deserialize_video),
        (audio(), serialize_audio_track, deserialize_audio),
        (subtitle(), serialize_subtitle_track, deserialize_subtitle),
    ]
    for track, encode, decode in forms:
        rest = track_fields(decode(json_copy(encode(track, include_url=True))))
        export = track_fields(track_from_dict(json_copy(track_to_dict(track))))
        assert {key: rest[key] for key in shared} == {key: export[key] for key in shared}


URL_SUBTITLE = {
    "type": "Subtitle",
    "id": "good",
    "url": "https://example.invalid/en.srt",
    "language": "en",
    "descriptor": "URL",
    "codec": "SubRip",
}


@pytest.mark.parametrize(
    "change, error",
    [
        ({"descriptor": "NEW"}, "'NEW'"),
        ({"url": None}, "'url'"),
        ({"is_original_lang": "yes"}, "Expected is_original_lang to be a <class 'bool'>, not <class 'str'>"),
        ({"needs_repack": 1}, "Expected needs_repack to be a <class 'bool'>, not <class 'int'>"),
        ({"note": 5}, "Expected note to be a <class 'str'>, not <class 'int'>"),
        (
            {"language": 5},
            "Expected language to be a <class 'langcodes.Language'> or <class 'str'>, not <class 'int'>",
        ),
        ({"codec": "NOPE"}, "'NOPE'"),
    ],
    ids=["descriptor", "no-url", "original-flag", "repack-flag", "note", "language", "codec"],
)
def test_an_import_skips_a_malformed_export_track(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, change: dict[str, Any], error: str
) -> None:
    bad = {key: value for key, value in {**URL_SUBTITLE, "id": "bad", **change}.items() if value is not None}
    entry = {
        "meta": {"type": "movie", "id": "movie-1", "name": "Example Movie", "year": 2024, "language": "en"},
        "manifest_type": "DASH",
        "tracks": {"good": URL_SUBTITLE, "bad": bad},
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps({"version": 2, "service": "EXAMPLE", "titles": {"movie-1": entry}}), encoding="utf8")
    service = ImportService(click.Context(click.Command("dl")), "EXAMPLE", "movie-1", str(path))
    with caplog.at_level(logging.WARNING):
        tracks = service.get_tracks(next(iter(service.get_titles())))
    assert [t.id for t in tracks.subtitles] == ["good"]
    assert [r.getMessage() for r in caplog.records if r.name == "EXAMPLE"] == [
        f"Skipping exported track 'bad': {error}"
    ]


def test_drm_round_trips_in_both_forms() -> None:
    drm = [widevine(VIDEO_KID), playready(VIDEO_KID, AUDIO_KID)]
    assert [drm_fields(d) for d in reconstruct_drm(json_copy(serialize_drm(drm)))] == [drm_fields(d) for d in drm]
    for one in (*drm, clearkey()):
        assert drm_fields(drm_from_dict(json_copy(drm_to_dict(one)))) == drm_fields(one)


def test_attachments_round_trip_in_both_forms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fields(attachment: Attachment) -> tuple[Any, ...]:
        return attachment.url, attachment.name, attachment.mime_type, attachment.description

    by_url = Attachment(
        url="https://img.example/poster.jpg", name="Poster", mime_type="image/jpeg", description="Cover"
    )
    for data in (serialize_attachment(by_url), attachment_to_dict(by_url)):
        rebuilt = build_attachment(json_copy(data))
        assert rebuilt is not None
        assert fields(rebuilt) == fields(by_url)
    monkeypatch.setattr(config.directories, "temp", tmp_path / "temp")
    poster = tmp_path / "poster.jpg"
    poster.write_bytes(b"jpeg-bytes")
    by_path = Attachment(path=poster, name="Poster", description="Cover")
    rebuilt = build_attachment(json_copy(serialize_attachment(by_path)))
    assert rebuilt is not None and rebuilt.path is not None
    assert (fields(rebuilt), rebuilt.path.read_bytes()) == (fields(by_path), b"jpeg-bytes")


def test_an_attachment_over_the_inline_limit_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wire, "ATTACHMENT_INLINE_LIMIT", 4)
    poster = tmp_path / "poster.jpg"
    poster.write_bytes(b"jpeg-bytes")
    assert serialize_attachment(Attachment(path=poster, name="Poster")) is None


def test_an_export_track_of_an_unhashable_type_is_refused_as_before() -> None:
    with pytest.raises(TypeError, match="unhashable type|as a dict key"):
        track_from_dict({**EXPORT_VIDEO, "type": {"a": 1}})


RENAMED = {"range_": "range", "id_": "id", "title": "series_title"}
NOT_SENT = {"session", "downloader", "downloader_args", "from_file", "extra", "service", "data"}
NOT_EXPORTED = NOT_SENT | {"closed_captions", "drm", "description"}


def constructor_fields(cls: type) -> set[str]:
    """The constructor parameters of a track or title class, under the name its wire form uses."""
    classes = [cls, Track] if issubclass(cls, Track) else [cls]
    names = {name for c in classes for name in inspect.signature(c.__init__).parameters}
    return {RENAMED.get(name, name) for name in names - {"self", "args", "kwargs"}}


@pytest.mark.parametrize(
    "item, rest, export",
    [
        (video, lambda t: serialize_video_track(t, include_url=True), track_to_dict),
        (audio, lambda t: serialize_audio_track(t, include_url=True), track_to_dict),
        (subtitle, lambda t: serialize_subtitle_track(t, include_url=True), track_to_dict),
        (full_episode, serialize_title, title_to_meta),
        (movie, serialize_title, title_to_meta),
        (song, serialize_title, None),
    ],
    ids=["video", "audio", "subtitle", "episode", "movie", "song"],
)
def test_every_constructor_field_crosses_the_edge(item: Any, rest: Any, export: Any) -> None:
    """A new constructor parameter fails here until a form carries it or a set above names it as not carried."""
    obj = item()
    fields = constructor_fields(type(obj))
    assert fields - NOT_SENT - set(rest(obj)) == set()
    if export is not None:
        assert fields - NOT_EXPORTED - set(export(obj)) == set()


@pytest.mark.parametrize(
    "module",
    ["unshackle.core.wire", "unshackle.core.api", "unshackle.core.tracks", "unshackle.core.drm"],
)
def test_each_module_imports_first_in_a_new_interpreter(module: str) -> None:
    """wire imports tracks, drm and titles, and they import wire inside their to_dict, so no order may cycle."""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=root, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
