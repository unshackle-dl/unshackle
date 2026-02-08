from __future__ import annotations

import atexit
import click
import gzip
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.parse

from bs4 import BeautifulSoup
from click import Context
from Cryptodome.Hash import MD5
from langcodes import Language
from pathlib import Path
from requests import Request
from typing import Any, Optional, Union, List, Tuple
from urllib import parse
from urllib.request import url2pathname
from requests.adapters import BaseAdapter
from requests.models import Response

import m3u8
from pymediainfo import MediaInfo

from unshackle.core.binaries import FFMPEG
from unshackle.core.cdm.monalisa import MonaLisaCDM
from unshackle.core.config import config
from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.downloaders import requests
from unshackle.core.drm import MonaLisa
from unshackle.core.service import Service
from unshackle.core.titles import Title_T, Titles_T, Episode, Movie, Movies, Series
from unshackle.core.tracks import Chapters, Tracks, Video, Audio, Subtitle, Chapter
from unshackle.core.utilities import get_ip_info

class LocalFileAdapter(BaseAdapter):
    """Adapter to handle file:// URLs in requests."""

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        # Parse the file:// URL to extract the path component
        parsed = urllib.parse.urlparse(request.url)
        # url2pathname handles platform-specific path conversion
        # Windows: file:///C:/path -> C:\path
        # Linux: file:///path -> /path
        path = url2pathname(parsed.path)

        resp = Response()
        resp.status_code = 200
        resp.url = request.url

        try:
            file_path = Path(path)
            file_size = file_path.stat().st_size
            resp.raw = open(file_path, "rb")
            resp.headers["Content-Length"] = str(file_size)
        except Exception as e:
            resp.status_code = 404
            resp._content = str(e).encode()

        return resp

    def close(self):
        pass

