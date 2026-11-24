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
from unshackle.core.manifests import DASH
from unshackle.core.search_result import SearchResult
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from unshackle.core.tracks import Attachment, Chapter, Chapters, Subtitle, Tracks, Video
from unshackle.core.utilities import is_close_match


class EXAMPLE(Service):
    """
    Reference service for domain.com - a deliberately exhaustive showcase of
    EVERYTHING an unshackle service can touch. It is NOT meant to operate against
    a real API. It exists so a new service author can see one canonical example of
    every framework feature in one place. Music is the one exception: it has its
    own reference service, MUSIC_EXAMPLE.

    Version: 2.1.0
    Author: sp4rk.y
    Date: 2026-08-23
    Authorization: Cookies + Credentials
    Geofence: US, UK
    Robustness:
        Widevine:
            L1: 2160p, HDR10, HDR10+, DV
            L3: 1080p, SDR
        PlayReady:
            SL3000: 2160p
        ClearKey: 1080p (DRM-free fallback)

    Tips:
        - Input may be a full URL or a bare ID/slug:
            https://domain.com/details/20914   ->   20914
        - -m / --movie forces movie parsing when the API type is ambiguous.
        - -d / --device selects a client profile block from config.yaml.

    Feature map (where to look):
        __init__              TrackRequest read/override, CDM-aware codec gating
        authenticate          cookies AND credentials, JWT decode, token cache+refresh
        search                SearchResult generator
        get_titles            Movies / Series + data passthrough
        get_tracks            DASH variant fan-out (default) + HLS/ISM alternates
        fetch_dash_manifest  range override, HDR10+ flip, DV-composite, Atmos,
                              descriptive audio, channel fixups, cover-art attachment,
                              VUI normalise, bitrate-window awareness,
                              drm_preference pinning for dual-PSSH manifests
        get_chapters          Chapters() with named + unnamed markers
        get_widevine_*        service certificate + license (per-segment PSSH through `track`)
        get_playready_license PlayReady challenge POST
        get_clearkey_license  DASH org.w3.clearkey JWK Set POST (Laurl fallback)
    """

    ALIASES = ("EX", "DOMAIN")
    GEOFENCE = ("US", "UK")
    TITLE_RE = r"^(?:https?://(?:www\.)?domain\.com/details/)?(?P<title_id>[^/?#]+)"
    NO_SUBTITLES = False
    ANIME = True
    ANONYMOUS_SEARCH = False
    VAULT_TAG = "DIFFERENT_NAME"

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
        self.title = title
        self.movie = movie
        self.device = device

        super().__init__(ctx)

        self.cdm = ctx.obj.cdm

        self.is_playready = is_playready_cdm(self.cdm)
        self.is_widevine = is_widevine_cdm(self.cdm)

        if self.is_playready:
            self.device = "playready_tv"
            self.log.info(" + PlayReady CDM detected - using PlayReady device profile")
        elif self.is_widevine:
            self.log.info(" + Widevine CDM detected")

        self.profile = (ctx.parent.params.get("profile") if ctx.parent else None) or "default"


        if any(r != Video.Range.SDR for r in self.track_request.ranges):
            self.track_request.codecs = [Video.Codec.HEVC]

        if self.is_widevine and getattr(self.cdm, "security_level", None) == 3:
            self.log.warning(" ! L3 CDM detected - clamping to AVC/SDR")
            self.track_request.codecs = [Video.Codec.AVC]
            self.track_request.ranges = [Video.Range.SDR]

        if self.config is None:
            raise EnvironmentError("config.yaml is missing for this service.")

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)

        self.session.headers.update({"user-agent": self.config["client"][self.device]["user_agent"]})

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
            if cookies:
                jwt_token = next((c.value for c in cookies if c.name == "streamco_token"), None)
                if not jwt_token:
                    raise EnvironmentError("Cookie 'streamco_token' not found in jar.")
                payload = json.loads(base64.urlsafe_b64decode(jwt_token.split(".")[1] + "==").decode())
                body = {"token": jwt_token, "profileId": payload.get("profileId")}
            elif credential:
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
        original_lang = Language.find(metadata["languages"][0])

        if self.movie or program_type == "movie":
            return Movies(
                [
                    Movie(
                        id_=metadata["id"],
                        service=self.__class__,
                        name=metadata["title"],
                        description=metadata.get("description"),
                        year=metadata["releaseYear"] if metadata.get("releaseYear", 0) > 0 else None,
                        language=original_lang,
                        data=metadata,
                    )
                ]
            )

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
                        absolute=ep.get("absoluteNumber"),
                    )
                )
                episodes[-1].anime = metadata.get("category") == "anime"
        return Series(episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        def fetch_variant(title: Title_T, codec: Optional[Video.Codec], range_: Video.Range) -> Tracks:
            vcodec_str = "H265" if codec == Video.Codec.HEVC else "H264"
            self.log.info(f" + Fetching {vcodec_str} {range_.name} manifest")
            tracks = self.fetch_dash_manifest(title, vcodec=vcodec_str, range_=range_)

            if range_ in (Video.Range.HDR10, Video.Range.HDR10P, Video.Range.DV):
                if not any(v.range == range_ for v in tracks.videos):
                    raise ValueError(f"{range_.name} requested but unavailable")
            return tracks

        return self.get_tracks_for_variants(title, fetch_variant)


    def fetch_dash_manifest(
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
                "drm": "playready" if self.is_playready else "widevine",
            },
        ).json()["media"]

        self.license_data = {
            "url": streams["drm"]["url"],
            "data": streams["drm"]["data"],
            "session": streams["drm"]["session"],
        }

        manifest_url = streams["url"].split("?")[0]
        self.log.debug(f"Manifest URL: {manifest_url}")
        tracks = DASH.from_url(url=manifest_url, session=self.session).to_tracks(language=title.language)

        for video in tracks.videos:
            if video.height and video.height > 1080:
                video.drm_preference = "playready" if self.is_playready else "widevine"
            else:
                video.drm_preference = "widevine"

        for audio in tracks.audio:
            audio.drm_preference = tracks.videos[0].drm_preference if tracks.videos else "widevine"

        for video in tracks.videos:
            video.range = range_

            if range_ == Video.Range.HDR10P:
                video.range = Video.Range.HDR10P

            if range_ == Video.Range.DV and vcodec == "H265":
                video.dv_compatible_bitstream = True


        tracks.audio = [a for a in tracks.audio if "clear" not in (a.data["dash"]["representation"].get("id") or "")]
        for audio in tracks.audio:
            if audio.channels == 6.0:
                audio.channels = 5.1
            label = audio.data["dash"]["adaptation_set"].get("label") or ""
            if "Audio Description" in label or "description" in label.lower():
                audio.descriptive = True
            if audio.data.get("isAtmos"):
                audio.joc = 16
            if audio.is_original_lang:
                audio.name = "Original"

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
                chapters.add(Chapter(timestamp=chapter["end"]))
            elif chapter["name"] == "Credits":
                chapters.add(Chapter(timestamp=chapter["start"], name="Credits"))
        return chapters

    def get_widevine_service_certificate(self, **_: Any) -> Optional[str]:
        return self.config.get("certificate")

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
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
        license_url = self.config["endpoints"].get("clearkey_license")
        if not license_url:
            return None
        response = self.session.post(url=license_url, data=challenge)
        response.raise_for_status()
        return response.json()

