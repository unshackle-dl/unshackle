"""Remote service adapter for unshackle.

Implements the Service interface by proxying authenticate, get_titles,
get_tracks, get_chapters, and license methods to a remote unshackle server.
Everything else (track selection, download, decrypt, mux) runs locally.
"""
from __future__ import annotations

import base64
import logging
import re
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
            self._session = requests.Session()
            if self.api_key:
                self._session.headers["X-Secret-Key"] = self.api_key
        return self._session

    def _request(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.server_url}{endpoint}"
        resp = getattr(self.session, method)(url, json=data, timeout=120 if method == "post" else 30)
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
    return Video(
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


def _deserialize_audio(data: Dict[str, Any]) -> Audio:
    return Audio(
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


def _build_tracks(data: Dict[str, Any]) -> Tracks:
    tracks = Tracks()
    tracks.videos = [_deserialize_video(v) for v in data.get("video", [])]
    tracks.audio = [_deserialize_audio(a) for a in data.get("audio", [])]
    tracks.subtitles = [_deserialize_subtitle(s) for s in data.get("subtitles", [])]
    tracks.attachments = [
        Attachment(url=a["url"], name=a.get("name"), mime_type=a.get("mime_type"), description=a.get("description"))
        for a in data.get("attachments", [])
    ]
    return tracks


def _build_title(info: Dict[str, Any], service_tag: str, fallback_id: str) -> Union[Episode, Movie]:
    svc_class = type(service_tag, (), {})
    lang = Language.get(info["language"]) if info.get("language") else None
    if info.get("type") == "episode":
        return Episode(
            id_=info.get("id", fallback_id), service=svc_class,
            title=info.get("series_title", "Unknown"),
            season=info.get("season", 0), number=info.get("number", 0),
            name=info.get("name"), year=info.get("year"), language=lang,
        )
    return Movie(
        id_=info.get("id", fallback_id), service=svc_class,
        name=info.get("name", "Unknown"), year=info.get("year"), language=lang,
    )


def resolve_server(server_name: Optional[str]) -> tuple[str, str, dict]:
    """Resolve server URL, API key, and per-service config from remote_services."""
    remote_services = config.remote_services
    if not remote_services:
        raise click.ClickException(
            "No remote services configured. Add 'remote_services' to your unshackle.yaml:\n\n"
            "  remote_services:\n"
            "    my_server:\n"
            "      url: \"https://server:8080\"\n"
            "      api_key: \"your-api-key\""
        )

    if server_name:
        svc = remote_services.get(server_name)
        if not svc:
            available = ", ".join(remote_services.keys())
            raise click.ClickException(f"Remote service '{server_name}' not found. Available: {available}")
        return svc["url"], svc.get("api_key", ""), svc.get("services", {})

    if len(remote_services) == 1:
        name, svc = next(iter(remote_services.items()))
        log.info(f"Using remote service: {name}")
        return svc["url"], svc.get("api_key", ""), svc.get("services", {})

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
    from unshackle.commands.dl import dl
    cookie_path = dl.get_cookie_path(service_tag, profile)
    if cookie_path and cookie_path.exists():
        return cookie_path.read_text(encoding="utf-8")
    return None


def _resolve_proxy(proxy_arg: Optional[str]) -> Optional[str]:
    if not proxy_arg:
        return None
    if re.match(r"^(https?://|socks)", proxy_arg):
        return proxy_arg

    from unshackle.core.proxies.basic import Basic
    from unshackle.core.proxies.nordvpn import NordVPN
    from unshackle.core.proxies.surfsharkvpn import SurfsharkVPN

    providers: list = []
    proxy_config = config.proxy_providers
    if proxy_config.get("basic"):
        providers.append(Basic(**proxy_config["basic"]))
    if proxy_config.get("nordvpn"):
        providers.append(NordVPN(**proxy_config["nordvpn"]))
    if proxy_config.get("surfsharkvpn"):
        providers.append(SurfsharkVPN(**proxy_config["surfsharkvpn"]))

    requested_provider = None
    query = proxy_arg
    if re.match(r"^[a-z]+:.+$", proxy_arg, re.IGNORECASE):
        requested_provider, query = proxy_arg.split(":", maxsplit=1)

    if requested_provider:
        provider = next(
            (x for x in providers if x.__class__.__name__.lower() == requested_provider.lower()), None,
        )
        if not provider:
            raise click.ClickException(f"Proxy provider '{requested_provider}' not found.")
        proxy_uri = provider.get_proxy(query)
        if not proxy_uri:
            raise click.ClickException(f"Proxy provider {requested_provider} had no proxy for {query}")
        return proxy_uri

    for provider in providers:
        proxy_uri = provider.get_proxy(query)
        if proxy_uri:
            return proxy_uri
    raise click.ClickException(f"No proxy provider had a proxy for {proxy_arg}")


class RemoteService:
    """Service adapter that proxies to a remote unshackle server.

    Implements the same interface dl.py's result() expects without
    subclassing Service (avoids proxy/geofence setup in __init__).
    """

    ALIASES: tuple[str, ...] = ()
    GEOFENCE: tuple[str, ...] = ()
    NO_SUBTITLES: bool = False

    def __init__(
        self, ctx: click.Context, service_tag: str, title_id: str,
        server_url: str, api_key: str, services_config: dict,
    ) -> None:
        self.__class__.__name__ = service_tag
        console.print(Padding(Rule(f"[rule.text]Service: {service_tag} (Remote)"), (1, 2)))

        self.service_tag = service_tag
        self.title_id = title_id
        self.client = RemoteClient(server_url, api_key)
        self.ctx = ctx
        self.log = logging.getLogger(service_tag)
        self.credential: Optional[Credential] = None
        self.current_region: Optional[str] = None
        self.title_cache = None
        self._titles: Optional[Titles_T] = None
        self._tracks_by_title: Dict[str, Tracks] = {}
        self._chapters_by_title: Dict[str, list] = {}
        self._session_id: Optional[str] = None

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

        self._apply_service_config(services_config.get(service_tag, {}))

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

        if profile:
            create_data["profile"] = profile
        if no_proxy:
            create_data["no_proxy"] = True

        cache_data = self._load_cache_files()
        if cache_data:
            create_data["cache"] = cache_data

        result = self.client.post("/api/session/create", create_data)
        self._session_id = result["session_id"]

    def get_titles(self) -> Titles_T:
        if self._titles is not None:
            return self._titles
        result = self.client.get(f"/api/session/{self._session_id}/titles")
        titles_list = [_build_title(t, self.service_tag, self.title_id) for t in result.get("titles", [])]
        self._titles = Series(titles_list) if titles_list and isinstance(titles_list[0], Episode) else Movies(titles_list)
        return self._titles

    def get_titles_cached(self, title_id: str = None) -> Titles_T:
        return self.get_titles()

    def get_tracks(self, title: Title_T) -> Tracks:
        title_id = str(title.id)
        if title_id in self._tracks_by_title:
            return self._tracks_by_title[title_id]
        result = self.client.post(f"/api/session/{self._session_id}/tracks", {"title_id": title_id})
        tracks = _build_tracks(result)
        self._tracks_by_title[title_id] = tracks
        self._chapters_by_title[title_id] = result.get("chapters", [])
        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        title_id = str(title.id)
        if title_id not in self._chapters_by_title:
            self.get_tracks(title)
        raw = self._chapters_by_title.get(title_id, [])
        return Chapters([Chapter(ch["timestamp"], ch.get("name")) for ch in raw])

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        return self._proxy_license(challenge, track, "widevine")

    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        return self._proxy_license(challenge, track, "playready")

    def get_widevine_service_certificate(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack,
    ) -> Union[bytes, str]:
        try:
            resp = self.client.post(f"/api/session/{self._session_id}/license", {
                "track_id": str(track.id),
                "challenge": base64.b64encode(challenge).decode("ascii"),
                "drm_type": "widevine", "is_certificate": True,
            })
            return base64.b64decode(resp["license"])
        except Exception:
            return None

    def _proxy_license(self, challenge: Union[bytes, str], track: AnyTrack, drm_type: str) -> bytes:
        if isinstance(challenge, str):
            challenge = challenge.encode("utf-8")

        payload: Dict[str, Any] = {
            "track_id": str(track.id),
            "challenge": base64.b64encode(challenge).decode("ascii"),
            "drm_type": drm_type,
        }

        if track.drm:
            for drm_obj in track.drm:
                drm_class = drm_obj.__class__.__name__
                if drm_type == "playready" and drm_class == "PlayReady":
                    payload["pssh"] = drm_obj.data["pssh_b64"]
                    break
                elif drm_type == "widevine" and drm_class == "Widevine":
                    payload["pssh"] = drm_obj.pssh.dumps()
                    break

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
                self.client.delete(f"/api/session/{self._session_id}")
            except Exception as e:
                self.log.warning(f"Failed to clean up remote session: {e}")
            self._session_id = None

    def _load_cache_files(self) -> Dict[str, str]:
        cache_dir = config.directories.cache / self.service_tag
        if not cache_dir.is_dir():
            return {}
        return {
            f.stem: f.read_text(encoding="utf-8")
            for f in cache_dir.glob("*.json")
            if not f.stem.startswith("titles_")
        }



__all__ = ("RemoteClient", "RemoteService", "resolve_server")