class iQ(Service):
    """
    Service code for iQIYI Streaming Service (https://www.iq.com).

    Author: Service made by CodeName393 with Special Thanks to narakama and DRM module made by Hugov and Improvement by sp4rk.y\n
    Authorization: Cookies\n
    Security: UHD@ML, FHD@ML
    """

    ALIASES = ("iQ", "iQIYI", "IQ", "IQIYI")
    TITLE_RE = (
        r"^(?:https?://(?:www\.)?iq\.com/(?:play|album)/)?(?P<id>[^/?]+)(?:\?.*)?$",
    )

    # Path to MonaLisa CDM device file (relative to this service)
    CDM_PATH = Path(__file__).parent / "CDM" / "monalisa.mld"

    ORIG_LANG_MAP = {
        "Mandarin": "zh-Hans",
        "Cantonese": "zh-Hant",
        "English": "en",
        "Korean": "ko",
        "Japanese": "ja",
        "Thai": "th",
        "Vietnamese": "vi",
        "Indonesian": "id",
        "Malay": "ms",
        "Spanish": "es-419",
        "Portuguese": "pt-BR",
        "Arabic": "ar",
        "French": "fr",
        "German": "de"
    }

    LANG_MAP = {
        1: "zh-Hans", # 표준 중국어(중국어 간체)
        2: "zh-Hant", # 광동어(중국어 번체)
        3: "en",      # 영어
        5: "ko",      # 한국어
        143: "pt-BR", # 포르투갈어
        157: "th",    # 태국어
        161: "vi"     # 베트남어
    }

    SUB_LANG_MAP = {
        1: "zh-Hans", # 표준 중국어(중국어 간체)
        2: "zh-Hant", # 광동어(중국어 번체)
        3: "en",      # 영어
        4: "ko",      # 한국어
        5: "ja",      # 일본어
        6: "fr",      # 불어
        18: "th",     # 태국어
        21: "ms",     # 말레이어
        23: "vi",     # 베트남어
        24: "id",     # 인도네시아어
        26: "es-419", # 스페인어
        27: "pt-BR",  # 포르투갈어
        28: "ar",     # 아랍어
        30: "de"      # 독일어
    }

    BID_QUALITY = {
        4320: ["1020"],
        2160: ["860", "800"],
        1080: ["650", "600"],
        720: ["500"],
        480: ["300"],
        360: ["200"]
    }

    @staticmethod
    @click.command(name="iQIYI", short_help="https://www.iq.com", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> iQ:
        return iQ(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.title_id = self.title
        for pattern in self.TITLE_RE:
            match = re.match(pattern, self.title)
            if match:
                self.title_id = match.group("id")
                break

        self.session.mount("file://", LocalFileAdapter())
        self._temp_files: list[Path] = []
        atexit.register(self._cleanup_temp_files)
        self.decrypt_tool_path = MonaLisaCDM.get_worker_path()

        self.vcodec: Video.Codec = ctx.parent.params.get("vcodec") or Video.Codec.AVC
        self.acodec: Audio.Codec = ctx.parent.params.get("acodec") or Audio.Codec.AAC
        self.range: List[Video.Range] = ctx.parent.params.get("range_") or [Video.Range.SDR]
        self.quality: List[int] = ctx.parent.params.get("quality") or [1080]
        self.wanted = ctx.parent.params.get("wanted")
        self.video_only = ctx.parent.params.get("video_only")
        self.audio_only = ctx.parent.params.get("audio_only")
        self.subs_only = ctx.parent.params.get("subs_only")
        self.chapters_only = ctx.parent.params.get("chapters_only")
        self.list_ = ctx.parent.params.get("list_")
        self.skip_dl = ctx.parent.params.get("skip_dl")

        self.active_session = {}
        self.playback_data = {
            "has_external_audio": False,
            "svp": None
        }
        self._fps_cache = None

        self.log.info("Preparing...")

        if self.quality > [2160]:
            self.log.info(" + 8K video maybe banned from account.")
        elif self.quality > [1080]:
            self.vcodec = Video.Codec.HEVC
            self.log.info(f" + Switched video codec to H265.")
        else:
            self.vcodec = Video.Codec.AVC
            self.log.info(f" + Switched video codec to H264.")

        self.session.headers.update({
            "User-Agent": self.config["device"]["user_agent"],
            "Referer": "https://www.iq.com/",
            "Origin": "https://www.iq.com"
        })

        ip_info = get_ip_info(self.session)
        country_key = None
        possible_keys = ["countryCode", "country", "country_code", "country-code"]
        for key in possible_keys:
            if key in ip_info:
                country_key = key
                break
        if country_key:
            region = str(ip_info[country_key]).upper()
            self.log.info(f" + IP Region: {region}")
        else:
            self.log.warning(f" - The region could not be determined from IP information: {ip_info}")
            region = "US"
            self.log.info(f" + IP Region: {region} (By Default)")

    def authenticate(self, cookies: Optional[Any] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        self.cookies = cookies
        if not self.cookies:
            raise EnvironmentError("Service requires Cookies for Authentication.")
        
        self.log.info("Logging into iQIYI...")
        self._login()

    def _login(self) -> None:
        cookie_dict = {c.name: c.value for c in self.cookies} if self.cookies else {}
        self.active_session["uid"] = cookie_dict.get("pspStatusUid", "0") # User ID
        self.active_session["qc005"] = cookie_dict.get("QC005", "") # Device Id
        self.active_session["pck"] = cookie_dict.get("I00001", "") # Passport Cookie
        self.active_session["dfp"] = cookie_dict.get("__dfp", "").split("@")[0] # Device Fingerprint
        self.active_session["type_code"] = cookie_dict.get("QCVtype", "")
        self.active_session["mode_code"] = cookie_dict.get("mod", "")
        self.active_session["lang_code"] = cookie_dict.get("lang", "en_us")

        if not self.active_session["qc005"]:
            self.log.warning(" - Device Id not found. Playback might fail.")
            self.active_session["qc005"] = "0"
        
        if not self.active_session["pck"]:
            self.log.debug(" + Fetching PCK token...")
            self.active_session["pck"] = self._fetch_pck()

        if not self.active_session["type_code"]:
            self.log.warning(" - Type code not found. Playback might fail.")
            self.active_session["type_code"] = "0"
        
        if not self.active_session["mode_code"]:
            self.active_session["mode_code"] = self._get_mode_code()

        self.active_session["ptid"] = self._get_ptid()

        data = self._get_vip_info()
        is_vip = False
        if data["code"] == "0":
            if "userinfo" not in data["data"]:
                self.log.error(f" - {data['data']['msg']}")
                sys.exit(1)
            elif "vip_list" in data["data"]:
                is_vip = True
        else:
            self.log.error(" - Could not resolve cookie session.")
            sys.exit(1)

        if not is_vip:
            self.log.warning(" - Account is not subscribed to iQiyi. Playback might fail.", exc_info=False)

        self.log.info(f" + Account ID: {self.active_session['uid']}")
        self.log.info(f" + Type Code: {self.active_session['type_code']}")
        self.log.info(f" + Mode Code: {self.active_session['mode_code']}")
        self.log.info(f" + Subscribed: {is_vip}")
        self.log.debug(f" + Platform Type ID: {self.active_session['ptid']}")

    def get_titles(self) -> Titles_T:
        video_info, lang_info = self._get_album_info(self.title_id, self.active_session["lang_code"])
        tvid = video_info.get("tvId") or video_info.get("defaultTvId")
        albumid = video_info["albumId"]
        qipuid = video_info["qipuId"]
        name = video_info["name"]
        year = video_info.get("year") or video_info.get("publishTime", "")[:4]

        video_info_en, _ = self._get_album_info(self.title_id, "en_us")
        orig_lang = video_info_en.get("categoryTagMap", {}).get("Language", [{}])[0].get("name", "Mandarin") # album orig Lang

        if not tvid and (albumid == qipuid): # Movie
            content_type = "movie"
        else:
            content_type = "series"
        self.log.debug(f" + Content Type: {content_type.upper()}")
    
        if content_type == "movie":
            return Movies([
                Movie(
                    id_=hashlib.md5(str(qipuid).encode()).hexdigest()[0:6],
                    service=self.__class__,
                    name=name,
                    year=year,
                    data={"tvid": qipuid, "orig_lang": orig_lang}
                )
            ])
        
        elif content_type == "series":
            return Series(self._get_series(albumid, name, year, orig_lang, lang_info["episode"]))
        
    def _get_series(self, albumid: str, name: str, year: str, lang: str, episode_lang: str) -> Series:
        episodes: List[Episode] = []
        raw_episodes = []
        block_size = 50

        first_batch = self._get_episode_list(albumid, 1, block_size)
        total_count = first_batch["data"]["total"]
        
        if scripts := first_batch["data"]["epg"]:
            raw_episodes.extend(scripts)

        if total_count > block_size:
            for start_order in range(block_size + 1, total_count + 1, block_size):
                end_order = min(start_order + block_size - 1, total_count)
                
                batch_data = self._get_episode_list(albumid, start_order, end_order)
                if scripts := batch_data["data"]["epg"]:
                    raw_episodes.extend(scripts)

        for ep in raw_episodes:
            episodes.append(
                Episode(
                    id_=hashlib.md5(str(ep["qipuId"]).encode()).hexdigest()[0:6],
                    service=self.__class__,
                    title=name,
                    season=1 if ep["contentType"] == 1 else 0,
                    number=ep["order"],
                    name=episode_lang.format(ep["order"]) if ep["contentType"] == 1 else ep["extraName"],
                    year=year,
                    data={"tvid": ep["qipuId"], "orig_lang": lang}
                )
            )
        
        return episodes
        
    def get_tracks(self, title: Title_T) -> Tracks:
        tvid = title.data["tvid"]
        self.log.debug(f" + TVID: {tvid}")
        videos, audios, subs = self._collect_media_sources(tvid)

        if not videos and not (self.subs_only or self.chapters_only):
            self.log.error("No playable streams found.")
            sys.exit(1)

        # Set Original Lang
        orig_lang_name = title.data.get("orig_lang")
        orig_lang_code = self.ORIG_LANG_MAP.get(orig_lang_name)
        title.language = Language.get(orig_lang_code if orig_lang_code else "und")
        self.log.debug(f" + Original Language : {title.language}")

        unique_video_lids = set(v.get("lid") for v in videos if "lid" in v)
        unique_audio_lids = set(a.get("lid") for a in audios if "lid" in a)

        force_video_lang = orig_lang_code if (len(unique_video_lids) <= 1 and orig_lang_code) else None
        force_audio_lang = orig_lang_code if (len(unique_audio_lids) <= 1 and orig_lang_code) else None

        tracks = Tracks()

        for video in videos:
            try:
                track = self._parse_video_track(video, title, force_video_lang)
                if track:
                    tracks.add(track, warn_only=True)
            except Exception as e:
                self.log.info(f"Failed to parse video track: {e}")

        for aud_data in audios:
            try:
                track = self._parse_audio_track(aud_data, title, force_audio_lang)
                if track:
                    tracks.add(track, warn_only=True)
                    self.playback_data["has_external_audio"] = True
            except Exception as e:
                self.log.info(f"Failed to parse audio track: {e}")

        for sub_data in subs:
            try:
                track = self._parse_subtitle_track(sub_data, title)
                if track:
                    tracks.add(track, warn_only=True)
            except Exception as e:
                self.log.info(f"Failed to parse subtitle track: {e}")

        return tracks

    def _collect_media_sources(self, tvid: str) -> Tuple[List[dict], List[dict], List[dict]]:
        all_videos = []
        all_audios = []
        all_subs = []

        # Request video
        if not any([self.audio_only, self.subs_only, self.chapters_only]):
            if self.vcodec == Video.Codec.HEVC:
                target_codec_keys = {"h265", "h265_edr"}
            else:
                target_codec_keys = {"h264"}

            if Video.Range.HYBRID in self.range[0]:
                target_codec_keys.update(["dv_edr", "hdr_edr", "dv", "hdr"])
            elif Video.Range.DV in self.range[0]:
                target_codec_keys.update(["dv_edr", "dv"])
            elif Video.Range.HDR10 in self.range[0] or Video.Range.HDR10P in self.range[0]:
                target_codec_keys.update(["hdr_edr", "hdr"])

            target_bids = []
            available_qualities = sorted(self.BID_QUALITY.keys())

            for q in self.quality:
                if q in self.BID_QUALITY:
                    target_bids.extend(self.BID_QUALITY[q])
                else:
                    closest_q = min(available_qualities, key=lambda x: abs(x - q))
                    target_bids.extend(self.BID_QUALITY[closest_q])
                if q > 2160:
                    target_codec_keys.add("8k")

            for c_key in target_codec_keys:
                for bid in target_bids:
                    try:
                        v_list, a_list, s_list = self._get_media_data(tvid, c_key, bid)
                        if v_list:
                            all_videos.extend(v_list)
                        if a_list:
                            all_audios.extend(a_list)
                        if s_list:
                            all_subs.extend(s_list)
                    except Exception:
                        continue

        # Request audio
        if not any([self.video_only, self.subs_only, self.chapters_only]):
            found_lids = set()
            for aud in all_audios:
                if "lid" in aud:
                    found_lids.add(str(aud["lid"]))

            # If audio_only, skip the video loop above to get id with default call because there is no id
            if self.audio_only and not found_lids:
                try:
                    _, a_list, _ = self._get_media_data(tvid, "h264", "600")
                    for aud in a_list:
                        if "lid" in aud:
                            found_lids.add(str(aud["lid"]))
                    all_audios.extend(a_list)
                except Exception:
                    pass

            additional_audio_types = ["dolby", "aac"]

            for lid in found_lids:
                for audio_type in additional_audio_types:
                    try:
                        _, a_list, _ = self._get_media_data(tvid, "h265_edr", "800", audio_type, lid) # edr include dolby surround audio
                        if a_list:
                            all_audios.extend(a_list)
                    except Exception as e:
                        continue

        # Use default data if any no data
        if not (all_videos or all_audios or all_subs):
            try:
                v_list, a_list, s_list = self._get_media_data(tvid, "h264", "600")
                if v_list:
                    all_videos.extend(v_list)
                if a_list:
                    all_audios.extend(a_list)
                if s_list:
                    all_subs.extend(s_list)
            except Exception:
                pass

        # Remove duplicate tracks
        video_map = {}
        audio_map = {}
        sub_map = {}

        for v in all_videos:
            if not (v.get("url") or v.get("fs") or v.get("m3u8")):
                continue
            v_key = (v.get("bid"), v.get("lid"), v.get("scrsz", "0x0"), v.get("vsize", 0), v.get("code"), v.get("dr"))
            if v_key not in video_map:
                v["unique_key"] = v_key
                video_map[v_key] = v

        for a in all_audios:
            if not (a.get("m3u8Url") or a.get("url") or a.get("mpdUrl") or a.get("fs")):
                continue
            a_key = (a.get("bid"), a.get("lid"), a.get("cf", "aac"), a.get("ct", 1))
            if a_key not in audio_map:
                a["unique_key"] = a_key
                audio_map[a_key] = a

        for s in all_subs:
            if not (s.get("webvtt") or s.get("xml") or s.get("srt")):
                continue
            s_key = s.get("lid")
            if s_key not in sub_map:
                sub_map[s_key] = s

        return list(video_map.values()), list(audio_map.values()), list(sub_map.values())
    
    # AstroS Requested Features
    def _get_real_fps(self, m3u8_content_or_url: str) -> float:
        if self._fps_cache:
            return self._fps_cache

        try:
            if m3u8_content_or_url.startswith("http"):
                playlist = m3u8.load(m3u8_content_or_url)
                base_uri = playlist.base_uri
            else:
                playlist = m3u8.loads(m3u8_content_or_url)
                base_uri = None

            if not playlist.segments:
                return 0

            first_segment_url = playlist.segments[0].uri
            if not first_segment_url.startswith("http") and base_uri:
                first_segment_url = urllib.parse.urljoin(base_uri, first_segment_url)

            temp_seg_path = config.directories.temp / f"temp_fps_{int(time.time())}.ts"
            res = self.session.get(first_segment_url, stream=True)
            with open(temp_seg_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=8192):
                    f.write(chunk)

            media_info = MediaInfo.parse(temp_seg_path)
            fps = 0
            for track in media_info.tracks:
                if track.track_type == "Video":
                    fps = float(track.frame_rate) if track.frame_rate else 0
                    break

            if temp_seg_path.exists():
                temp_seg_path.unlink()
            
            self._fps_cache = fps
            return fps
        except Exception as e:
            self.log.warning(f"Failed to analyze real FPS: {e}")
            return 0

    def _parse_video_track(self, video: dict, title: Title_T, forced_lang: str = None) -> Optional[Video]:
        m3u8_url = video.get("url") or video.get("fs") or video.get("m3u8")
        if not m3u8_url:
            return None

        scrsz = video.get("scrsz", "0x0")
        width, height = map(int, scrsz.split("x")) if "x" in scrsz else (0, 0)
        duration = video.get("duration", 1)
        vsize = video.get("vsize", 0)
        bitrate = int((vsize / duration * 8) / 1000 * 1024) if duration else 0
        fps = self._get_real_fps(m3u8_url)
        if fps == 0:
            fps = float(video.get("fr", 0))
        
        codec_code = video.get("code")
        actual_codec = Video.Codec.AVC if codec_code == 2 else Video.Codec.HEVC

        dr = video.get("dr") # Dynamic Range
        if dr == 1:
            range_type = Video.Range.DV
        elif dr == 2:
            range_type = Video.Range.HDR10 # HDR Vivid
        else:
            range_type = Video.Range.SDR

        if forced_lang:
            lang_code = forced_lang
        else:
            lang_code = self.LANG_MAP.get(video.get("lid"), "und")
        if video.get("duration"):
            title.data["duration"] = video.get("duration")

        video_info = "_".join(map(str, video["unique_key"]))
        video_id = f'Video_{hashlib.md5(video_info.encode()).hexdigest()[0:6]}'

        # If m3u8 is raw content (not HTTP URL), save to local file
        from_file = None
        track_url = m3u8_url
        if not m3u8_url.strip().startswith("http"):
            from_file = self._save_temp_m3u8(m3u8_url, video_id)
            if not from_file:
                return None
            track_url = from_file.as_uri()

        track = Video(
            id_=video_id,
            url=track_url,
            codec=actual_codec,
            bitrate=bitrate,
            fps=fps,
            width=width,
            height=height,
            language=Language.get(lang_code),
            descriptor=Video.Descriptor.HLS,
            range_=range_type,
            from_file=from_file,
        )

        if drm := video.get("drm"):
            self._handle_monalisa_drm(track, drm.get("ticket")) # DRM PSSH
            track.downloader=requests # DRM requires segment decryption

        return track

    def _parse_audio_track(self, audio: dict, title: Title_T, forced_lang: str = None) -> Optional[Audio]:
        m3u8_url = audio.get("m3u8Url") or audio.get("url") or audio.get("mpdUrl")
        fs = audio.get("fs")
        if not m3u8_url and not fs:
            return None

        if forced_lang:
            lang_code = forced_lang
            lang_name = title.data["orig_lang"]
        else:
            lang_code = self.LANG_MAP.get(audio.get("lid"), "und")
            lang_name = audio.get("name", "Unknown")

        cf = audio.get("cf", "aac") # Codec Format
        ct = audio.get("ct", 1) # Codec Type

        if cf == "dolby":
            acodec = Audio.Codec.EC3
            if ct == 1:
                channels, bitrate = 2.0, 96_000
            elif ct == 2:
                channels, bitrate = 5.1, 256_000
            elif ct == 4:
                channels, bitrate = 16, 448_000
            else:
                self.log.debug(f" - Unknown audio codec type.{cf} - {ct}")
                channels, bitrate = 2.0, 0
        elif cf == "aac":
            acodec = Audio.Codec.AAC
            if ct == 1:
                channels, bitrate = 2.0, 128_000
            elif ct == 6:
                channels, bitrate = 2.0, 192_000
            else:
                self.log.debug(f" - Unknown audio codec type.{cf} - {ct}")
                channels, bitrate = 2.0, 0
        else:
            self.log.debug(f" - Unknown audio codec.{cf} - {ct}")
            acodec, channels, bitrate = None, 2.0, 0

        display_name = lang_name
        is_original = Language.get(lang_code) == title.language
        if is_original:
            display_name += " [Original]"

        audio_info = "_".join(map(str, audio["unique_key"]))
        audio_id = f'Audio_{hashlib.md5(audio_info.encode()).hexdigest()[0:6]}'

        # Determine m3u8 source: stitch segments or use provided URL
        from_file = None
        track_url = m3u8_url

        if not m3u8_url and fs: # Stitch from flag segments
            from_file = self._stitch_audio_segments(audio, audio_id)
            if not from_file:
                return None
            track_url = from_file.as_uri()
        elif m3u8_url:
            if not isinstance(m3u8_url, str):
                return None
            if not m3u8_url.strip().startswith("http"):
                from_file = self._save_temp_m3u8(m3u8_url, audio_id)
                if not from_file:
                    return None
                track_url = from_file.as_uri()
        else:
            return None

        track = Audio(
            id_=audio_id,
            url=track_url,
            codec=acodec,
            bitrate=bitrate,
            channels=channels,
            language=Language.get(lang_code),
            is_original_lang=is_original,
            descriptor=Audio.Descriptor.HLS,
            name=display_name,
            downloader=requests, # Audio needs segment processing due to gzip processing
            from_file=from_file,
        )

        # Atmos(16ch) -> 5.1 JOC 16
        if track.channels == 16:
            track.channels = 5.1
            track.joc = 16
        
        if drm := audio.get("drm"):
            self._handle_monalisa_drm(track, drm.get("ticket"))

        return track

    def _stitch_audio_segments(self, audio: dict, audio_id: str) -> Optional[Path]:
        try:
            segments = audio["fs"]
            m3u8_lines = [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                "#EXT-X-TARGETDURATION:6"
            ]
            
            resolved_count = 0
            for seg in segments:
                api_path = seg.get("l")
                if not api_path:
                    continue
                
                json_data = self._get_audio_segment_info(api_path)
                
                real_media_url = json_data.get("l") if isinstance(json_data, dict) else None
                
                if real_media_url:
                    m3u8_lines.append("#EXTINF:10.0,")
                    m3u8_lines.append(real_media_url)
                    resolved_count += 1

            if resolved_count > 0:
                m3u8_lines.append("#EXT-X-ENDLIST")
                raw_m3u8 = "\n".join(m3u8_lines)
                return self._save_temp_m3u8(raw_m3u8, audio_id)
                
        except Exception as e:
            self.log.error(f"Failed to stitch audio segments: {e}")
        
        return None

    def _parse_subtitle_track(self, subtitle: dict, title: Title_T) -> Optional[Subtitle]:
        path = subtitle.get("webvtt")
        codec = Subtitle.Codec.WebVTT
        if not path:
            path = subtitle.get("xml")
            codec = Subtitle.Codec.TimedTextMarkupLang
        if not path:
            path = subtitle.get("srt")
            codec = Subtitle.Codec.SubRip
        if not path:
            return None

        lang_code = self.SUB_LANG_MAP.get(subtitle.get("lid"), "und") # Lang ID
        lang_obj = Language.get(lang_code)
        is_original = lang_obj == title.language

        base_name = subtitle.get("_name", "Unknown")
        name_parts = [base_name]
        is_ai = int(subtitle.get("ss", 0)) == 1
        if is_ai:
            name_parts.append("(AI)")
        if is_original:
            name_parts.append("[Original]")
        
        return Subtitle(
            id_=lang_code,
            url=self.config["endpoint"]["subtitle"].format(path=path),
            codec=codec,
            language=lang_obj,
            is_original_lang=is_original,
            name=" ".join(name_parts)
        )

    def get_chapters(self, title: Title_T) -> Chapters:
        # self.log.info(title.data["duration"])
        # self.log.info(self.playback_data.get("svp"))
        svp_data = self.playback_data["svp"]
        if not svp_data:
            return Chapters()

        valid_segments = []
        for item in svp_data:
            for segment in item.get("vl", []):
                start = float(segment.get("sp", 0))
                end = float(segment.get("ep", 0))
                
                if start <= 0 and end <= 0:
                    continue
                
                valid_segments.append((start, end))

        duration = float(title.data["duration"])
        pre_chapter = [] 

        if valid_segments and duration > 0:
            min_start = min(s for s, e in valid_segments)
            max_end = max(e for s, e in valid_segments)

            if min_start > 0:
                pre_chapter.append(("Intro", 0.0))
                pre_chapter.append(("Scene", min_start))
            else:
                pre_chapter.append(("Scene", 0.0))

            if max_end < (duration - 1.0):
                pre_chapter.append(("Credits", max_end))

        else:
            pre_chapter.append(("Scene", 0.0))
            if valid_segments:
                for start, end in valid_segments:
                    if start < 600:
                        pre_chapter.append(("Intro", 0.0))
                        pre_chapter.append(("Scene", start))
                        break

        pre_chapter.sort(key=lambda x: x[1])

        unique_chapters_data = []
        if pre_chapter:
            curr_time = -1.0
            last_name = None

            for name, timestamp in pre_chapter:
                if abs(timestamp - curr_time) <= 1.0:
                    continue
                
                if name == last_name:
                    continue
                
                unique_chapters_data.append((name, timestamp))
                curr_time = timestamp
                last_name = name

        if not unique_chapters_data:
            unique_chapters_data.insert(0, ("Scene", 0.0))
        elif unique_chapters_data[0][1] > 1.0:
            if unique_chapters_data[0][0] == "Scene":
                unique_chapters_data.insert(0, ("Intro", 0.0))
            else:
                unique_chapters_data.insert(0, ("Scene", 0.0))

        chapters = Chapters()
        for name, timestamp in unique_chapters_data:
            c_name = name if name != "Scene" else None
            chapters.add(
                Chapter(
                    timestamp=timestamp,
                    name=c_name
                )
            )

        return chapters

    def get_widevine_service_certificate(self, **kwargs) -> Union[bytes, str]:
        return None

    def get_widevine_license(self, **kwargs) -> Optional[Union[bytes, str]]:
        return None

    def get_playready_license(self, **kwargs) -> Optional[bytes]:
        return None

    def _handle_monalisa_drm(self, track: AnyTrack, ticket: str) -> None:
        if not ticket:
            return

        if not self.decrypt_tool_path or not self.decrypt_tool_path.exists():
            self.log.error("ML-Worker not found. Place it in unshackle/binaries/")
            sys.exit(1)

        if not self.CDM_PATH.exists():
            self.log.error(f"MonaLisa CDM not found at: {self.CDM_PATH}")
            sys.exit(1)

        try:
            drm = MonaLisa(
                ticket=ticket,
                aes_key=self.config["key"]["ml"],
                device_path=self.CDM_PATH,
            )
            # Store DRM on track like Widevine/PlayReady
            track.drm = [drm]

        except Exception as e:
            self.log.error(f"MonaLisa Key challenge failed: {e}")

    def on_segment_downloaded(self, track: AnyTrack, segment: Path) -> None:
        # Video Segment - decrypt MonaLisa DRM
        if isinstance(track, Video):
            if hasattr(track, "drm") and track.drm:
                for drm in track.drm:
                    if isinstance(drm, MonaLisa):
                        try:
                            drm.decrypt_segment(segment)
                        except MonaLisa.Exceptions.WorkerNotFound:
                            self.log.error("ML-Worker not found. Place it in unshackle/binaries/")
                        except MonaLisa.Exceptions.DecryptionFailed as e:
                            self.log.error(str(e))
                        except Exception as e:
                            self.log.error(f"Failed to decrypt segment {segment.name}: {e}")
                        break

        # Audio Segment - decompress gzip if needed
        if isinstance(track, Audio):
            try:
                if segment.exists():
                    with open(segment, "rb") as f:
                        data = f.read()
                    if data.startswith(b"\x1f\x8b"): # gzip header
                        decompressed_data = gzip.decompress(data)
                        with open(segment, "wb") as f_out:
                            f_out.write(decompressed_data)
            except Exception as e:
                self.log.warning(f"Failed to decompress gzip segment {segment.name}: {e}")

    def on_track_downloaded(self, track: AnyTrack) -> None:
        # Use FFmpeg to remux Video MPEG-TS to MKV
        if isinstance(track, Video):
            if track.path is None: # unshackle bug...
                return
            if track.path.suffix.lower() == ".mkv":
                return

            mkv_path = track.path.with_suffix(".mkv")
            cmd = [str(FFMPEG), "-y", "-i", str(track.path)]
            if self.playback_data["has_external_audio"]:
                # External audio exists -> Remove audio from TS, keep video only
                cmd.extend(["-c:v", "copy", "-an"])
            else:
                # No external audio -> Keep everything (Audio + Video) from TS
                cmd.extend(["-c", "copy"])
            cmd.append(str(mkv_path))

            try:
                process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
                if process.returncode == 0 and mkv_path.exists():
                    if track.path.exists():
                        track.path.unlink()
                    track.path = mkv_path
                else:
                    self.log.warning(f"FFmpeg failed: {process.stderr or 'unknown error'}")
                    if mkv_path.exists():
                        mkv_path.unlink()
            except FileNotFoundError:
                self.log.warning("FFmpeg not found, skipping remux")
            except Exception as e:
                self.log.error(f"Error processing video track: {e}")
                if mkv_path.exists():
                    try:
                        mkv_path.unlink()
                    except Exception:
                        pass

    def _save_temp_m3u8(self, content: str, prefix: str) -> Optional[Path]:
        """Save m3u8 content to a temp file and return as file:// URL."""
        temp_dir = config.directories.temp
        temp_dir.mkdir(parents=True, exist_ok=True)
        safe_prefix = re.sub(r'[\\/*?:"<>|]', "", str(prefix))
        temp_path = temp_dir / f"{safe_prefix}_{int(time.time())}.m3u8"

        try:
            temp_path = temp_path.resolve()

            if self.list_ or self.chapters_only:
                return temp_path

            filtered_lines = [
                line for line in content.splitlines() 
                if "#EM" not in line and line.strip() != "#EXT-X-DISCONTINUITY"
            ]
            content_to_save = "\n".join(filtered_lines)
            
            temp_path.write_text(content_to_save, encoding="utf-8")
            self._temp_files.append(temp_path)
            
            # Return as file:// URL for compatibility with downloaders
            return temp_path
        except Exception as e:
            self.log.warning(f"Failed to save temp m3u8: {e}")
            return None

    def _cleanup_temp_files(self) -> None:
        """Clean up all tracked temp files."""
        for path in self._temp_files:
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass

    def _get_album_info(self, title_id: str, lang_code: str) -> Tuple[dict, dict]:
        endpoint = self.config["endpoint"]["album"].format(id=title_id, lang_code=lang_code)
        cookies_dict = {c.name: c.value for c in self.cookies}
        if "lang" not in cookies_dict or cookies_dict["lang"] != lang_code:
            cookies_dict["lang"] = lang_code
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
        if "lang" not in cookies_dict:
            cookie_header += "; lang=en_us"
        headers = {
            "User-Agent": self.config["device"]["user_agent_bws"],
            "Cookie": cookie_header
        }
        try:
            res = self.session.get(endpoint, headers=headers)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "html.parser")
            script_tag = soup.find("script", id="__NEXT_DATA__", type="application/json")
            if not script_tag:
                self.log.error(" - Failed to parse page data.")
                sys.exit(1)

            data = json.loads(script_tag.string)
            initial_state = data["props"]["initialState"]
            play_info = initial_state.get("play", {}).get("videoInfo", {})
            album_info = initial_state.get("album", {}).get("videoAlbumInfo", {})
            language_info = initial_state.get("language", {}).get("langPkg", {})
            if play_info and (play_info.get("qipuId")) and language_info:
                return play_info, language_info
            elif album_info and language_info:
                return album_info, language_info
            else:
                raise ValueError("No content information found.")
        except Exception as e:
            self.log.error(f"Content info not found: {e}", exc_info=False)
            sys.exit(1)
    
    def _get_episode_list(self, episode_list_id: str, start_order: int, end_order: int) -> dict:
        headers = {
            "User-Agent": self.config["device"]["user_agent_bws"],
            "Cookie": "; ".join([f"{cookie.name}={cookie.value}" for cookie in self.cookies])
        }
        params = {
            "platformId": "3",
            "modeCode": self.active_session["mode_code"],
            "langCode": self.active_session["lang_code"],
            "deviceId": self.active_session["qc005"],
            "startOrder": str(start_order),
            "endOrder": str(end_order),
            "isVip": "true"
        }
        try:
            url = self.config["endpoint"]["episode"].format(list_id=episode_list_id)
            data = self._request("GET", url, params=params, headers=headers)
            return data
        except Exception:
            return {}

    def _get_dash_stream(self, data: dict) -> dict:
        query = parse.urlencode(data)
        path = f"/dash?{query}"
        vf = MD5.new((path + self.config["key"]["dash"]).encode()).hexdigest()
        url = self.config["endpoint"]["stream"].format(path=path, vf=vf)
        headers = {
            "accept": "*/*",
            "qyid": self.active_session["qc005"],
            "bop": f'{{"b_ft1":"3","dfp":"{str(self.active_session["dfp"])}","version":"9.0"}}',
            "pck": self.active_session["pck"],
            "User-Agent": self.config["device"]["user_agent"]
        }
        data = self._request("GET", url, headers=headers)
        return data

    def _get_media_data(
        self, tvid: str, codec_key: str, bid: str, audio_codec_key: str = "dolby", lid: str = "1"
    ) -> Tuple[List[dict], List[dict], List[dict]]:
        video_config = self.config["quality"]["video"]
        audio_config = self.config["quality"]["audio"]

        video_params = video_config.get(codec_key, video_config["h264"]).copy()
        audio_params = audio_config.get(audio_codec_key, audio_config["dolby"]).copy()

        data = {
            "tvid": tvid,
            "uid": self.active_session["uid"], # User ID
            "k_uid": self.active_session["qc005"], # Device ID
            "tm": str(int(time.time() * 1000)), # Timestemp
            "bid": str(bid), # Resolution
            "ut": self.active_session["type_code"], # User Type
            "src": self.active_session["ptid"], # Platform Typr ID
            "ps": "1", # ?
            "d": "0",
            "pm": "0",
            "fr": "25", # Frame Rate
            "pt": "0", # ?
            "s": "0",
            "rs": "1",
            "sr": "1",
            "sver": "2",
            "k_ver": "7.12.0", # Client Version
            "k_tag": "1",
            "atype": "0",
            "vid": "", # Request Video ID(?)
            "lid": lid, # Request Lang(Audio)
            "dcdv": "3", # DRM Type (3 = Monalisa, 5 = ?, 7 = Widevine, 8 = ?, 9 = ?)
            "ccsn": self.config["key"]["ccsn"],
            "agent_type": "366",
            "su": "2",
            "applang": "en_us", # Display Lang
            "ds": "0",
            "from_type": "1",
            "hdcp": "22", # Client Display Content Protection version
            "cc_site": self.active_session["mode_code"], # User mode code
            "cc_business": "1",
            "pano264": "800",
            "pano265": "800",
            "pre": "0",
            "ap": "1",
            "qd_v": "1",
            "fv": "2",
            "rt": "1",
            "dcv": "6",
            "ori": "puma",
            "X-USER-MODE": self.active_session["mode_code"], # User mode code
            "ff": "ts" # Segement Type
        }
        data.update(video_params)
        data.update(audio_params)

        # If the parameters are different, account ban...
        if codec_key == "8k":
            data.update({
                "ps": "0", # ?
                "pt": "28000", # ?
                "fr": "60", # Frame Rate(HFR)
            })
        
        try:
            json_data = self._get_dash_stream(data)
        except SystemExit:
            return [], [], []
            
        program = json_data.get("data", {}).get("program", {})
        self.playback_data["svp"] = json_data.get("data", {}).get("svp", {}) # Chapter

        return program.get("video", []), program.get("audio", []), program.get("stl", [])

    def _get_audio_segment_info(self, path: str) -> dict:
        url = self.config["endpoint"]["audio"].format(path=path)
        headers = {
            "Accept": "*/*",
            "User-Agent": self.config["device"]["user_agent"]
        }
        try:
            res = self.session.get(url, headers=headers)
            return res.json() if res.status_code == 200 else {}
        except Exception:
            return {}

    def _get_mode_code(self) -> str:
        url = self.config["endpoint"]["mode"]
        params = {"format": "json", "scene": "4"}
        headers = {"Accept": "*/*"}
        data = self._request("GET", url, params=params, headers=headers)
        return data.get("data", {}).get("country", "br").lower() if data else "br"
        
    def _fetch_pck(self) -> str:
        endpoint = self.config["endpoint"]["pck"]
        params = {
            "platformId": "3",
            "modeCode": self.active_session["mode_code"],
            "langCode": self.active_session["lang_code"],
            "deviceId": self.active_session["qc005"],
            "uid": self.active_session["uid"],
            "interfaceCode": "indexnav_layer"
        }
        headers = {
            "User-Agent": self.config["device"]["user_agent_bws"],
            "Cookie": "; ".join([f"{cookie.name}={cookie.value}" for cookie in self.cookies])
        }
        res_data = self._request("GET", endpoint, params=params, headers=headers)
        
        for item in res_data.get("data", []):
            url = item.get("apiUrl")
            if url:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                return qs.get("P00001", [None])[0]
        return None
    
    def _get_ptid(self) -> str:
        endpoint = self.config["endpoint"]["ptid"]
        params = {
            "platformId": "3",
            "modeCode": self.active_session["mode_code"],
            "langCode": self.active_session["lang_code"],
            "deviceId": self.active_session["qc005"]
        }
        headers = {
            "User-Agent": self.config["device"]["user_agent_bws"],
            "Cookie": "; ".join([f"{cookie.name}={cookie.value}" for cookie in self.cookies])
        }
        data = self._request("GET", endpoint, params=params, headers=headers)
        ptid = data.get("data", {}).get("ptid", "") if data else ""
        if ptid.startswith("0101003"):
            ptid = ptid.replace("0101003", "0202200", 1)
        return ptid

    def _get_vip_info(self) -> dict:
        endpoint = self.config["endpoint"]["vip"]
        params = {
            "platformId": "3",
            "modeCode": self.active_session["mode_code"],
            "langCode": self.active_session["lang_code"],
            "deviceId": self.active_session["qc005"],
            "fields": "userinfo",
            "version": "1.0",
            "vipInfoVersion": "5.0",
        }
        headers = {
            "User-Agent": self.config["device"]["user_agent_bws"],
            "Cookie": "; ".join([f"{cookie.name}={cookie.value}" for cookie in self.cookies])
        }
        data = self._request("GET", endpoint, params=params, headers=headers)
        return data
    
    def _request(self, method: str, endpoint: str, params: dict = None, headers: dict = None, payload: dict = None) -> Any:
        _headers = self.session.headers.copy()
        if headers: 
            _headers.update(headers)
        
        req = Request(method, endpoint, headers=_headers, params=params, json=payload)
        prepped = self.session.prepare_request(req)

        try:
            res = self.session.send(prepped)
            res.raise_for_status()
            data = res.json() if res.text else {}
            return data
        except Exception as e:
            ignore_keys = ["episode", "stream", "audio"]
            if any(self.config["endpoints"][key] in endpoint for key in ignore_keys):
                raise e
            else:
                self.log.error(f"API Request failed: {e}", exc_info=False)
                sys.exit(1)
