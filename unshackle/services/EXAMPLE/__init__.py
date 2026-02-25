import base64
import hashlib
import json
import re
from collections.abc import Generator
from datetime import datetime
from http.cookiejar import CookieJar
from typing import Optional, Union

import click
from langcodes import Language

from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.manifests import DASH
from unshackle.core.search_result import SearchResult
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from unshackle.core.tracks import Chapter, Subtitle, Tracks, Video


class EXAMPLE(Service):
    """
    Service code for domain.com
    Version: 1.0.0

    Authorization: Cookies

    Security: FHD@L3

    Use full URL (for example - https://domain.com/details/20914) or title ID (for example - 20914).
    """

    TITLE_RE = r"^(?:https?://?domain\.com/details/)?(?P<title_id>[^/]+)"
    GEOFENCE = ("US", "UK")
    NO_SUBTITLES = True

    @staticmethod
    @click.command(name="EXAMPLE", short_help="https://domain.com")
    @click.argument("title", type=str)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Specify if it's a movie")
    @click.option("-d", "--device", type=str, default="android_tv", help="Select device from the config file")
    @click.pass_context
    def cli(ctx, **kwargs):
        return EXAMPLE(ctx, **kwargs)

    def __init__(self, ctx, title, movie, device):
        super().__init__(ctx)

        self.title = title
        self.movie = movie
        self.device = device
        self.cdm = ctx.obj.cdm

        # Get range parameter for HDR support
        range_param = ctx.parent.params.get("range_")
        self.range = range_param[0].name if range_param else "SDR"

        if self.config is None:
            raise Exception("Config is missing!")
        else:
            profile_name = ctx.parent.params.get("profile")
            if profile_name is None:
                profile_name = "default"
            self.profile = profile_name

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            raise EnvironmentError("Service requires Cookies for Authentication.")

        jwt_token = next((cookie.value for cookie in cookies if cookie.name == "streamco_token"), None)
        payload = json.loads(base64.urlsafe_b64decode(jwt_token.split(".")[1] + "==").decode("utf-8"))
        profile_id = payload.get("profileId", None)
        self.session.headers.update({"user-agent": self.config["client"][self.device]["user_agent"]})

        cache = self.cache.get(f"tokens_{self.device}_{self.profile}")

        if cache:
            if cache.data["expires_in"] > int(datetime.now().timestamp()):
                self.log.info("Using cached tokens")
            else:
                self.log.info("Refreshing tokens")

                refresh = self.session.post(
                    url=self.config["endpoints"]["refresh"], data={"refresh_token": cache.data["refresh_data"]}
                ).json()

                cache.set(data=refresh)

        else:
            self.log.info("Retrieving new tokens")

            token = self.session.post(
                url=self.config["endpoints"]["login"],
                data={
                    "token": jwt_token,
                    "profileId": profile_id,
                },
            ).json()

            cache.set(data=token)

        self.token = cache.data["token"]
        self.user_id = cache.data["userId"]

    def search(self) -> Generator[SearchResult, None, None]:
        search = self.session.get(
            url=self.config["endpoints"]["search"], params={"q": self.title, "token": self.token}
        ).json()

        for result in search["entries"]:
            yield SearchResult(
                id_=result["id"],
                title=result["title"],
                label="SERIES" if result["programType"] == "series" else "MOVIE",
                url=result["url"],
            )

    def get_titles(self) -> Titles_T:
        self.title = re.match(self.TITLE_RE, self.title).group(1)

        metadata = self.session.get(
            url=self.config["endpoints"]["metadata"].format(title_id=self.title), params={"token": self.token}
        ).json()

        if metadata["programType"] == "movie":
            self.movie = True

        if self.movie:
            return Movies(
                [
                    Movie(
                        id_=metadata["id"],
                        service=self.__class__,
                        name=metadata["title"],
                        description=metadata["description"],
                        year=metadata["releaseYear"] if metadata["releaseYear"] > 0 else None,
                        language=Language.find(metadata["languages"][0]),
                        data=metadata,
                    )
                ]
            )
        else:
            episodes = []

            for season in metadata["seasons"]:
                if "Trailers" not in season["title"]:
                    season_data = self.session.get(url=season["url"], params={"token": self.token}).json()

                    for episode in season_data["entries"]:
                        episodes.append(
                            Episode(
                                id_=episode["id"],
                                service=self.__class__,
                                title=metadata["title"],
                                season=episode["season"],
                                number=episode["episode"],
                                name=episode["title"],
                                description=episode["description"],
                                year=metadata["releaseYear"] if metadata["releaseYear"] > 0 else None,
                                language=Language.find(metadata["languages"][0]),
                                data=episode,
                            )
                        )
            return Series(episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        # Handle HYBRID mode by fetching both HDR10 and DV tracks separately
        if self.range == "HYBRID" and self.cdm.security_level != 3:
            tracks = Tracks()

            # Get HDR10 tracks
            hdr10_tracks = self._get_tracks_for_range(title, "HDR10")
            tracks.add(hdr10_tracks, warn_only=True)

            # Get DV tracks
            dv_tracks = self._get_tracks_for_range(title, "DV")
            tracks.add(dv_tracks, warn_only=True)

            return tracks
        else:
            # Normal single-range behavior
            return self._get_tracks_for_range(title, self.range)

    def _get_tracks_for_range(self, title: Title_T, range_override: str = None) -> Tracks:
        # Use range_override if provided, otherwise use self.range
        current_range = range_override if range_override else self.range

        # Build API request parameters
        params = {
            "token": self.token,
            "guid": title.id,
        }

        data = {
            "type": self.config["client"][self.device]["type"],
        }

        # Add range-specific parameters
        if current_range == "HDR10":
            data["video_format"] = "hdr10"
        elif current_range == "DV":
            data["video_format"] = "dolby_vision"
        else:
            data["video_format"] = "sdr"

        # Only request high-quality HDR content with L1 CDM
        if current_range in ("HDR10", "DV") and self.cdm.security_level == 3:
            # L3 CDM - skip HDR content
            return Tracks()

        streams = self.session.post(
            url=self.config["endpoints"]["streams"],
            params=params,
            data=data,
        ).json()["media"]

        self.license = {
            "url": streams["drm"]["url"],
            "data": streams["drm"]["data"],
            "session": streams["drm"]["session"],
        }

        manifest_url = streams["url"].split("?")[0]

        self.log.debug(f"Manifest URL: {manifest_url}")
        tracks = DASH.from_url(url=manifest_url, session=self.session).to_tracks(language=title.language)

        # Set range attributes on video tracks
        for video in tracks.videos:
            if current_range == "HDR10":
                video.range = Video.Range.HDR10
            elif current_range == "DV":
                video.range = Video.Range.DV
            else:
                video.range = Video.Range.SDR

        # Remove DRM-free ("clear") audio tracks
        tracks.audio = [
            track for track in tracks.audio if "clear" not in track.data["dash"]["representation"].get("id")
        ]

        for track in tracks.audio:
            if track.channels == 6.0:
                track.channels = 5.1
            track_label = track.data["dash"]["adaptation_set"].get("label")
            if track_label and "Audio Description" in track_label:
                track.descriptive = True

        tracks.subtitles.clear()
        if streams.get("captions"):
            for subtitle in streams["captions"]:
                tracks.add(
                    Subtitle(
                        id_=hashlib.md5(subtitle["url"].encode()).hexdigest()[0:6],
                        url=subtitle["url"],
                        codec=Subtitle.Codec.from_mime("vtt"),
                        language=Language.get(subtitle["language"]),
                        # cc=True if '(cc)' in subtitle['name'] else False,
                        sdh=True,
                    )
                )

        if not self.movie:
            title.data["chapters"] = self.session.get(
                url=self.config["endpoints"]["metadata"].format(title_id=title.id), params={"token": self.token}
            ).json()["chapters"]

        return tracks

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        chapters = []

        if title.data.get("chapters", []):
            for chapter in title.data["chapters"]:
                if chapter["name"] == "Intro":
                    chapters.append(Chapter(timestamp=chapter["start"], name="Opening"))
                    chapters.append(Chapter(timestamp=chapter["end"]))
                if chapter["name"] == "Credits":
                    chapters.append(Chapter(timestamp=chapter["start"], name="Credits"))

        return chapters

    def get_widevine_service_certificate(self, **_: any) -> str:
        """Return the Widevine service certificate from config, if available."""
        return self.config.get("certificate")

    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        """Retrieve a PlayReady license for a given track."""

        license_url = self.config["endpoints"].get("playready_license")
        if not license_url:
            raise ValueError("PlayReady license endpoint not configured")

        response = self.session.post(
            url=license_url,
            data=challenge,
            headers={
                "user-agent": self.config["client"][self.device]["license_user_agent"],
            },
        )
        response.raise_for_status()
        return response.content

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        license_url = self.license.get("url") or self.config["endpoints"].get("widevine_license")
        if not license_url:
            raise ValueError("Widevine license endpoint not configured")

        response = self.session.post(
            url=license_url,
            data=challenge,
            params={
                "session": self.license.get("session"),
                "userId": self.user_id,
            },
            headers={
                "dt-custom-data": self.license.get("data"),
                "user-agent": self.config["client"][self.device]["license_user_agent"],
            },
        )
        response.raise_for_status()
        try:
            return response.json().get("license")
        except ValueError:
            return response.content
