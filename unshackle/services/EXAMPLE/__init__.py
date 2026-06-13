from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Generator
from datetime import datetime
from http.cookiejar import CookieJar
from typing import Any, Optional, Union

import click
from langcodes import Language

from unshackle.core.cdm.detect import is_playready_cdm, is_widevine_cdm
from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.manifests import DASH  # also: HLS, ISM - see get_tracks() alternates
from unshackle.core.search_result import SearchResult
from unshackle.core.service import Service
from unshackle.core.titles import Album, Episode, Movie, Movies, Series, Song, Title_T, Titles_T
from unshackle.core.tracks import Attachment, Chapter, Chapters, Subtitle, Tracks, Video
from unshackle.core.utilities import is_close_match


class EXAMPLE(Service):
    """
    \b
    Reference service for domain.com - a deliberately exhaustive showcase of
    EVERYTHING an unshackle service can touch. It is NOT meant to run against a
    real API; it exists so a new service author can see one canonical example of
    every framework feature in one place.

    \b
    Version: 2.0.0
    Author: sp4rk.y
    Authorization: Cookies + Credentials
    Geofence: US, UK
    Robustness:
        Widevine:
            L1: 2160p, HDR10, HDR10+, DV
            L3: 1080p, SDR
        PlayReady:
            SL3000: 2160p
        ClearKey: 1080p (DRM-free fallback)

    \b
    Tips:
        - Input may be a full URL or a bare ID/slug:
            https://domain.com/details/20914   ->   20914
        - -m / --movie forces movie parsing when the API type is ambiguous.
        - -d / --device selects a client profile block from config.yaml.

    \b
    Feature map (where to look):
        __init__              TrackRequest read/override, CDM-aware codec gating
        authenticate          cookies AND credentials, JWT decode, token cache+refresh
        search                SearchResult generator
        get_titles            Movies / Series / Album (music) + data passthrough
        get_tracks            DASH variant fan-out (default) + HLS/ISM alternates
        _fetch_dash_manifest  range override, HDR10+ flip, DV-composite, Atmos,
                              descriptive audio, channel fixups, cover-art attachment,
                              VUI normalize, bitrate-window awareness
        get_chapters          Chapters() with named + unnamed markers
        get_widevine_*        service cert + license (per-segment PSSH via `track`)
        get_playready_license PlayReady challenge POST
        get_clearkey_license  DASH org.w3.clearkey JWK Set POST (Laurl fallback)
    """

    # ALIASES: extra CLI tags that resolve to this service (e.g. `dl EX ...`).
    ALIASES = ("EX", "DOMAIN")
    # GEOFENCE: regions required; the framework warns/blocks if proxy region mismatches.
    GEOFENCE = ("US", "UK")
    # TITLE_RE: named groups (?P<...>) parsed in get_titles(). Accepts URL or bare id.
    TITLE_RE = r"^(?:https?://(?:www\.)?domain\.com/details/)?(?P<title_id>[^/?#]+)"
    # NO_SUBTITLES: service-level idiom telling the pipeline subs are handled in-band.
    NO_SUBTITLES = False
    # VAULT_TAG: store/read keys under a different vault namespace than this service's tag.
    # Lets sibling services share one key vault. Omit to use the service's own tag.
    VAULT_TAG = "DIFFERENT_NAME"

    # Map our API's range strings <-> unshackle's Video.Range enum.
    VIDEO_RANGE_MAP = {
        Video.Range.SDR: "sdr",
        Video.Range.HLG: "hlg",
        Video.Range.HDR10: "hdr10",
        Video.Range.HDR10P: "hdr10plus",
        Video.Range.DV: "dolby_vision",
    }

    @staticmethod
    @click.command(name="EXAMPLE", short_help="https://domain.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Treat the title as a movie.")
    @click.option(
        "-d",
        "--device",
        type=click.Choice(["android_tv", "web", "ios"], case_sensitive=False),
        default="android_tv",
        help="Client profile block to use from config.yaml.",
    )
    @click.pass_context
    def cli(ctx: click.Context, **kwargs: Any) -> EXAMPLE:
        return EXAMPLE(ctx, **kwargs)

    def __init__(self, ctx: click.Context, title: str, movie: bool, device: str):
        # Store CLI args BEFORE super().__init__ if the base needs them; here order
        # doesn't matter, but storing first is the common convention.
        self.title = title
        self.movie = movie
        self.device = device

        # super().__init__ wires up self.config, self.log, self.session (rnet TLS),
        # self.cache, self.title_cache, self.request_input, self.current_region,
        # and builds self.track_request from the global `dl` flags.
        super().__init__(ctx)

        # The selected CDM (Widevine OR PlayReady). May be None for DRM-free runs.
        self.cdm = ctx.obj.cdm

        # `is_playready_cdm` / `is_widevine_cdm` (unshackle.core.cdm.detect) classify
        # BOTH local CDMs (pyplayready / pywidevine) AND remote/wrapper CDMs by
        # inspecting the object - never hand-roll `isinstance` checks in a service.
        # Many services pick a device profile / manifest variant off this, because a
        # PlayReady box and a Widevine box often need different stream endpoints.
        self.is_playready = is_playready_cdm(self.cdm)
        self.is_widevine = is_widevine_cdm(self.cdm)

        # Swap the client profile to match the CDM - a PlayReady box and a Widevine
        # box frequently register as different device types with the service.
        if self.is_playready:
            self.device = "playready_tv"
            self.log.info(" + PlayReady CDM detected - using PlayReady device profile")
        elif self.is_widevine:
            self.log.info(" + Widevine CDM detected")

        # `dl` global flags live on the parent context. Profile picks cookie/cred set.
        self.profile = (ctx.parent.params.get("profile") if ctx.parent else None) or "default"

        # self.track_request.codecs : list[Video.Codec]   (empty == accept any)
        # self.track_request.ranges : list[Video.Range]   (defaults to [SDR])
        #
        # Services may REWRITE the request before tracks are fetched. Two common rules:

        # 1) HDR/DV needs HEVC on this service - force it.
        if any(r != Video.Range.SDR for r in self.track_request.ranges):
            self.track_request.codecs = [Video.Codec.HEVC]

        # 2) CDM-aware gating. A Widevine L3 box can't pull UHD/HDR here, so clamp it.
        #    (security_level is a Widevine concept; PlayReady exposes SL via its own API.)
        if self.is_widevine and getattr(self.cdm, "security_level", None) == 3:
            self.log.warning(" ! L3 CDM detected - clamping to AVC/SDR")
            self.track_request.codecs = [Video.Codec.AVC]
            self.track_request.ranges = [Video.Range.SDR]

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
        elif cache and cache.data.get("refresh_token"):
            self.log.info(" + Refreshing tokens")
            refresh = self.session.post(
                url=self.config["endpoints"]["refresh"],
                data={"refresh_token": cache.data["refresh_token"]},
            ).json()
            cache.set(data=refresh, expiration=refresh.get("expires_in"))
        else:
            # Two interchangeable auth paths shown: a cookie-borne JWT, or email/pass.
            if cookies:
                jwt_token = next((c.value for c in cookies if c.name == "streamco_token"), None)
                if not jwt_token:
                    raise EnvironmentError("Cookie 'streamco_token' not found in jar.")
                payload = json.loads(base64.urlsafe_b64decode(jwt_token.split(".")[1] + "==").decode())
                body = {"token": jwt_token, "profileId": payload.get("profileId")}
            elif credential:
                # `request_input` works locally AND over `serve` (InputBridge relays it).
                otp = self.request_input("Enter the OTP sent to your device: ")
                body = {"username": credential.username, "password": credential.password, "otp": otp}
            else:
                raise EnvironmentError("Service requires either Cookies or Credentials.")

            token = self.session.post(url=self.config["endpoints"]["login"], data=body).json()
            cache.set(data=token, expiration=token.get("expires_in"))

        self.token = cache.data["token"]
        self.user_id = cache.data.get("userId")

    def search(self) -> Generator[SearchResult, None, None]:
        results = self.session.get(
            url=self.config["endpoints"]["search"],
            params={"q": self.title, "token": self.token},
        ).json()

        for result in results["entries"]:
            yield SearchResult(
                id_=result["id"],
                title=result["title"],
                description=result.get("description"),
                label="SERIES" if result["programType"] == "series" else result["programType"].upper(),
                url=result.get("url"),
            )

    def get_titles(self) -> Titles_T:
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError("Could not parse a title ID - is the URL/ID correct?")
        title_id = match.group("title_id")

        metadata = self.session.get(
            url=self.config["endpoints"]["metadata"].format(title_id=title_id),
            params={"token": self.token},
        ).json()

        program_type = metadata.get("programType")
        # Resolve the title's ORIGINAL recorded language from API metadata. Stored on
        # every Title as `language` and later handed to `to_tracks(language=...)`, which
        # is the single source of truth the manifest parsers use to flag is_original_lang.
        # This drives `-l best/all` original-audio selection and the filename language token.
        original_lang = Language.find(metadata["languages"][0])

        # MUSIC - Album of Song titles. Showcases the music branch of the title system.
        if program_type == "album":
            return Album(
                [
                    Song(
                        id_=tr["id"],
                        service=self.__class__,
                        name=tr["title"],
                        artist=metadata["artist"],
                        album=metadata["title"],
                        track=tr["trackNumber"],
                        disc=tr.get("discNumber", 1),
                        year=metadata["releaseYear"],
                        language=original_lang,
                        data=tr,
                    )
                    for tr in metadata["tracks"]
                ]
            )

        # MOVIE
        if self.movie or program_type == "movie":
            return Movies(
                [
                    Movie(
                        id_=metadata["id"],
                        service=self.__class__,
                        name=metadata["title"],
                        description=metadata.get("description"),
                        year=metadata["releaseYear"] if metadata.get("releaseYear", 0) > 0 else None,
                        # `language` should be the ORIGINAL audio language - drives the
                        # filename metadata token, not the user's preferred -l language.
                        language=original_lang,
                        data=metadata,  # passthrough - read later as title.data
                    )
                ]
            )

        # SERIES - flatten seasons into Episodes (skip trailer "seasons").
        episodes = []
        for season in metadata["seasons"]:
            if "Trailers" in season["title"]:
                continue
            season_data = self.session.get(url=season["url"], params={"token": self.token}).json()
            for ep in season_data["entries"]:
                episodes.append(
                    Episode(
                        id_=ep["id"],
                        service=self.__class__,
                        title=metadata["title"],
                        season=ep["season"],
                        number=ep["episode"],
                        name=ep.get("title"),
                        description=ep.get("description"),
                        year=metadata["releaseYear"] if metadata.get("releaseYear", 0) > 0 else None,
                        language=original_lang,
                        data=ep,
                    )
                )
        return Series(episodes)

    # DEFAULT (shown live): this service needs a SEPARATE manifest per codec/range,
    # so we fan out with the base helper `_get_tracks_for_variants`. It walks every
    # codec x range in the TrackRequest, handles HYBRID (HDR10 + DV merge), and -
    # when --best-available is set - skips combos the service can't deliver.
    def get_tracks(self, title: Title_T) -> Tracks:
        def _fetch_variant(title: Title_T, codec: Optional[Video.Codec], range_: Video.Range) -> Tracks:
            vcodec_str = "H265" if codec == Video.Codec.HEVC else "H264"
            self.log.info(f" + Fetching {vcodec_str} {range_.name} manifest")
            tracks = self._fetch_dash_manifest(title, vcodec=vcodec_str, range_=range_)

            # Guard: if we asked for HDR/DV but the manifest came back SDR-only, raise
            # so the helper can fall back (best-available) or fail loudly otherwise.
            if range_ in (Video.Range.HDR10, Video.Range.HDR10P, Video.Range.DV):
                if not any(v.range == range_ for v in tracks.videos):
                    raise ValueError(f"{range_.name} requested but unavailable")
            return tracks

        return self._get_tracks_for_variants(title, _fetch_variant)

    # ── ALTERNATE A - HLS (one master playlist returns every codec/range) ───────
    # When a service exposes a single master playlist, you do NOT need the variant
    # fan-out; dl.py filters by the user's selection. Just parse and return:
    #
    #   def get_tracks(self, title: Title_T) -> Tracks:
    #       playback = self.session.get(
    #           url=self.config["endpoints"]["playback"].format(title_id=title.id),
    #           params={"token": self.token},
    #       ).json()
    #       return HLS.from_url(url=playback["manifest_url"], session=self.session) \
    #                 .to_tracks(language=title.language)
    #
    # ── ALTERNATE B - ISM (Microsoft Smooth Streaming) ──────────────────────────
    #   from unshackle.core.manifests import ISM
    #   return ISM.from_url(url=ism_url, session=self.session).to_tracks(title.language)

    def _fetch_dash_manifest(
        self, title: Title_T, vcodec: str = "H264", range_: Video.Range = Video.Range.SDR
    ) -> Tracks:
        video_format = self.VIDEO_RANGE_MAP.get(range_, "sdr")

        streams = self.session.post(
            url=self.config["endpoints"]["streams"],
            params={"token": self.token, "guid": title.id},
            data={
                "type": self.config["client"][self.device]["type"],
                "video_format": video_format,
                "video_codec": vcodec,
                # Ask the API for the protection system our CDM actually speaks.
                "drm": "playready" if self.is_playready else "widevine",
            },
        ).json()["media"]

        # Stash DRM bits for the license callbacks (per-title, set just-in-time).
        self.license_data = {
            "url": streams["drm"]["url"],
            "data": streams["drm"]["data"],
            "session": streams["drm"]["session"],
        }

        manifest_url = streams["url"].split("?")[0]
        self.log.debug(f"Manifest URL: {manifest_url}")
        # DASH parser auto-extracts PSSH, segment timelines, SIDX, multi-period dedup.
        # Passing `language=title.language` (the ORIGINAL recorded language resolved in
        # get_titles) is what lets the parser flag is_original_lang on each track: for
        # every track it runs is_close_match(track_language, [title.language]) and sets
        # the flag. It also backfills that language onto any track the manifest leaves
        # unlabelled. HLS/ISM accept the same `language=` arg identically.
        tracks = DASH.from_url(url=manifest_url, session=self.session).to_tracks(language=title.language)

        for video in tracks.videos:
            # The manifest can't always be trusted for range - stamp what we asked for.
            video.range = range_

            # HDR10+ is a BITSTREAM feature the manifest often labels as plain HDR10.
            # If this service is known to ship HDR10+ SEI, flip it so mediainfo agrees.
            if range_ == Video.Range.HDR10P:
                video.range = Video.Range.HDR10P

            # DV-composite: a stream carrying DV RPU NALs inside a container that only
            # signals HEVC. Setting this flag makes DVFixup round-trip the bitstream
            # through dovi_tool so the muxed MKV is recognised as Dolby Vision.
            if range_ == Video.Range.DV and vcodec == "H265":
                video.dv_compatible_bitstream = True

            # normalize_vui() runs automatically post-repackage on HDR tracks to rewrite
            # stale BT.709 SPS colour primaries; nothing to call here, just be aware.

        # Drop "clear"/unencrypted decoy renditions the API sometimes returns.
        tracks.audio = [a for a in tracks.audio if "clear" not in (a.data["dash"]["representation"].get("id") or "")]
        for audio in tracks.audio:
            # Normalize odd channel counts (6.0 -> 5.1) for correct filename tokens.
            if audio.channels == 6.0:
                audio.channels = 5.1
            # Mark descriptive / audio-description tracks from the AdaptationSet label.
            label = audio.data["dash"]["adaptation_set"].get("label") or ""
            if "Audio Description" in label or "description" in label.lower():
                audio.descriptive = True
            # Atmos: the framework detects JOC for filename/-l selection; if the API
            # tells us explicitly we can set it eagerly.
            if audio.data.get("isAtmos"):
                audio.joc = 16
            # is_original_lang was set by to_tracks (via the language= arg). Use it to
            # label the original audio so it reads "Original" in selection/output.
            if audio.is_original_lang:
                audio.name = "Original"

        # Subtitles built by hand (not via to_tracks) skip the parser's is_original_lang
        # pass, so determine it ourselves with the same helper the parser uses.
        tracks.subtitles.clear()
        for sub in streams.get("captions", []):
            sub_lang = Language.get(sub["language"])
            tracks.add(
                Subtitle(
                    id_=hashlib.md5(sub["url"].encode()).hexdigest()[0:6],
                    url=sub["url"],
                    codec=Subtitle.Codec.from_mime("vtt"),
                    language=sub_lang,
                    is_original_lang=is_close_match(sub_lang, [title.language]),
                    sdh=sub.get("sdh", False),
                    forced=sub.get("forced", False),
                )
            )

        if cover := title.data.get("coverUrl"):
            tracks.add(
                Attachment(
                    url=cover,
                    name="cover",
                    description="Cover art",
                    session=self.session,
                )
            )

        # Cache chapter data now so get_chapters() needs no extra request.
        if not self.movie:
            title.data["chapters"] = (
                self.session.get(
                    url=self.config["endpoints"]["metadata"].format(title_id=title.id),
                    params={"token": self.token},
                )
                .json()
                .get("chapters", [])
            )

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        chapters = Chapters()
        for chapter in title.data.get("chapters", []):
            if chapter["name"] == "Intro":
                chapters.add(Chapter(timestamp=chapter["start"], name="Opening"))
                chapters.add(Chapter(timestamp=chapter["end"]))  # unnamed marker = chapter break
            elif chapter["name"] == "Credits":
                chapters.add(Chapter(timestamp=chapter["start"], name="Credits"))
        return chapters

    def get_widevine_service_certificate(self, **_: Any) -> Optional[str]:
        # Returning the service cert enables privacy-mode license requests.
        return self.config.get("certificate")

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        # `track` is passed so you can use its per-segment PSSH if the service rotates keys.
        license_url = self.license_data.get("url") or self.config["endpoints"].get("widevine_license")
        if not license_url:
            raise ValueError("Widevine license endpoint not configured")

        response = self.session.post(
            url=license_url,
            data=challenge,
            params={"session": self.license_data.get("session"), "userId": self.user_id},
            headers={
                "dt-custom-data": self.license_data.get("data"),
                "user-agent": self.config["client"][self.device]["license_user_agent"],
            },
        )
        response.raise_for_status()
        # Services return the license either as raw bytes or wrapped in JSON.
        try:
            return response.json()["license"]
        except (ValueError, KeyError):
            return response.content

    def get_playready_license(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Optional[Union[bytes, str]]:
        license_url = self.config["endpoints"].get("playready_license")
        if not license_url:
            raise ValueError("PlayReady license endpoint not configured")
        response = self.session.post(
            url=license_url,
            data=challenge,
            headers={"user-agent": self.config["client"][self.device]["license_user_agent"]},
        )
        response.raise_for_status()
        return response.content

    def get_clearkey_license(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Optional[Union[bytes, str, dict]]:
        # DASH org.w3.clearkey: `challenge` is the W3C JSON license request; return the
        # JWK Set response. Omit this method entirely when the manifest carries a Laurl —
        # the framework then POSTs the challenge there with no service code at all.
        license_url = self.config["endpoints"].get("clearkey_license")
        if not license_url:
            return None  # fall back to the manifest-provided Laurl, if any
        response = self.session.post(url=license_url, data=challenge)
        response.raise_for_status()
        return response.json()

    # For HLS AES-128 ClearKey or unencrypted content there is no license callback;
    # the key comes from the manifest or a side endpoint and is attached to the
    # track's DRM directly. Vaults (`self.cache` is separate) cache KID:KEY so repeat
    # downloads skip the license round-trip entirely.
