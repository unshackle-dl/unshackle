import base64
from datetime import datetime
import json
from math import e

from pathlib import Path
import random
import sys
import time
import typing
from uuid import UUID
import click
import re
from typing import List, Literal, Optional, Set, Union, Tuple
from http.cookiejar import CookieJar
from itertools import zip_longest
from Crypto.Random import get_random_bytes

import jsonpickle
from pymp4.parser import Box
from pywidevine import PSSH, Cdm
import requests
from langcodes import Language

from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.drm.widevine import Widevine
from unshackle.core.service import Service
from unshackle.core.titles import Titles_T, Title_T
from unshackle.core.titles.episode import Episode, Series
from unshackle.core.titles.movie import Movie, Movies
from unshackle.core.titles.title import Title
from unshackle.core.tracks import Tracks, Chapters
from unshackle.core.tracks.audio import Audio
from unshackle.core.tracks.chapter import Chapter
from unshackle.core.tracks.subtitle import Subtitle
from unshackle.core.tracks.track import Track
from unshackle.core.tracks.video import Video
from unshackle.core.utils.collections import flatten, as_list

from unshackle.core.tracks.attachment import Attachment
from unshackle.core.drm.playready import PlayReady
from unshackle.core.titles.song import Song
from unshackle.utils.base62 import decode
from .MSL import MSL, KeyExchangeSchemes
from .MSL.schemes.UserAuthentication import UserAuthentication

