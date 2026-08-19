from __future__ import annotations

import re
from collections.abc import Generator
from datetime import datetime
from http.cookiejar import CookieJar
from typing import Any, Optional

import click

from unshackle.core.console import console
from unshackle.core.constants import AnyTrack
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
        on_track_downloaded   restore the real container suffix before tagging

    There is no music-specific Service API. A `Song` is an ordinary `Title` on the
    one generic download path, so everything else a music service needs (auth,
    search, DRM license callbacks, proxies) works exactly as it does in EXAMPLE -
    read that service for the DRM and manifest sides, which are not repeated here.
    """

    # ALIASES: extra CLI tags that resolve to this service (e.g. `dl MUSICEX ...`).
    ALIASES = ("MUSICEX", "MUSICDOMAIN")
    # GEOFENCE: regions required; the framework warns/blocks if proxy region mismatches.
    GEOFENCE = ("US", "UK")
    # TITLE_RE: named groups (?P<...>) parsed in get_titles(). Accepts URL or bare id.
    # A music catalogue is addressed by several kinds of thing, so `kind`
    # is part of the identifier and decides which endpoint get_titles() asks.
    TITLE_RE = (
        r"^(?:https?://(?:www\.)?musicdomain\.com/)?(?P<kind>track|album|playlist|artist)/(?P<content_id>[^/?#]+)"
    )
    # NO_SUBTITLES: a music catalogue never ships subtitle tracks.
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

    def __init__(self, ctx: click.Context, title: str, device: str, lyrics: bool):
        self.title = title
        self.device = device
        self.lyrics = lyrics

        # super().__init__ wires up self.config, self.log, self.session (rnet TLS),
        # self.cache, self.title_cache, self.request_input, self.current_region,
        # and builds self.track_request from the global `dl` flags.
        super().__init__(ctx)

        # `dl` global flags live on the parent context. Profile picks cookie/cred set.
        self.profile = (ctx.parent.params.get("profile") if ctx.parent else None) or "default"

        # self.track_request.codecs / .ranges are VIDEO concepts, so a music service
        # leaves them alone. `--quality` is a video height and never reaches audio
        # selection either. The user narrows audio with --acodec and --abitrate, which
        # the framework applies to whatever get_tracks() returns.

        if self.config is None:
            raise EnvironmentError("config.yaml is missing for this service.")

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        # Loads the cookie jar into self.session and stores self.credential.
        super().authenticate(cookies, credential)

        # Per-device UA from config. Never hardcode UAs in code.
        self.session.headers.update({"user-agent": self.config["client"][self.device]["user_agent"]})

        # Token cache keyed by device + profile so multiple profiles don't collide.
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
        # The account's subscription tier decides which formats get_tracks() may ask
        # for. Entitlement is a service concern; never filter tracks on it in
        # get_tracks() - return everything the account can actually fetch and let the
        # framework's audio selection choose.
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

        # ONE TRACK. A `Music` of one Song, not a bare Song: the framework downloads a
        # collection, and the "track" kind is what tells the listing header to say so.
        # Note the helper takes the header's track and disc totals off the Song, which
        # still carries its album's totals, because the TRACKNUMBER tag needs them.
        # Build `Music` directly, as the discography branch does, to say otherwise.
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

        # PLAYLIST. Its tracks come from many albums, so synthesise the release dict
        # that build_song reads. A playlist also renumbers its tracks, so the album
        # track and disc numbers on each track are meaningless here.
        if kind == "playlist":
            playlist = self.api("playlist", release_id=content_id)
            release = {
                "id": playlist["id"],
                "title": playlist["title"],
                # `format_names` flattens the shapes an API returns for an artist field
                # (a string, a dict, or a list of dicts) into one de-duplicated string.
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
                # `owner` and `description` belong to the playlist, not to any album.
                owner=first_text(playlist.get("owner")) or None,
                description=first_text(playlist.get("description")) or None,
                empty_error="The playlist is empty or region-locked.",
            )

        # ARTIST DISCOGRAPHY - the release kind `build_music_from_songs` cannot build, so
        # this branch constructs Music directly and states every header row itself.
        artist = self.api("artist", artist_id=content_id)
        releases = artist["releases"]
        discography: list[Song] = []
        # N+1 HAZARD: one request per release, and every one of them runs before the
        # user sees a single track. A large discography is minutes of silence. Prefer a
        # bulk endpoint when the API has one, and keep the user informed when it does
        # not. A production service can go further and subclass `Music` to load its
        # songs on first access, so the listing appears immediately.
        for index, release_id in enumerate(releases, start=1):
            with console.status(f"Loading release {index}/{len(releases)}..."):
                discography.extend(self.songs_from_album(release_id))
        if not discography:
            raise ValueError(f"The artist has no available releases: {artist['name']}")

        return Music(
            discography,
            # An unknown kind is title-cased for the Type row, so a service is not
            # limited to the kinds `classify_release_kind` returns.
            kind="discography",
            title=f"{artist['name']} Discography",
            artist=artist["name"],
            # These are the header rows the helper would fill from the FIRST song,
            # which is one album's answer and wrong for a discography. A None does not
            # blank a row: the renderer falls back to the first song for the title,
            # the artist and the year, so only the counts and the duration below are
            # really the collection's to state.
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
            # `classify_release_kind` needs the REAL track count. Many APIs label a
            # multi-track EP a "single", and only the count separates the two.
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
            # `disc` defaults to 1, so a single-disc release need not pass it.
            disc=track.get("discNumber", 1),
            # `year` is OPTIONAL. An undated single or a promo release passes None here
            # and the year row simply disappears from the header and the filename.
            # `year_from_value` falls back to 1900 on an unparseable value, so guard it
            # with the date instead of writing a wrong year onto an undated release.
            year=year_from_value(release_date) if release_date else None,
            # The ORIGINAL recorded language, same meaning as on a Movie or an Episode.
            # Always resolve it to something: `-l` defaults to `orig`, `orig` resolves off
            # THIS field, and a Song without one resolves to no language at all, so audio
            # selection keeps nothing and the run stops on "There's no orig Audio Track".
            # "und" is the honest answer when a catalogue does not say; it matches the
            # tracks below, which carry the same tag.
            language=release.get("language") or "und",
            # `album_artist` differs from `artist` on compilations and playlists, and it
            # is what the folder template and the ALBUMARTIST tag prefer.
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
            # The tagger writes this to LYRICS on a FLAC, USLT on an MP3 and ©lyr on an
            # M4A. It costs a request per track, so it stays behind the flag.
            lyrics=self.get_lyrics(track) if self.lyrics else None,
            # Per-track artwork wins over the release artwork; the tagger downloads it
            # with the service session and embeds it.
            artwork_url=first_text(track.get("artworkUrl"), release.get("artworkUrl")) or None,
            # `data` is a free-form passthrough, read later as title.data. Keep every key
            # FLAT: the tagger reads the Song's data dict itself, so a nested sub-dict is
            # silently dropped and its tags never reach the file. The renderer takes
            # `quality`, `duration`, `hires`, `lossless` and `atmos` for the line under
            # each track, plus `unavailable_reason` for a "Skipped:" line in place of it.
            # The tagger takes `release_date`, `composer`, `service` and the id/url pairs.
            data={
                # Audio has no bit_depth or sample_rate field, so the human-readable
                # quality string lives here. The renderer parses "24-bit"/"96 kHz" out
                # of it to decide the Hi-Res badge, and it becomes the COMMENT tag.
                "quality": first_text(track.get("qualityLabel")),
                # `duration_seconds` reads seconds or milliseconds, because an API that
                # calls the field `durationMs` is as common as one that does not.
                "duration": duration_seconds(track.get("durationMs")),
                "hires": track.get("hires"),
                "lossless": track.get("lossless"),
                # Say WHY a track cannot be fetched instead of letting it fail later.
                # The listing then shows "Skipped: ..." under it, and get_tracks reads
                # the same key back.
                "unavailable_reason": None if track.get("streamable", True) else "Not available in this region",
                # The full date, where `year` above is only the year, so DATE and
                # RELEASEDATE carry the day rather than "2020".
                "release_date": release_date,
                "composer": first_text(track.get("composer")),
                # `service` names the tag prefix for the id/url pairs below, which the
                # tagger writes as MUSIC_EXAMPLE_TRACK_ID and friends.
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
            # The listing already showed this track as skipped. Fail with the same
            # reason rather than downloading something the user was told to expect.
            raise ValueError(f"{title.name}: {reason}")

        streams = self.session.get(
            url=self.config["endpoints"]["streams"].format(track_id=title.id),
            params={
                "token": self.token,
                # Ask only for what this client profile and tier can actually play,
                # so the user is never offered a format the API will refuse.
                "formats": ",".join(self.config["client"][self.device]["formats"]),
                "tier": self.tier,
            },
        ).json()["streams"]

        tracks = Tracks()
        for stream in streams:
            tracks.add(
                Audio(
                    # Every option needs its own id or selection breaks, and APIs
                    # often reuse one file id across qualities. Composing the song id
                    # with the format can never collide.
                    id_=f"{title.id}-{stream['format']}",
                    url=stream["url"],
                    # from_mime maps the API's MIME/4CC to the Audio.Codec enum, which
                    # is what --acodec matches against.
                    codec=Audio.Codec.from_mime(stream["mime"]),
                    # Bits per second; the framework floors to kb/s for --abitrate.
                    # Two FLAC options differ only by bitrate, so `sort_audio` ranks
                    # them on it and takes the hi-res one by default. `--acodec` picks
                    # the codec. `--abitrate` matches an exact kb/s, which suits a
                    # constant-bitrate AAC; use `--abitrate-range` for a FLAC.
                    bitrate=stream["bitrate"],
                    channels=stream.get("channels", 2),
                    # `name` is the human-readable quality label. Audio has no
                    # bit_depth or sample_rate, so this is the only place a user reads
                    # "24-bit / 96 kHz" in the Available Tracks listing.
                    name=first_text(stream.get("qualityLabel")) or None,
                    # A Track must have a language even where the catalogue gives none.
                    # build_song falls back to the BCP-47 "undetermined" tag for that
                    # reason, so mirror it here rather than guessing at English; a
                    # playlist of many languages has no honest single answer.
                    language=title.language or "und",
                    # A song is in its own original language by definition, and the
                    # framework only learns that from to_tracks() on a manifest. A
                    # hand-built track has to say so itself.
                    is_original_lang=True,
                    # Descriptor.URL: one whole file behind one URL, not a manifest.
                    # A service that serves music over DASH/HLS instead should use the
                    # manifest parsers exactly as EXAMPLE does.
                    descriptor=Audio.Descriptor.URL,
                    session=self.session,
                    data=stream,
                )
            )

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        # A song has no chapters. Return an empty Chapters() rather than raising; the
        # base class requires the method and the pipeline reads it for every title.
        return Chapters()

    def on_track_downloaded(self, track: AnyTrack) -> None:
        # Every track downloads to a temp file named `.mp4` whatever it holds, and a
        # Song skips the muxer entirely, so its download IS the delivered file, suffix
        # and all. The tagger picks its metadata format off that suffix, so restore the
        # real container here or a FLAC ships as a `.mp4` the tagger then fails to open
        # as one.
        if not isinstance(track, Audio) or not track.path or not track.codec:
            return
        # This event fires twice, once before decryption and once at the end of the
        # download. Decryption still reads the original path, so a track that is still
        # encrypted has to wait for the second call, by which time drm is cleared.
        if track.drm:
            return
        suffix = self.config["containers"].get(track.codec.name)
        if not suffix or track.path.suffix == suffix:
            return
        track.path = track.path.rename(track.path.with_suffix(suffix))

    # DRM is identical to any other service: implement get_widevine_license (and the
    # PlayReady/ClearKey callbacks) and mark the Audio tracks with the DRM the API
    # reports. See the EXAMPLE service for those; nothing about them is music-specific.
