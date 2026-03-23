"""Remote service adapter for unshackle.

Implements the Service interface by proxying authenticate, get_titles,
get_tracks, get_chapters, and license methods to a remote unshackle server.
Everything else (track selection, download, decrypt, mux) runs locally.
"""

from __future__ import annotations

import base64
import logging
import time
from enum import Enum
from http.cookiejar import CookieJar
from typing import Any, Dict, Optional, Union

import click
import requests
from langcodes import Language
from requests.adapters import HTTPAdapter, Retry
from rich.padding import Padding
from rich.rule import Rule

from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.titles import Title_T, Titles_T
from unshackle.core.titles.episode import Episode, Series
from unshackle.core.titles.movie import Movie, Movies
from unshackle.core.tracks import Audio, Chapter, Chapters, Subtitle, Tracks, Video
from unshackle.core.tracks.attachment import Attachment
from unshackle.core.tracks.track import Track

log = logging.getLogger("remote_service")


class RemoteClient:
    """HTTP client for the unshackle serve API."""

    def __init__(self, server_url: str, api_key: str) -> None:
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self._session: Optional[requests.Session] = None

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            from unshackle.core import __version__

            self._session = requests.Session()
            self._session.headers["User-Agent"] = f"unshackle/{__version__}"
            if self.api_key:
                self._session.headers["X-Secret-Key"] = self.api_key
        return self._session

    def _request(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.server_url}{endpoint}"
        try:
            resp = getattr(self.session, method)(url, json=data, timeout=120 if method == "post" else 30)
        except requests.ConnectionError:
            log.error(f"Could not connect to remote server at {self.server_url}. Is it running? (unshackle serve)")
            raise SystemExit(1)
        except requests.Timeout:
            log.error(f"Request to remote server timed out: {endpoint}")
            raise SystemExit(1)
        result = resp.json()
        if resp.status_code >= 400:
            error_msg = result.get("message", resp.text)
            error_code = result.get("error_code", "UNKNOWN")
            raise click.ClickException(f"Server error [{error_code}]: {error_msg}")
        return result

    def post(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("post", endpoint, data)

    def get(self, endpoint: str) -> Dict[str, Any]:
        return self._request("get", endpoint)

    def delete(self, endpoint: str) -> Dict[str, Any]:
        return self._request("delete", endpoint)


def _enum_get(enum_cls: type[Enum], name: Optional[str], default: Any = None) -> Any:
    """Safely get an enum value by name."""
    if not name:
        return default
    try:
        return enum_cls[name]
    except KeyError:
        return default


def _deserialize_video(data: Dict[str, Any]) -> Video:
    v = Video(
        url=data.get("url") or "https://placeholder",
        language=Language.get(data.get("language") or "und"),
        descriptor=_enum_get(Track.Descriptor, data.get("descriptor"), Track.Descriptor.URL),
        codec=_enum_get(Video.Codec, data.get("codec")),
        range_=_enum_get(Video.Range, data.get("range"), Video.Range.SDR),
        bitrate=data["bitrate"] * 1000 if data.get("bitrate") else 0,
        width=data.get("width") or 0,
        height=data.get("height") or 0,
        fps=data.get("fps"),
        id_=data.get("id"),
    )
    return v


def _deserialize_audio(data: Dict[str, Any]) -> Audio:
    a = Audio(
        url=data.get("url") or "https://placeholder",
        language=Language.get(data.get("language") or "und"),
        descriptor=_enum_get(Track.Descriptor, data.get("descriptor"), Track.Descriptor.URL),
        codec=_enum_get(Audio.Codec, data.get("codec")),
        bitrate=data["bitrate"] * 1000 if data.get("bitrate") else 0,
        channels=data.get("channels"),
        joc=1 if data.get("atmos") else 0,
        descriptive=data.get("descriptive", False),
        id_=data.get("id"),
    )
    return a


def _deserialize_subtitle(data: Dict[str, Any]) -> Subtitle:
    return Subtitle(
        url=data.get("url") or "https://placeholder",
        language=Language.get(data.get("language") or "und"),
        descriptor=_enum_get(Track.Descriptor, data.get("descriptor"), Track.Descriptor.URL),
        codec=_enum_get(Subtitle.Codec, data.get("codec")),
        cc=data.get("cc", False),
        sdh=data.get("sdh", False),
        forced=data.get("forced", False),
        id_=data.get("id"),
    )


def _reconstruct_drm(drm_list: Optional[list]) -> list:
    """Reconstruct DRM objects from serialized API data."""
    if not drm_list:
        return []
    result = []
    for drm_info in drm_list:
        drm_type = drm_info.get("type", "")
        pssh_str = drm_info.get("pssh")
        if not pssh_str:
            continue
        try:
            if drm_type == "widevine":
                from pywidevine.pssh import PSSH as WidevinePSSH

                from unshackle.core.drm import Widevine

                wv_pssh = WidevinePSSH(pssh_str)
                result.append(Widevine(pssh=wv_pssh))
            elif drm_type == "playready":
                import base64 as b64

                from pyplayready.system.pssh import PSSH as PlayReadyPSSH

                from unshackle.core.drm import PlayReady

                pr_pssh = PlayReadyPSSH(b64.b64decode(pssh_str))
                result.append(PlayReady(pssh=pr_pssh, pssh_b64=pssh_str))
        except Exception:
            continue
    return result


def _build_tracks(data: Dict[str, Any]) -> Tracks:
    tracks = Tracks()
    tracks.videos = [_deserialize_video(v) for v in data.get("video", [])]
    tracks.audio = [_deserialize_audio(a) for a in data.get("audio", [])]
    tracks.subtitles = [_deserialize_subtitle(s) for s in data.get("subtitles", [])]

    for track_data, track_obj in [
        *zip(data.get("video", []), tracks.videos),
        *zip(data.get("audio", []), tracks.audio),
    ]:
        drm_objs = _reconstruct_drm(track_data.get("drm"))
        if drm_objs:
            track_obj.drm = drm_objs
    tracks.attachments = [
        Attachment(url=a["url"], name=a.get("name"), mime_type=a.get("mime_type"), description=a.get("description"))
        for a in data.get("attachments", [])
    ]
    return tracks


def _resolve_manifest_data(tracks: Tracks, manifests: list, session: Any) -> None:
    """Re-parse serialized manifests and populate track.data for downloading.

    The server serializes DASH and ISM manifest XML as zlib-compressed base64.
    We decode and decompress locally, re-parse with the appropriate manifest
    parser, then match each remote track to the locally-parsed track by ID
    to copy track.data. HLS is skipped as it re-fetches from track.url.
    """
    import base64 as b64
    import zlib

    if not manifests:
        return

    log_m = logging.getLogger("remote_service")
    all_tracks = list(tracks.videos) + list(tracks.audio) + list(tracks.subtitles)

    for manifest_info in manifests:
        m_type = manifest_info.get("type")
        m_url = manifest_info.get("url")
        m_data = manifest_info.get("data")
        if not m_data or not m_url:
            continue

        try:
            raw = zlib.decompress(b64.b64decode(m_data))

            if m_type == "dash":
                from lxml import etree

                from unshackle.core.manifests import DASH

                xml_tree = etree.fromstring(raw)
                fallback_lang = next(
                    (t.language for t in all_tracks if t.language and str(t.language) != "und"),
                    None,
                )
                local_tracks = DASH(xml_tree, m_url).to_tracks(language=fallback_lang)
            elif m_type == "ism":
                from lxml import etree

                from unshackle.core.manifests import ISM

                local_tracks = ISM(etree.fromstring(raw), m_url).to_tracks()
            else:
                continue

            local_all = list(local_tracks.videos) + list(local_tracks.audio) + list(local_tracks.subtitles)
            for remote_track in all_tracks:
                if remote_track.data.get(m_type):
                    continue
                matched = _match_track(remote_track, local_all)
                if matched and matched.data.get(m_type):
                    remote_track.data.update(matched.data)
                    remote_track.descriptor = matched.descriptor
                    if matched.drm and not remote_track.drm:
                        remote_track.drm = matched.drm

        except Exception as e:
            log_m.warning("Failed to re-parse %s manifest from %s: %s", m_type, m_url, e)


def _match_track(remote_track: Track, local_tracks: list) -> Optional[Track]:
    """Match a remote track to a locally-parsed track by ID or attributes."""
    remote_id = str(remote_track.id)
    for lt in local_tracks:
        if str(lt.id) == remote_id:
            return lt

    for lt in local_tracks:
        if type(lt).__name__ != type(remote_track).__name__:
            continue
        if lt.codec != remote_track.codec or str(lt.language) != str(remote_track.language):
            continue
        if hasattr(lt, "width") and hasattr(remote_track, "width"):
            if lt.width == remote_track.width and lt.height == remote_track.height:
                return lt
        elif hasattr(lt, "channels") and hasattr(remote_track, "channels"):
            if lt.bitrate == remote_track.bitrate:
                return lt
        elif hasattr(lt, "forced"):
            if lt.forced == remote_track.forced and lt.sdh == remote_track.sdh:
                return lt
    return None


def _build_title(info: Dict[str, Any], service_tag: str, fallback_id: str) -> Union[Episode, Movie]:
    svc_class = type(service_tag, (), {})
    lang = Language.get(info["language"]) if info.get("language") else None
    if info.get("type") == "episode":
        return Episode(
            id_=info.get("id", fallback_id),
            service=svc_class,
            title=info.get("series_title", "Unknown"),
            season=info.get("season", 0),
            number=info.get("number", 0),
            name=info.get("name"),
            year=info.get("year"),
            language=lang,
        )
    return Movie(
        id_=info.get("id", fallback_id),
        service=svc_class,
        name=info.get("name", "Unknown"),
        year=info.get("year"),
        language=lang,
    )


def resolve_server(server_name: Optional[str]) -> tuple[str, str, dict]:
    """Resolve server URL, API key, and per-service config from remote_services."""
    remote_services = config.remote_services
    if not remote_services:
        raise click.ClickException(
            "No remote services configured. Add 'remote_services' to your unshackle.yaml:\n\n"
            "  remote_services:\n"
            "    my_server:\n"
            '      url: "https://server:8080"\n'
            '      api_key: "your-api-key"'
        )

    if server_name:
        svc = remote_services.get(server_name)
        if not svc:
            available = ", ".join(remote_services.keys())
            raise click.ClickException(f"Remote service '{server_name}' not found. Available: {available}")
        services = svc.get("services", {})
        services["_server_cdm"] = svc.get("server_cdm", False)
        return svc["url"], svc.get("api_key", ""), services

    if len(remote_services) == 1:
        name, svc = next(iter(remote_services.items()))
        log.info(f"Using remote service: {name}")
        services = svc.get("services", {})
        services["_server_cdm"] = svc.get("server_cdm", False)
        return svc["url"], svc.get("api_key", ""), services

    available = ", ".join(remote_services.keys())
    raise click.ClickException(f"Multiple remote services configured. Use --server to select one: {available}")


def _load_credentials_for_transport(service_tag: str, profile: Optional[str]) -> Optional[Dict[str, str]]:
    from unshackle.commands.dl import dl

    credential = dl.get_credentials(service_tag, profile)
    if credential:
        result: Dict[str, str] = {"username": credential.username, "password": credential.password}
        if credential.extra:
            result["extra"] = credential.extra
        return result
    return None


def _load_cookies_for_transport(service_tag: str, profile: Optional[str]) -> Optional[str]:
    import zlib

    from unshackle.commands.dl import dl

    cookie_path = dl.get_cookie_path(service_tag, profile)
    if cookie_path and cookie_path.exists():
        return base64.b64encode(zlib.compress(cookie_path.read_bytes())).decode("ascii")
    return None


def _resolve_proxy(proxy_arg: Optional[str]) -> Optional[str]:
    if not proxy_arg:
        return None

    from unshackle.core.proxies.resolve import initialize_proxy_providers, resolve_proxy

    try:
        providers = initialize_proxy_providers()
        return resolve_proxy(proxy_arg, providers)
    except ValueError as e:
        raise click.ClickException(str(e))


class RemoteService:
    """Service adapter that proxies to a remote unshackle server.

    Implements the same interface dl.py's result() expects without
    subclassing Service (avoids proxy/geofence setup in __init__).
    """

    ALIASES: tuple[str, ...] = ()
    GEOFENCE: tuple[str, ...] = ()
    NO_SUBTITLES: bool = False

    def __init__(
        self,
        ctx: click.Context,
        service_tag: str,
        title_id: str,
        server_url: str,
        api_key: str,
        services_config: dict,
        service_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.__class__.__name__ = service_tag
        console.print(Padding(Rule(f"[rule.text]Service: {service_tag} (Remote)"), (1, 2)))

        self.service_tag = service_tag
        self.title_id = title_id
        self.client = RemoteClient(server_url, api_key)
        self.ctx = ctx
        self._service_params = service_params or {}
        self.log = logging.getLogger(service_tag)
        self.credential: Optional[Credential] = None
        self.current_region: Optional[str] = None
        self.title_cache = None
        self._titles: Optional[Titles_T] = None
        self._tracks_by_title: Dict[str, Tracks] = {}
        self._chapters_by_title: Dict[str, list] = {}
        self._session_id: Optional[str] = None
        self._server_cdm_type: str = "widevine"

        self._session = requests.Session()
        self._session.headers.update(config.headers)
        self._session.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(total=5, backoff_factor=0.2, status_forcelist=[429, 500, 502, 503, 504]),
                pool_block=True,
            ),
        )
        self._session.mount("http://", self._session.adapters["https://"])

        svc_config = services_config.get(service_tag, {})
        self._server_cdm = services_config.get("_server_cdm", False)
        self._apply_service_config(svc_config)

    def _apply_service_config(self, svc_config: dict) -> None:
        if not svc_config:
            return
        config_maps = {
            "cdm": ("cdm", self.service_tag),
            "decryption": ("decryption_map", self.service_tag),
            "downloader": ("downloader_map", self.service_tag),
        }
        for key, (attr, tag) in config_maps.items():
            if svc_config.get(key):
                target = getattr(config, attr, None)
                if target is None:
                    setattr(config, attr, {})
                    target = getattr(config, attr)
                target[tag] = svc_config[key]

        if svc_config.get("downloader"):
            config.downloader = svc_config["downloader"]
        if svc_config.get("decryption"):
            config.decryption = svc_config["decryption"]

        extra = {k: v for k, v in svc_config.items() if k not in config_maps}
        if extra:
            existing = config.services.get(self.service_tag, {})
            for key, value in extra.items():
                if key in existing and isinstance(existing[key], dict) and isinstance(value, dict):
                    existing[key].update(value)
                else:
                    existing[key] = value
            config.services[self.service_tag] = existing

    @property
    def session(self) -> requests.Session:
        return self._session

    @property
    def title(self) -> str:
        return self.title_id

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        self.credential = credential
        profile = self.ctx.parent.params.get("profile") if self.ctx.parent else None
        proxy = self.ctx.parent.params.get("proxy") if self.ctx.parent else None
        no_proxy = self.ctx.parent.params.get("no_proxy", False) if self.ctx.parent else False

        create_data: Dict[str, Any] = {"service": self.service_tag, "title_id": self.title_id}

        credentials = _load_credentials_for_transport(self.service_tag, profile)
        if credentials:
            create_data["credentials"] = credentials

        cookies_text = _load_cookies_for_transport(self.service_tag, profile)
        if cookies_text:
            create_data["cookies"] = cookies_text

        if not no_proxy and proxy:
            resolved_proxy = _resolve_proxy(proxy)
            if resolved_proxy:
                create_data["proxy"] = resolved_proxy

        if not no_proxy and not proxy:
            try:
                from unshackle.core.utilities import get_cached_ip_info

                ip_info = get_cached_ip_info(self._session)
                if ip_info and ip_info.get("country"):
                    create_data["client_region"] = ip_info["country"].lower()
            except Exception:
                pass

        if profile:
            create_data["profile"] = profile
        if no_proxy:
            create_data["no_proxy"] = True
        if self._service_params:
            create_data.update(self._service_params)

        cdm = self.ctx.obj.cdm if self.ctx.obj else None
        if cdm is not None:
            from unshackle.core.cdm.detect import is_playready_cdm

            create_data["cdm_type"] = "playready" if is_playready_cdm(cdm) else "widevine"

        cache_data = self._load_cache_files()
        if cache_data:
            create_data["cache"] = cache_data

        result = self.client.post("/api/session/create", create_data)
        self._session_id = result["session_id"]

        status = result.get("status", "authenticated")
        if status == "authenticating":
            self._poll_auth_completion()

    def _poll_auth_completion(self, poll_interval: float = 2.0, timeout: float = 600.0) -> None:
        """Poll the server until authentication completes, handling interactive prompts.

        When the server needs user input (OTP, device code, PIN), it returns
        ``pending_input`` with a prompt. We display it locally, collect the
        response, and POST it back. The server resumes its auth flow.
        """
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            resp = self.client.get(f"/api/session/{self._session_id}/prompt")
            status = resp.get("status")

            if status == "authenticated":
                return

            if status == "failed":
                error = resp.get("error", "Authentication failed on server")
                raise click.ClickException(f"Remote auth failed: {error}")

            if status == "pending_input":
                prompt = resp.get("prompt", "Enter input: ")
                user_response = click.prompt(prompt.rstrip("\n "), default="", show_default=False)
                self.client.post(
                    f"/api/session/{self._session_id}/prompt",
                    {"response": user_response},
                )
                continue

            time.sleep(poll_interval)

        raise click.ClickException("Remote authentication timed out")

    def get_titles(self) -> Titles_T:
        if self._titles is not None:
            return self._titles
        result = self.client.get(f"/api/session/{self._session_id}/titles")
        titles_list = [_build_title(t, self.service_tag, self.title_id) for t in result.get("titles", [])]
        self._titles = (
            Series(titles_list) if titles_list and isinstance(titles_list[0], Episode) else Movies(titles_list)
        )
        return self._titles

    def get_titles_cached(self, title_id: str = None) -> Titles_T:
        return self.get_titles()

    def get_tracks(self, title: Title_T) -> Tracks:
        title_id = str(title.id)
        if title_id in self._tracks_by_title:
            return self._tracks_by_title[title_id]
        result = self.client.post(f"/api/session/{self._session_id}/tracks", {"title_id": title_id})
        tracks = _build_tracks(result)

        for k, v in result.get("session_headers", {}).items():
            if k.lower() not in ("host", "content-length", "content-type"):
                self._session.headers[k] = v
        for k, v in result.get("session_cookies", {}).items():
            self._session.cookies.set(k, v)

        _resolve_manifest_data(tracks, result.get("manifests", []), self._session)

        self._server_cdm_type = result.get("server_cdm_type", "widevine")

        self._tracks_by_title[title_id] = tracks
        self._chapters_by_title[title_id] = result.get("chapters", [])

        return tracks

    def resolve_server_keys(self, title: Title_T) -> None:
        """Resolve DRM keys via server CDM for all tracks on a title.

        Called by dl.py between track selection and download. The server
        decides which CDM device to use and tells the client via
        server_cdm_type. We send track IDs and the server does the full
        CDM flow, returning KID:KEY pairs.
        """
        if not self._server_cdm:
            return

        from uuid import UUID

        track_ids = [str(t.id) for t in title.tracks.videos + title.tracks.audio]
        if not track_ids:
            return

        drm_type = getattr(self, "_server_cdm_type", "widevine")
        self.log.debug(f"Requesting server CDM keys (server_cdm_type={drm_type})")

        try:
            with console.status("Retrieving Remote License...", spinner="dots"):
                resp = self.client.post(
                    f"/api/session/{self._session_id}/license",
                    {
                        "track_ids": track_ids,
                        "mode": "server_cdm",
                        "drm_type": drm_type,
                    },
                )
            keys_by_track = resp.get("keys", {})
            server_drm_type = resp.get("drm_type", drm_type)
            self._server_cdm_type = server_drm_type
            self.log.debug(f"Server responded with drm_type={server_drm_type}, keys for {len(keys_by_track)} track(s)")

            for track in title.tracks:
                track_keys = keys_by_track.get(str(track.id), {})
                if not track_keys:
                    continue

                kid_list = list(track_keys.keys())
                drm_obj = self._create_drm_stub(server_drm_type, kid_list)
                for kid_hex, key_hex in track_keys.items():
                    drm_obj.content_keys[UUID(hex=kid_hex)] = key_hex
                track.drm = [drm_obj]
                self.log.debug(
                    f"Track {track.id}: set DRM to {drm_obj.__class__.__name__} with {len(track_keys)} key(s)"
                )
            key_count = sum(len(v) for v in keys_by_track.values())
            if key_count:
                self.log.debug(f"Server CDM resolved {key_count} key(s) using {server_drm_type.upper()}")
        except Exception as e:
            self.log.warning("Failed to resolve server CDM keys: %s", e)

    @staticmethod
    def _create_drm_stub(drm_type: str, kid_hexes: list[str]) -> Any:
        """Create a DRM object stub matching the type the server actually used.

        For server_cdm mode, this is only used for display — keys are already
        resolved. We build a minimal DRM object that holds content_keys.
        """
        from uuid import UUID

        if drm_type == "playready":
            import base64 as b64
            import struct

            from pyplayready.system.pssh import PSSH as PlayReadyPSSH

            from unshackle.core.drm import PlayReady

            kid_uuids = [UUID(hex=k) for k in kid_hexes]
            kid_b64 = b64.b64encode(kid_uuids[0].bytes_le).decode()
            wrm_xml = (
                '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="4.0.0.0">'
                f"<DATA><PROTECTINFO><KEYLEN>16</KEYLEN><ALGID>AESCTR</ALGID></PROTECTINFO>"
                f"<KID>{kid_b64}</KID></DATA></WRMHEADER>"
            )
            wrm_bytes = wrm_xml.encode("utf-16-le")
            record_length = len(wrm_bytes)
            obj_length = 4 + 2 + 2 + 2 + record_length
            pr_obj = struct.pack("<IHH", obj_length, 1, 1) + struct.pack("<H", record_length) + wrm_bytes
            pr_pssh = PlayReadyPSSH(pr_obj)
            pssh_b64 = b64.b64encode(pr_obj).decode("ascii")
            drm = PlayReady(pssh=pr_pssh, pssh_b64=pssh_b64)
            for kid_uuid in kid_uuids:
                if kid_uuid not in drm.kids:
                    drm.kids.append(kid_uuid)
            return drm
        else:
            from pywidevine.pssh import PSSH as WvPSSH

            from unshackle.core.drm import Widevine

            kid_uuids = [UUID(hex=k) for k in kid_hexes]
            WIDEVINE_SYSTEM_ID = UUID("edef8ba9-79d6-4ace-a3c8-27dcd51d21ed")
            dummy_pssh = WvPSSH.new(system_id=WIDEVINE_SYSTEM_ID, key_ids=kid_uuids)
            return Widevine(pssh=dummy_pssh, kid=kid_hexes[0])

    def get_chapters(self, title: Title_T) -> Chapters:
        title_id = str(title.id)
        if title_id not in self._chapters_by_title:
            self.get_tracks(title)
        raw = self._chapters_by_title.get(title_id, [])
        return Chapters([Chapter(ch["timestamp"], ch.get("name")) for ch in raw])

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        return self._proxy_license(challenge, track, "widevine")

    def get_playready_license(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Optional[Union[bytes, str]]:
        return self._proxy_license(challenge, track, "playready")

    def get_widevine_service_certificate(
        self,
        *,
        challenge: bytes,
        title: Title_T,
        track: AnyTrack,
    ) -> Union[bytes, str]:
        try:
            resp = self.client.post(
                f"/api/session/{self._session_id}/license",
                {
                    "track_id": str(track.id),
                    "challenge": base64.b64encode(challenge).decode("ascii"),
                    "drm_type": "widevine",
                    "is_certificate": True,
                },
            )
            return base64.b64decode(resp["license"])
        except Exception:
            return None

    def _proxy_license(self, challenge: Union[bytes, str], track: AnyTrack, drm_type: str) -> bytes:
        if isinstance(challenge, str):
            challenge = challenge.encode("utf-8")

        pssh_b64 = None
        if track.drm:
            for drm_obj in track.drm:
                drm_class = drm_obj.__class__.__name__
                if drm_type == "playready" and drm_class == "PlayReady":
                    pssh_b64 = drm_obj.data["pssh_b64"]
                    break
                elif drm_type == "widevine" and drm_class == "Widevine":
                    pssh_b64 = drm_obj.pssh.dumps()
                    break

        if self._server_cdm:
            from uuid import UUID

            if pssh_b64:
                try:
                    resp = self.client.post(
                        f"/api/session/{self._session_id}/license",
                        {
                            "track_id": str(track.id),
                            "drm_type": drm_type,
                            "mode": "server_cdm",
                            "pssh": pssh_b64,
                        },
                    )
                    keys = resp.get("keys", {})
                    if keys and track.drm:
                        for drm_obj in track.drm:
                            if hasattr(drm_obj, "content_keys"):
                                for kid_hex, key_hex in keys.items():
                                    drm_obj.content_keys[UUID(hex=kid_hex)] = key_hex
                        return challenge
                except Exception as e:
                    self.log.warning("server_cdm license failed: %s", e)
            return challenge

        payload = {
            "track_id": str(track.id),
            "challenge": base64.b64encode(challenge).decode("ascii"),
            "drm_type": drm_type,
        }
        if pssh_b64:
            payload["pssh"] = pssh_b64

        resp = self.client.post(f"/api/session/{self._session_id}/license", payload)
        return base64.b64decode(resp["license"])

    def on_segment_downloaded(self, track: AnyTrack, segment: Any) -> None:
        pass

    def on_track_downloaded(self, track: AnyTrack) -> None:
        pass

    def on_track_decrypted(self, track: AnyTrack, drm: Any, segment: Any = None) -> None:
        pass

    def on_track_repacked(self, track: AnyTrack) -> None:
        pass

    def on_track_multiplex(self, track: AnyTrack) -> None:
        pass

    def close(self) -> None:
        if self._session_id:
            try:
                result = self.client.delete(f"/api/session/{self._session_id}")
                self._save_returned_cache(result.get("cache", {}))
            except Exception as e:
                self.log.warning(f"Failed to clean up remote session: {e}")
            self._session_id = None

    def _save_returned_cache(self, cache_data: Dict[str, str]) -> None:
        """Save cache files returned by the server to the local cache directory.

        The server returns updated cache files (e.g. refreshed tokens) on
        session close. Writing them locally means the next remote session
        can forward them back, skipping interactive auth.
        """
        if not cache_data:
            return

        import zlib

        cache_dir = config.directories.cache / self.service_tag
        cache_dir.mkdir(parents=True, exist_ok=True)

        for key, content in cache_data.items():
            try:
                decompressed = zlib.decompress(base64.b64decode(content))
                (cache_dir / key).with_suffix(".json").write_bytes(decompressed)
            except Exception as e:
                self.log.warning(f"Failed to save returned cache file '{key}': {e}")

        self.log.info(f"Saved {len(cache_data)} cache file(s) from server")

    def _load_cache_files(self) -> Dict[str, str]:
        import zlib

        cache_dir = config.directories.cache / self.service_tag
        if not cache_dir.is_dir():
            return {}
        return {
            f.stem: base64.b64encode(zlib.compress(f.read_bytes())).decode("ascii")
            for f in cache_dir.glob("*.json")
            if not f.stem.startswith("titles_")
        }


__all__ = ("RemoteClient", "RemoteService", "resolve_server")
