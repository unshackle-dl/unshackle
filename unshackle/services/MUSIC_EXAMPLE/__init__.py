from __future__ import annotations

import re
from collections.abc import Generator
from datetime import datetime
from http.cookiejar import CookieJar
from typing import Any, Optional

import click

from unshackle.core.console import console
from unshackle.core.credential import Credential
from unshackle.core.music import (
    build_music_from_songs,
    classify_release_kind,
    duration_seconds,
    first_number,
    first_text,
    format_names,
    year_from_value,
)
from unshackle.core.search_result import SearchResult
from unshackle.core.service import Service
from unshackle.core.titles import Music, Song, Title_T, Titles_T
from unshackle.core.tracks import Audio, Chapters, Tracks


class MUSIC_EXAMPLE(Service):
    """
    Reference music service for musicdomain.com - the music counterpart of the
    EXAMPLE service. Like EXAMPLE it is NOT meant to operate against a real API.
    It exists so a new music-service author can see the whole music surface in one
    place.

    Version: 1.0.0
    Author: sp4rk.y
    Date: 2026-08-18
    Authorization: Cookies + Credentials
    Geofence: US, UK
    Robustness:
        FLAC 24-bit: subscription tier `hires`
        FLAC 16-bit: subscription tier `lossless`
        AAC:         every tier

    Tips:
        - Input may be a full URL or a bare kind/ID pair. The kind is part of the
          identifier, because a catalogue addresses tracks, albums, playlists and
          artists separately:
            https://musicdomain.com/album/20914   ->   album/20914
        - -d / --device selects a client profile block from config.yaml. A mobile
          profile usually gets fewer formats than a desktop one.
        - --lyrics costs one extra request per track, so it is opt-in. The tagger
          writes what it returns into the track's lyrics tag.
        - -w / --wanted narrows a release to some of its tracks: `-w 1-5` takes tracks
          1 to 5 off disc 1, `-w 2x3` takes disc 2 track 3. The framework matches those
          keys against every Song, so a service never filters its own tracklist.

    Feature map (where to look):
        get_titles            track / album / playlist / artist release kinds
        music_from_album      build_music_from_songs, the idiomatic collection builder
        build_song            the full Song field set, and what `data` feeds
        get_tracks            ONE Audio track per codec+bitrate the source offers
        get_chapters          empty, because a song has no chapters

    There is no music-specific Service API. A `Song` is an ordinary `Title` on the
    one generic download path, so everything else a music service needs (auth,
    search, DRM license callbacks, proxies) works exactly as it does in EXAMPLE -
    read that service for the DRM and manifest sides, which are not repeated here.
    """

    ALIASES = ("MUSICEX", "MUSICDOMAIN")
    GEOFENCE = ("US", "UK")
    TITLE_RE = (
        r"^(?:https?://(?:www\.)?musicdomain\.com/)?(?P<kind>track|album|playlist|artist)/(?P<content_id>[^/?#]+)"
    )
    NO_SUBTITLES = True

    @staticmethod
    @click.command(name="MUSIC_EXAMPLE", short_help="https://musicdomain.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option(
        "-d",
        "--device",
        type=click.Choice(["desktop", "web", "mobile"], case_sensitive=False),
        default="desktop",
        help="Client profile block to use from config.yaml.",
    )
    @click.option(
        "--lyrics", is_flag=True, default=False, help="Fetch and tag lyrics for each track (one request each)."
    )
    @click.pass_context
    def cli(ctx: click.Context, **kwargs: Any) -> MUSIC_EXAMPLE:
        return MUSIC_EXAMPLE(ctx, **kwargs)

    def __init__(self, ctx: click.Context, title: str, device: str, lyrics: bool) -> None:
        self.title = title
        self.device = device
        self.lyrics = lyrics

        super().__init__(ctx)

        self.profile = (ctx.parent.params.get("profile") if ctx.parent else None) or "default"


        if self.config is None:
            raise EnvironmentError("config.yaml is missing for this service.")

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)

        self.session.headers.update({"user-agent": self.config["client"][self.device]["user_agent"]})

        cache = self.cache.get(f"tokens_{self.device}_{self.profile}")

        if cache and cache.data.get("expires_in", 0) > int(datetime.now().timestamp()):
            self.log.info(" + Using cached tokens")
        else:
            if not credential:
                raise EnvironmentError("Service requires Credentials.")
            token = self.session.post(
                url=self.config["endpoints"]["login"],
                data={"username": credential.username, "password": credential.password},
            ).json()
            cache.set(data=token, expiration=token.get("expires_in"))

        self.token = cache.data["token"]
        self.tier = cache.data.get("tier", "lossless")

    def search(self) -> Generator[SearchResult, None, None]:
        results = self.session.get(
            url=self.config["endpoints"]["search"],
            params={"q": self.title, "token": self.token},
        ).json()

        for result in results["releases"]:
            yield SearchResult(
                id_=f"{result['kind']}/{result['id']}",
                title=f"{result['artist']} - {result['title']}",
                description=result.get("description"),
                label=result["kind"].upper(),
                url=result.get("url"),
            )

    def get_titles(self) -> Titles_T:
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError("Could not parse a release ID - is the URL/ID correct?")
        kind, content_id = match.group("kind"), match.group("content_id")

        if kind == "track":
            track = self.api("track", track_id=content_id)
            song = self.build_song(track["album"], track)
            return build_music_from_songs(
                [song],
                kind="track",
                title=song.name,
                artist=song.artist,
                empty_error="The track is not available in this region.",
            )

        if kind == "album":
            return self.music_from_album(content_id)

        if kind == "playlist":
            playlist = self.api("playlist", release_id=content_id)
            release = {
                "id": playlist["id"],
                "title": playlist["title"],
                "artist": format_names(playlist.get("curators")) or "Various Artists",
                "type": "playlist",
                "artworkUrl": playlist.get("artworkUrl"),
                "url": playlist.get("url"),
            }
            songs = [
                self.build_song(release, dict(track, trackNumber=index, discNumber=1))
                for index, track in enumerate(playlist["tracks"], start=1)
            ]
            return build_music_from_songs(
                songs,
                kind="playlist",
                title=release["title"],
                artist=release["artist"],
                owner=first_text(playlist.get("owner")) or None,
                description=first_text(playlist.get("description")) or None,
                empty_error="The playlist is empty or region-locked.",
            )

        artist = self.api("artist", artist_id=content_id)
        releases = artist["releases"]
        discography: list[Song] = []
        for index, release_id in enumerate(releases, start=1):
            with console.status(f"Loading release {index}/{len(releases)}..."):
                discography.extend(self.songs_from_album(release_id))
        if not discography:
            raise ValueError(f"The artist has no available releases: {artist['name']}")

        return Music(
            discography,
            kind="discography",
            title=f"{artist['name']} Discography",
            artist=artist["name"],
            year=None,
            total_tracks=len(discography),
            total_discs=1,
            total_duration=sum(int(first_number((song.data or {}).get("duration")) or 0) for song in discography),
            artwork_url=artist.get("artworkUrl"),
            description=first_text(artist.get("biography")) or None,
        )

    def music_from_album(self, release_id: str) -> Music:
        """An ALBUM, EP or SINGLE. All three come back from one endpoint here.

        `build_music_from_songs` is the idiomatic way to assemble a release. It reads the
        year, the track and disc totals, the artwork and the total duration back off
        the Songs, so build_song states each of those facts once instead of
        twice. Construct `Music` directly only when the header must say something the
        songs do not imply, as the discography branch above does.
        """
        release = self.api("album", release_id=release_id)
        songs = [self.build_song(release, track) for track in release["tracks"]]
        return build_music_from_songs(
            songs,
            kind=classify_release_kind(release.get("type", "album"), len(songs)),
            title=release["title"],
            artist=format_names(release.get("artists")) or "Unknown Artist",
            empty_error=f"The release has no available tracks: {release['title']}",
        )

    def songs_from_album(self, release_id: str) -> list[Song]:
        release = self.api("album", release_id=release_id)
        return [self.build_song(release, track) for track in release["tracks"]]

    def api(self, endpoint: str, **path_args: str) -> Any:
        response = self.session.get(
            url=self.config["endpoints"][endpoint].format(**path_args),
            params={"token": self.token},
        )
        response.raise_for_status()
        return response.json()

    def build_song(self, release: dict, track: dict) -> Song:
        """One Song per track. `name`, `artist`, `album` and `track` are necessary.
        Everything else is optional, and the service leaves it out when the API cannot say.

        Music APIs are inconsistent about the shape of every one of these fields, which
        is why `unshackle.core.music` ships normalizers. Use them instead of writing
        the same defensive getters in each service.
        """
        release_date = first_text(release.get("releaseDate"))
        return Song(
            id_=track["id"],
            service=self.__class__,
            name=first_text(track.get("title")),
            artist=format_names(track.get("artists")) or "Unknown Artist",
            album=release["title"],
            track=track["trackNumber"],
            disc=track.get("discNumber", 1),
            year=year_from_value(release_date) if release_date else None,
            language=release.get("language") or "und",
            album_artist=format_names(release.get("artists")) or None,
            release_type=release.get("type", "album"),
            total_tracks=release.get("trackCount"),
            total_discs=release.get("discCount"),
            genre=first_text(track.get("genre"), release.get("genre")) or None,
            explicit=track.get("explicit"),
            isrc=track.get("isrc"),
            upc=release.get("upc"),
            copyright=first_text(track.get("copyright"), release.get("copyright")) or None,
            label=first_text(release.get("label")) or None,
            lyrics=self.get_lyrics(track) if self.lyrics else None,
            artwork_url=first_text(track.get("artworkUrl"), release.get("artworkUrl")) or None,
            data={
                "quality": first_text(track.get("qualityLabel")),
                "duration": duration_seconds(track.get("durationMs")),
                "hires": track.get("hires"),
                "lossless": track.get("lossless"),
                "unavailable_reason": None if track.get("streamable", True) else "Not available in this region",
                "release_date": release_date,
                "composer": first_text(track.get("composer")),
                "service": "MUSIC_EXAMPLE",
                "track_id": track["id"],
                "album_id": release["id"],
                "track_url": track.get("url"),
                "album_url": release.get("url"),
            },
        )

    def get_lyrics(self, track: dict) -> Optional[str]:
        response = self.session.get(
            url=self.config["endpoints"]["lyrics"].format(track_id=track["id"]),
            params={"token": self.token},
        )
        if not response.ok:
            return None
        return response.json().get("lyrics")

    def get_tracks(self, title: Title_T) -> Tracks:
        """Return ONE Audio track per codec+bitrate the source offers for this Song.

        This is the whole music contract. Do not pick the best format here: hand the
        framework every option the account can fetch and let its normal audio
        selection (-a/--acodec, --abitrate, sort_audio, select_best_audio) choose.
        Since Audio has no bit_depth or sample_rate, `bitrate` is what separates a
        24-bit FLAC from a 16-bit FLAC, and it is what sort_audio ranks them by.

        MIGRATING FROM THE OLD MODEL: a music service used to return exactly one Audio
        track and do its own quality fallback inside an overridden Audio.download().
        Do not. Nothing downstream can see past a single track, so the user cannot ask
        for the CD master, cannot see what exists, and cannot tell what they got.
        """
        reason = (title.data or {}).get("unavailable_reason")
        if reason:
            raise ValueError(f"{title.name}: {reason}")

        streams = self.session.get(
            url=self.config["endpoints"]["streams"].format(track_id=title.id),
            params={
                "token": self.token,
                "formats": ",".join(self.config["client"][self.device]["formats"]),
                "tier": self.tier,
            },
        ).json()["streams"]

        tracks = Tracks()
        for stream in streams:
            tracks.add(
                Audio(
                    id_=f"{title.id}-{stream['format']}",
                    url=stream["url"],
                    codec=Audio.Codec.from_mime(stream["mime"]),
                    bitrate=stream["bitrate"],
                    channels=stream.get("channels", 2),
                    name=first_text(stream.get("qualityLabel")) or None,
                    language=title.language or "und",
                    is_original_lang=True,
                    descriptor=Audio.Descriptor.URL,
                    session=self.session,
                    data=stream,
                )
            )

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()