class Netflix(Service):
    """
    Service for https://netflix.com
    Version: 1.0.0

    Authorization: Cookies
    Security: UHD@SL3000/L1 FHD@SL3000/L1
    """
    TITLE_RE = [
        r"^(?:https?://(?:www\.)?netflix\.com(?:/[a-z0-9]{2})?/(?:title/|watch/|.+jbv=))?(?P<id>\d+)",
        r"^https?://(?:www\.)?unogs\.com/title/(?P<id>\d+)",
    ]
    ALIASES= ("NF", "Netflix")
    NF_LANG_MAP = {
        "es": "es-419",
        "pt": "pt-PT",
    }

    @staticmethod
    @click.command(name="Netflix", short_help="https://netflix.com")
    @click.argument("title", type=str)
    @click.option("-drm", "--drm-system", type=click.Choice(["widevine", "playready"], case_sensitive=False),
                  default="widevine",
                  help="which drm system to use")
    @click.option("-p", "--profile", type=click.Choice(["MPL", "HPL", "QC", "MPL+HPL", "MPL+HPL+QC", "MPL+QC"], case_sensitive=False),
                  default=None,
                  help="H.264 profile to use. Default is best available.")
    @click.option("--meta-lang", type=str, help="Language to use for metadata")
    @click.option("-ht","--hydrate-track", is_flag=True, default=False, help="Hydrate missing audio and subtitle.")
    @click.option("-hb", "--high-bitrate", is_flag=True, default=False, help="Get more video bitrate")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Netflix(ctx, **kwargs)

    def __init__(self, ctx: click.Context, title: str, drm_system: Literal["widevine", "playready"], profile: str, meta_lang: str, hydrate_track: bool, high_bitrate: bool):
        super().__init__(ctx)
        # General
        self.title = title
        self.profile = profile
        self.meta_lang = meta_lang
        self.hydrate_track = hydrate_track
        self.drm_system = drm_system
        self.profiles: List[str] = []
        self.requested_profiles: List[str] = []
        self.high_bitrate = high_bitrate
        
        # MSL
        self.esn = self.cache.get("ESN")
        self.msl: Optional[MSL] = None
        self.userauthdata = None

        # Download options
        self.range = ctx.parent.params.get("range_") or [Video.Range.SDR]
        self.vcodec = ctx.parent.params.get("vcodec") or Video.Codec.AVC # Defaults to H264
        self.acodec : Audio.Codec = ctx.parent.params.get("acodec") or Audio.Codec.EC3
        self.quality: List[int] = ctx.parent.params.get("quality")
        self.audio_only = ctx.parent.params.get("audio_only")
        self.subs_only = ctx.parent.params.get("subs_only")
        self.chapters_only = ctx.parent.params.get("chapters_only")


    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        # Configure first before download
        self.log.debug("Authenticating Netflix service")
        auth = super().authenticate(cookies, credential)
        if not cookies:
            raise EnvironmentError("Service requires Cookies for Authentication.")
        self.configure()
        return auth

    def get_titles(self) -> Titles_T:
        metadata = self.get_metadata(self.title)
        # self.log.info(f"Metadata: {jsonpickle.encode(metadata, indent=2)}")
        if "video" not in metadata:
            self.log.error(f"Failed to get metadata: {metadata}")
            sys.exit(1)
        titles: Titles_T | None = None
        if metadata["video"]["type"] == "movie":
            movie = Movie(
                id_=self.title,
                name=metadata["video"]["title"],
                year=metadata["video"]["year"],
                service=self.__class__,
                data=metadata["video"],
                description=metadata["video"]["synopsis"]
            )
            titles = Movies([
                movie
            ])
        else:
            # self.log.info(f"Episodes: {jsonpickle.encode(episodes, indent=2)}")

            episode_list: List[Episode] = []
            for season in metadata["video"]["seasons"]:
                for episode in season["episodes"]:
                    episode_list.append(
                        Episode(
                            id_=self.title,
                            title=metadata["video"]["title"],
                            year=season["year"],
                            service=self.__class__,
                            season=season["seq"],
                            number=episode["seq"],
                            name=episode["title"],
                            data=episode,
                            description=episode["synopsis"],
                        )
                    )

            titles = Series(episode_list)



        return titles



    def get_tracks(self, title: Title_T) -> Tracks:
       
        tracks = Tracks()
        
        # If Video Codec is H.264 is selected but `self.profile is none` profile QC has to be requested seperately
        if self.vcodec == Video.Codec.AVC:
            # self.log.info(f"Profile: {self.profile}")
            try:
                manifest = self.get_manifest(title, self.profiles)
                movie_track = self.manifest_as_tracks(manifest, title, self.hydrate_track)
                tracks.add(movie_track)

                if self.profile is not None:
                    self.log.info(f"Requested profiles: {self.profile}")
                else:
                    qc_720_profile = [x for x in self.config["profiles"]["video"][self.vcodec.extension.upper()]["QC"] if "l40" not in x and 720 in self.quality]
                    qc_manifest = self.get_manifest(title, qc_720_profile if 720 in self.quality else self.config["profiles"]["video"][self.vcodec.extension.upper()]["QC"])
                    qc_tracks = self.manifest_as_tracks(qc_manifest, title, False)
                    tracks.add(qc_tracks.videos)

                    mpl_manifest = self.get_manifest(title, [x for x in self.config["profiles"]["video"][self.vcodec.extension.upper()]["MPL"] if "l40" not in x])
                    mpl_tracks = self.manifest_as_tracks(mpl_manifest, title, False)
                    tracks.add(mpl_tracks.videos)
            except Exception as e:
                self.log.error(e)
        else:
            if self.high_bitrate:
                splitted_profiles = self.split_profiles(self.profiles)
                for index, profile_list in enumerate(splitted_profiles):
                    try:
                        self.log.debug(f"Index: {index}. Getting profiles: {profile_list}")
                        manifest = self.get_manifest(title, profile_list)
                        manifest_tracks = self.manifest_as_tracks(manifest, title, self.hydrate_track if index == 0 else False)
                        tracks.add(manifest_tracks if index == 0 else manifest_tracks.videos)
                    except Exception:
                        self.log.error(f"Error getting profile: {profile_list}. Skipping")
                        continue
            else:
                try:
                    manifest = self.get_manifest(title, self.profiles)
                    manifest_tracks = self.manifest_as_tracks(manifest, title, self.hydrate_track)
                    tracks.add(manifest_tracks)
                except Exception as e:
                    self.log.error(e)


            
        # Add Attachments for profile picture
        if isinstance(title, Movie):
            tracks.add(
                Attachment.from_url(
                    url=title.data["boxart"][0]["url"]
                )
            )
        else:
            tracks.add(
                Attachment.from_url(title.data["stills"][0]["url"])
            )
        
        return tracks
        
    def split_profiles(self, profiles: List[str]) -> List[List[str]]:
        """
        Split profiles with names containing specific patterns based on video codec
        For H264: uses patterns "l30", "l31", "l40" (lowercase)
        For non-H264: uses patterns "L30", "L31", "L40", "L41", "L50", "L51" (uppercase)
        Returns List[List[str]] type with profiles grouped by pattern
        """
        # Define the profile patterns to match based on video codec
        if self.vcodec == Video.Codec.AVC:  # H264
            patterns = ["l30", "l31", "l40"]
        else:
            patterns = ["L30", "L31", "L40", "L41", "L50", "L51"]
        
        # Group profiles by pattern
        result: List[List[str]] = []
        for pattern in patterns:
            pattern_group = []
            for profile in profiles:
                if pattern in profile:
                    pattern_group.append(profile)
            if pattern_group:  # Only add non-empty groups
                result.append(pattern_group)
        
        return result
        
        
    def get_chapters(self, title: Title_T) -> Chapters:
        chapters: Chapters = Chapters()
        # self.log.info(f"Title data: {title.data}")
        credits = title.data["skipMarkers"]["credit"]
        if credits["start"] > 0 and credits["end"] > 0:
            chapters.add(Chapter(
                timestamp=credits["start"], # Milliseconds
                name="Intro"
            ))
            chapters.add(
                Chapter(
                    timestamp=credits["end"], # Milliseconds
                    name="Part 01"
                )
            )

        chapters.add(Chapter(
            timestamp=float(title.data["creditsOffset"]), # this is seconds, needed to assign to float
            name="Outro"
        ))

        return chapters

    def get_widevine_license(self, *, challenge: bytes, title: Movie | Episode | Song, track: AnyTrack) -> bytes | str | None:
        if not self.msl:
            self.log.error(f"MSL Client is not intialized!")
            sys.exit(1)
        application_data = {
                "version": 2,
                "url": track.data["license_url"],
                "id": int(time.time() * 10000),
                "esn": self.esn.data,
                "languages": ["en-US"],
                # "uiVersion": "shakti-v9dddfde5",
                "clientVersion": "6.0026.291.011",
                "params": [{
                    "sessionId": base64.b64encode(get_random_bytes(16)).decode("utf-8"),
                    "clientTime": int(time.time()),
                    "challengeBase64": base64.b64encode(challenge).decode("utf-8"),
                    "xid": str(int((int(time.time()) + 0.1612) * 1000)),
                }],
                "echo": "sessionId"
            }
        header, payload_data = self.msl.send_message(
            endpoint=self.config["endpoints"]["license"],
            params={
                "reqAttempt": 1,
                "reqName": "license",
            },
            application_data=application_data,
            userauthdata=self.userauthdata
        )
        if not payload_data:
            self.log.error(f" - Failed to get license: {header['message']} [{header['code']}]")
            sys.exit(1)
        if "error" in payload_data[0]:
            error = payload_data[0]["error"]
            error_display = error.get("display")
            error_detail = re.sub(r" \(E3-[^)]+\)", "", error.get("detail", ""))

            if error_display:
                self.log.critical(f" - {error_display}")
            if error_detail:
                self.log.critical(f" - {error_detail}")

            if not (error_display or error_detail):
                self.log.critical(f" - {error}")

            sys.exit(1)
        return payload_data[0]["licenseResponseBase64"]
    
    def get_playready_license(self, *, challenge: bytes, title: Movie | Episode | Song, track: AnyTrack) -> bytes | str | None:
        return None
        # return super().get_widevine_license(challenge=challenge, title=title, track=track)

    def configure(self):
        # self.log.info(ctx)
        # if profile is none from argument let's use them all profile in video codec scope
        # self.log.info(f"Requested profiles: {self.profile}")
        if self.profile is None:
            self.profiles = self.config["profiles"]["video"][self.vcodec.extension.upper()]


        if self.profile is not None:
            self.requested_profiles = self.profile.split('+')
            self.log.info(f"Requested profile: {self.requested_profiles}")
        else:
            # self.log.info(f"Video Range: {self.range}")
            self.requested_profiles = self.config["profiles"]["video"][self.vcodec.extension.upper()]
        # Make sure video codec is supported by Netflix
        if self.vcodec.extension.upper() not in self.config["profiles"]["video"]:
            raise ValueError(f"Video Codec {self.vcodec} is not supported by Netflix")

        if self.range[0].name not in list(self.config["profiles"]["video"][self.vcodec.extension.upper()].keys()) and self.vcodec != Video.Codec.AVC and self.vcodec != Video.Codec.VP9:
            self.log.error(f"Video range {self.range[0].name} is not supported by Video Codec: {self.vcodec}")
            sys.exit(1)

        if len(self.range) > 1:
            self.log.error(f"Multiple video range is not supported right now.")
            sys.exit(1)
        
        if self.vcodec == Video.Codec.AVC and self.range[0] != Video.Range.SDR:
            self.log.error(f"H.264 Video Codec only supports SDR")
            sys.exit(1)

        self.profiles = self.get_profiles()
        self.log.info("Intializing a MSL client")
        self.get_esn()
        scheme = KeyExchangeSchemes.AsymmetricWrapped
        self.log.info(f"Scheme: {scheme}")


        self.msl = MSL.handshake(
            scheme=scheme,
            session=self.session,
            endpoint=self.config["endpoints"]["manifest"],
            sender=self.esn.data,
            cache=self.cache.get("MSL"),
            # kenc=self.config["keys"]["kenc"],
            # khmac=self.config["keys"]["khmac"]
        )
        cookie = self.session.cookies.get_dict()
        self.userauthdata = UserAuthentication.NetflixIDCookies(
            netflixid=cookie["NetflixId"],
            securenetflixid=cookie["SecureNetflixId"]
        )


    def get_profiles(self):
        result_profiles = []

        if self.vcodec == Video.Codec.AVC:
            if self.requested_profiles is not None:
                for requested_profiles in self.requested_profiles:
                    result_profiles.extend(flatten(list(self.config["profiles"]["video"][self.vcodec.extension.upper()][requested_profiles])))
                return result_profiles
                
            result_profiles.extend(flatten(list(self.config["profiles"]["video"][self.vcodec.extension.upper()].values())))
            return result_profiles

        # Handle case for codec VP9
        if self.vcodec == Video.Codec.VP9 and self.range[0] != Video.Range.HDR10:
            result_profiles.extend(self.config["profiles"]["video"][self.vcodec.extension.upper()].values())
            return result_profiles
        for profiles in self.config["profiles"]["video"][self.vcodec.extension.upper()]:
            for range in self.range:
                if range in profiles:
                    result_profiles.extend(self.config["profiles"]["video"][self.vcodec.extension.upper()][range.name])
                    # sys.exit(1)
        self.log.debug(f"Result_profiles: {result_profiles}")
        return result_profiles
        
    def get_esn(self):
        ESN_GEN = "".join(random.choice("0123456789ABCDEF") for _ in range(30))
        esn_value = f"NFCDIE-03-{ESN_GEN}"
        path = Path(".esn")
        if path.exists():
            esn = open(path).read()
            self.esn.set(esn)
            return
        # Check if ESN is expired or doesn't exist
        if self.esn.data is None or self.esn.data == {} or (hasattr(self.esn, 'expired') and self.esn.expired):
            # Set new ESN with 6-hour expiration
            self.esn.set(esn_value, 1 * 60 * 60)  # 1 hours in seconds
            self.log.info(f"Generated new ESN with 1-hour expiration")
        else:
            self.log.info(f"Using cached ESN.")
        self.log.info(f"ESN: {self.esn.data}")


    def get_metadata(self, title_id: str):
        """
        Obtain Metadata information about a title by it's ID.
        :param title_id: Title's ID.
        :returns: Title Metadata.
        """

        try:
            metadata = self.session.get(
                self.config["endpoints"]["metadata"].format(build_id="release"),
                params={
                    "movieid": title_id,
                    "drmSystem": self.config["configuration"]["drm_system"],
                    "isWatchlistEnabled": False,
                    "isShortformEnabled": False,
                    "languages": self.meta_lang
                }
            ).json()
        except requests.HTTPError as e:
            if e.response.status_code == 500:
                self.log.warning(
                    " - Recieved a HTTP 500 error while getting metadata, deleting cached reactContext data"
                )
                # self.cache.
                # os.unlink(self.get_cache("web_data.json"))
                # return self.get_metadata(self, title_id)
            raise Exception(f"Error getting metadata: {e}")
        except json.JSONDecodeError:
            self.log.error(" - Failed to get metadata, title might not be available in your region.")
            sys.exit(1)
        else:
            if "status" in metadata and metadata["status"] == "error":
                self.log.error(
                    f" - Failed to get metadata, cookies might be expired. ({metadata['message']})"
                )
                sys.exit(1)
            return metadata

    def get_manifest(self, title: Title_T, video_profiles: List[str], required_text_track_id: Optional[str] = None, required_audio_track_id: Optional[str] = None):
        audio_profiles = self.config["profiles"]["audio"].values()
        video_profiles = sorted(set(flatten(as_list(
                video_profiles,
                audio_profiles,
                self.config["profiles"]["video"]["H264"]["BPL"] if self.vcodec == Video.Codec.AVC else [],
                self.config["profiles"]["subtitles"],
        ))))
        

            
        self.log.debug("Profiles:\n\t" + "\n\t".join(video_profiles))

        if not self.msl:
            raise Exception("MSL Client is not intialized.")

        params = {
            "reqAttempt": 1,
            "reqPriority": 10,
            "reqName": "manifest",
        }
        _, payload_chunks = self.msl.send_message(
            endpoint=self.config["endpoints"]["manifest"],
            params=params,
            application_data={
                "version": 2,
                "url": "manifest",
                "id": int(time.time()),
                "esn": self.esn.data,
                "languages": ["en-US"],
                "clientVersion": "6.0026.291.011",
                "params": {
                    "clientVersion": "6.0051.090.911",
                    "challenge": self.config["payload_challenge_pr"] if self.drm_system == 'playready' else self.config["payload_challenge"],
                    "challanges": {
                        "default": self.config["payload_challenge_pr"] if self.drm_system == 'playready' else self.config["payload_challenge"]
                    },
                    "contentPlaygraph": ["v2"],
                    "deviceSecurityLevel": "3000",
                    "drmVersion": 25,
                    "desiredVmaf": "plus_lts",
                    "desiredSegmentVmaf": "plus_lts",
                    "flavor": "STANDARD",  # ? PRE_FETCH, SUPPLEMENTAL
                    "drmType": self.drm_system,
                    "imageSubtitleHeight": 1080,
                    "isBranching": False,
                    "isNonMember": False,
                    "isUIAutoPlay": False,
                    "licenseType": "standard",
                    "liveAdsCapability": "replace",
                    "liveMetadataFormat": "INDEXED_SEGMENT_TEMPLATE",
                    "manifestVersion": "v2",
                    "osName": "windows",
                    "osVersion": "10.0",
                    "platform": "138.0.0.0",
                    "profilesGroups": [{
                        "name": "default",
                        "profiles": video_profiles
                    }],
                    "profiles": video_profiles,
                    "preferAssistiveAudio": False,
                    "requestSegmentVmaf": False,
                    "requiredAudioTrackId": required_audio_track_id, # This is for getting missing audio tracks (value get from `new_track_id``)
                    "requiredTextTrackId": required_text_track_id, # This is for getting missing subtitle. (value get from `new_track_id``)
                    "supportsAdBreakHydration": False,
                    "supportsNetflixMediaEvents": True,
                    "supportsPartialHydration": True, # This is important if you want get available all tracks. but you must fetch each missing url tracks with "requiredAudioTracksId" or "requiredTextTrackId"
                    "supportsPreReleasePin": True,
                    "supportsUnequalizedDownloadables": True,
                    "supportsWatermark": True,
                    "titleSpecificData": {
                        title.data.get("episodeId", title.data["id"]): {"unletterboxed": False}
                    },
                    "type": "standard",  # ? PREPARE
                    "uiPlatform": "SHAKTI",
                    "uiVersion": "shakti-v49577320",
                    "useBetterTextUrls": True,
                    "useHttpsStreams": True,
                    "usePsshBox": True,
                    "videoOutputInfo": [{
                        # todo ; make this return valid, but "secure" values, maybe it helps
                        "type": "DigitalVideoOutputDescriptor",
                        "outputType": "unknown",
                        "supportedHdcpVersions": self.config["configuration"]["supported_hdcp_versions"],
                        "isHdcpEngaged": self.config["configuration"]["is_hdcp_engaged"]
                    }],
                    "viewableId": title.data.get("episodeId", title.data["id"]),
                    "xid": str(int((int(time.time()) + 0.1612) * 1000)),
                    "showAllSubDubTracks": True,
                }
            },
            userauthdata=self.userauthdata
        )
        if "errorDetails" in payload_chunks:
            raise Exception(f"Manifest call failed: {payload_chunks['errorDetails']}")
        # with open(f"./manifest_{"+".join(video_profiles)}.json", mode='w') as r:
        #     r.write(jsonpickle.encode(payload_chunks, indent=4))
        return payload_chunks
        
    @staticmethod
    def get_original_language(manifest) -> Language:
        for language in manifest["audio_tracks"]:
            if language["languageDescription"].endswith(" [Original]"):
                return Language.get(language["language"])
        # e.g. get `en` from "A:1:1;2;en;0;|V:2:1;[...]"
        return Language.get(manifest["defaultTrackOrderList"][0]["mediaId"].split(";")[2])

    def get_widevine_service_certificate(self, *, challenge: bytes, title: Movie | Episode | Song, track: AnyTrack) -> bytes | str:
        return self.config["certificate"]

    def manifest_as_tracks(self, manifest, title: Title_T, hydrate_tracks = False) -> Tracks:
        
        tracks = Tracks()
        original_language = self.get_original_language(manifest)
        self.log.debug(f"Original language: {original_language}")
        license_url = manifest["links"]["license"]["href"]
        # self.log.info(f"Video: {jsonpickle.encode(manifest["video_tracks"], indent=2)}")
        # self.log.info()
        for video in reversed(manifest["video_tracks"][0]["streams"]):
            # self.log.info(video)
            id = video["downloadable_id"]
            # self.log.info(f"Adding video {video["res_w"]}x{video["res_h"]}, bitrate: {(float(video["framerate_value"]) / video["framerate_scale"]) if "framerate_value" in video else None} with profile {video["content_profile"]}. kid: {video["drmHeaderId"]}")
            tracks.add(
                Video(
                    id_=video["downloadable_id"],
                    url=video["urls"][0]["url"],
                    codec=Video.Codec.from_netflix_profile(video["content_profile"]),
                    bitrate=video["bitrate"] * 1000,
                    width=video["res_w"],
                    height=video["res_h"],
                    fps=(float(video["framerate_value"]) / video["framerate_scale"]) if "framerate_value" in video else None,
                    language=Language.get(original_language),
                    edition=video["content_profile"],
                    range_=self.parse_video_range_from_profile(video["content_profile"]),
                    drm=[Widevine(
                        pssh=PSSH(
                            # Box.parse(
                            #     Box.build(
                            #         dict(
                            #             type=b"pssh",
                            #             version=0,
                            #             flags=0,
                            #             system_ID=Cdm.uuid,
                            #             init_data=b"\x12\x10" + UUID(hex=video["drmHeaderId"]).bytes
                            #         )
                            #     )
                            # )
                            manifest["video_tracks"][0]["drmHeader"]["bytes"]
                        ),
                        kid=video["drmHeaderId"]
                    )],
                    data={
                        'license_url': license_url
                    }
                )
            )
        # Audio

        # store unavailable tracks for hydrating later
        unavailable_audio_tracks: List[Tuple[str, str]] = []
        for index, audio in enumerate(manifest["audio_tracks"]):
            if len(audio["streams"]) < 1:
                # This 
                # self.log.debug(f"Audio lang {audio["languageDescription"]} is available but no stream available.")
                unavailable_audio_tracks.append((audio["new_track_id"], audio["id"])) # Assign to `unavailable_subtitle` for request missing audio tracks later
                continue
            # self.log.debug(f"Adding audio lang: {audio["language"]} with profile: {audio["content_profile"]}")
            is_original_lang = audio["language"] == original_language.language
            # self.log.info(f"is audio {audio["languageDescription"]} original language: {is_original_lang}")
            for stream in audio["streams"]:
                tracks.add(
                    Audio(
                        id_=stream["downloadable_id"],
                        url=stream["urls"][0]["url"],
                        codec=Audio.Codec.from_netflix_profile(stream["content_profile"]),
                        language=Language.get(self.NF_LANG_MAP.get(audio["language"]) or audio["language"]),
                        is_original_lang=is_original_lang,
                        bitrate=stream["bitrate"] * 1000,
                        channels=stream["channels"],
                        descriptive=audio.get("rawTrackType", "").lower() == "assistive",
                        name="[Original]" if Language.get(audio["language"]).language == original_language.language else None,
                        joc=6 if "atmos" in stream["content_profile"] else None
                    )
                )

    


        # Subtitle
        unavailable_subtitle: List[Tuple[str, str]] = []
        for index, subtitle in enumerate(manifest["timedtexttracks"]):
            if "isNoneTrack" in subtitle and subtitle["isNoneTrack"] == True:
                continue
            if subtitle["hydrated"] == False:
                # This subtitles is there but has to request stream first
                unavailable_subtitle.append((subtitle["new_track_id"], subtitle["id"])) # Assign to `unavailable_subtitle` for request missing subtitles later
                # self.log.debug(f"Audio language: {subtitle["languageDescription"]} id: {subtitle["new_track_id"]} is not hydrated.")
                
                continue
            
            if subtitle["languageDescription"] == 'Off':
                # I don't why this subtitles is requested, i consider for skip these subtitles for now
                continue
                # pass

            id = list(subtitle["downloadableIds"].values())
            language = Language.get(subtitle["language"])
            profile = next(iter(subtitle["ttDownloadables"].keys()))
            tt_downloadables = next(iter(subtitle["ttDownloadables"].values()))
            is_original_lang = subtitle["language"] == original_language.language
            # self.log.info(f"is subtitle {subtitle["languageDescription"]} original language {is_original_lang}")   
            # self.log.info(f"ddd")
            tracks.add(
                Subtitle(
                    id_=id[0],
                    url=tt_downloadables["urls"][0]["url"],
                    codec=Subtitle.Codec.from_netflix_profile(profile),
                    language=language,
                    forced=subtitle["isForcedNarrative"],
                    cc=subtitle["rawTrackType"] == "closedcaptions",
                    sdh=subtitle["trackVariant"] == 'STRIPPED_SDH' if "trackVariant" in subtitle else False,
                    is_original_lang=is_original_lang,
                    name=("[Original]" if language.language == original_language.language else None or "[Dubbing]" if "trackVariant" in subtitle and subtitle["trackVariant"] == "DUBTITLE" else None),
                )
            )
        if hydrate_tracks == False:
            return tracks
        # Hydrate missing tracks
        self.log.info(f"Getting all missing audio and subtitle tracks")
        for audio_hydration, subtitle_hydration in zip_longest(unavailable_audio_tracks, unavailable_subtitle, fillvalue=("N/A", "N/A")):
            # self.log.info(f"Audio hydration: {audio_hydration}")
            manifest = self.get_manifest(title, self.profiles, subtitle_hydration[0], audio_hydration[0])
            
            audios = next(item for item in manifest["audio_tracks"] if 'id' in item and item["id"] == audio_hydration[1])
            subtitles = next(item for item in manifest["timedtexttracks"] if 'id' in item and item["id"] == subtitle_hydration[1])
            for stream in audios["streams"]:
                if audio_hydration[0] == 'N/A' and audio_hydration[1] == 'N/A':
                    # self.log.info(f"Skipping not available hydrated audio tracks")
                    continue
                tracks.add(
                    Audio(
                        id_=stream["downloadable_id"],
                        url=stream["urls"][0]["url"],
                        codec=Audio.Codec.from_netflix_profile(stream["content_profile"]),
                        language=Language.get(self.NF_LANG_MAP.get(audios["language"]) or audios["language"]),
                        is_original_lang=stream["language"] == original_language.language,
                        bitrate=stream["bitrate"] * 1000,
                        channels=stream["channels"],
                        descriptive=audios.get("rawTrackType", "").lower() == "assistive",
                        name="[Original]" if Language.get(audios["language"]).language == original_language.language else None,
                        joc=6 if "atmos" in stream["content_profile"] else None
                    )
                )
            
            # self.log.info(jsonpickle.encode(subtitles, indent=2))
            # sel
            
            if subtitle_hydration[0] == 'N/A':
                # self.log.info(f"Skipping not available hydrated subtitle tracks")
                continue
            id = list(subtitles["downloadableIds"].values())
            language = Language.get(subtitles["language"])
            profile = next(iter(subtitles["ttDownloadables"].keys()))
            tt_downloadables = next(iter(subtitles["ttDownloadables"].values()))
            tracks.add(
                Subtitle(
                    id_=id[0],
                    url=tt_downloadables["urls"][0]["url"],
                    codec=Subtitle.Codec.from_netflix_profile(profile),
                    language=language,
                    forced=subtitles["isForcedNarrative"],
                    cc=subtitles["rawTrackType"] == "closedcaptions",
                    sdh=subtitles["trackVariant"] == 'STRIPPED_SDH' if "trackVariant" in subtitles else False,
                    is_original_lang=subtitles["language"] == original_language.language,
                    name=("[Original]" if language.language == original_language.language else None or "[Dubbing]" if "trackVariant" in subtitle and subtitle["trackVariant"] == "DUBTITLE" else None),
                )
            )
                
        return tracks

    
    def parse_video_range_from_profile(self, profile: str) -> Video.Range:
        """
        Parse the video range from a Netflix profile string.
        
        Args:
            profile (str): The Netflix profile string (e.g., "hevc-main10-L30-dash-cenc")
            
        Returns:
            Video.Range: The corresponding Video.Range enum value
            
        Examples:
            >>> parse_video_range_from_profile("hevc-main10-L30-dash-cenc")
            <Video.Range.SDR: 'SDR'>
            >>> parse_video_range_from_profile("hevc-dv5-main10-L30-dash-cenc")
            <Video.Range.DV: 'DV'>
        """
        
        # Get video profiles from config
        video_profiles = self.config.get("profiles", {}).get("video", {})
        
        # Search through all codecs and ranges to find the profile
        for codec, ranges in video_profiles.items():
            # if codec == 'H264':
            #     return Video.Range.SDR # for H264 video always return SDR
            for range_name, profiles in ranges.items():
                # self.log.info(f"Checking range {range_name}")
                if profile in profiles:
                    # Return the corresponding Video.Range enum value
                    try:
                        # self.log.info(f"Found {range_name}")
                        return Video.Range(range_name)
                    except ValueError:
                        # If range_name is not a valid Video.Range, return SDR as default
                        self.log.debug(f"Video range is not valid {range_name}")
                        return Video.Range.SDR
        
        # If profile not found, return SDR as default
        return Video.Range.SDR