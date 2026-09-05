"""Remote service adapter for unshackle.

Implements the Service interface by proxying authenticate, get_titles,
get_tracks, get_chapters, and license methods to a remote unshackle server.
Everything else (track selection, download, decrypt, mux) runs locally.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import sys
import time
from datetime import date as date_
from enum import Enum
from http.cookiejar import CookieJar
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Optional, Union

import click
import requests
from langcodes import Language
from requests.adapters import HTTPAdapter, Retry
from rich.padding import Padding
from rich.rule import Rule

from unshackle.core.config import config
from unshackle.core.console import console, prompt_user
from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.titles import Title_T, Titles_T, remap_titles
from unshackle.core.titles.episode import Episode, Series
from unshackle.core.titles.movie import Movie, Movies
from unshackle.core.titles.music import Album, Song
from unshackle.core.tracks import Audio, Chapter, Chapters, Subtitle, Tracks, Video
from unshackle.core.tracks.attachment import Attachment
from unshackle.core.tracks.track import Track
from unshackle.core.utils.redact import redact_text, safe_display_url

log = logging.getLogger("remote_service")

SENSITIVE_DATA_KEYS = ("credential", "credentials", "password", "token", "api_key")

DEFAULT_AUTH_HEADERS = ["X-Secret-Key", "X-Api-Key"]


def redact_secrets(text: str, data: Optional[Dict[str, Any]] = None) -> str:
    """Mask URL userinfo and any request-payload secrets before unshackle writes the text to the log."""
    secrets = [v for k in SENSITIVE_DATA_KEYS if isinstance(v := (data or {}).get(k), str) and v]
    return redact_text(text, secrets) or ""


class RemoteClient:
    """HTTP client for the unshackle serve API."""

    def __init__(self, server_url: str, api_key: str, auth_headers: Optional[list[str]] = None) -> None:
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.auth_headers = list(auth_headers) if auth_headers else list(DEFAULT_AUTH_HEADERS)
        self._auth_header_index = 0
        self._session: Optional[requests.Session] = None

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            from unshackle.core import __version__

            self._session = requests.Session()
            self._session.headers["User-Agent"] = f"unshackle/{__version__}"
            if self.api_key:
                self._session.headers[self.auth_headers[self._auth_header_index]] = self.api_key
        return self._session

    def next_auth_header(self) -> bool:
        """Move the api key onto the next candidate header. False when no candidate header remains."""
        if not self.api_key or self._auth_header_index + 1 >= len(self.auth_headers):
            return False
        session = self.session
        session.headers.pop(self.auth_headers[self._auth_header_index], None)
        self._auth_header_index += 1
        session.headers[self.auth_headers[self._auth_header_index]] = self.api_key
        return True

    def request(
        self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None, optional: bool = False
    ) -> Dict[str, Any]:
        url = f"{self.server_url}{endpoint}"
        while True:
            try:
                resp = getattr(self.session, method)(url, json=data, timeout=120)
            except requests.ConnectionError:
                server_url = safe_display_url(self.server_url)
                log.error(f"Could not connect to remote server at {server_url}. Is it running? (unshackle serve)")
                raise SystemExit(1)
            except requests.Timeout:
                log.error(f"Request to remote server timed out: {endpoint}")
                raise SystemExit(1)
            # servers differ on which header carries the key, so retry the rest before giving up
            if resp.status_code == 401 and self.next_auth_header():
                continue
            break
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except ValueError:
                detail = {}
            error_msg = redact_secrets(str(detail.get("message", resp.text)), data)
            error_code = detail.get("error_code", "UNKNOWN")
            if optional:
                log.debug(f"Optional endpoint {endpoint} unavailable [{error_code}]: {error_msg}")
                return {}
            log.error(f"Server error [{error_code}]: {error_msg}")
            raise SystemExit(1)
        return resp.json()

    def post(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("post", endpoint, data)

    def post_optional(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """POST to an endpoint that an older server may not have. Empty dict when the route is missing."""
        return self.request("post", endpoint, data, optional=True)

    def get(self, endpoint: str) -> Dict[str, Any]:
        return self.request("get", endpoint)

    def get_optional(self, endpoint: str) -> Dict[str, Any]:
        """GET an endpoint that an older server may not have. Empty dict when the route is missing."""
        return self.request("get", endpoint, optional=True)

    def delete(self, endpoint: str) -> Dict[str, Any]:
        return self.request("delete", endpoint)


def enum_get(enum_cls: type[Enum], name: Optional[str], default: Any = None) -> Any:
    """Safely get an enum value by name."""
    if not name:
        return default
    try:
        return enum_cls[name]
    except KeyError:
        return default


def base_track_kwargs(data: Dict[str, Any]) -> Dict[str, Any]:
    """The shared Track constructor kwargs a serialized track carries.

    An older server omits the newer keys, so each one falls back to the value a
    locally-built track would have had.
    """
    return {
        "url": data.get("url") or "https://placeholder",
        "language": Language.get(data.get("language") or "und"),
        "is_original_lang": bool(data.get("is_original_lang", data.get("is_original", False))),
        "descriptor": enum_get(Track.Descriptor, data.get("descriptor"), Track.Descriptor.URL),
        "needs_repack": bool(data.get("needs_repack", False)),
        "name": data.get("name"),
        "edition": data.get("edition") or None,
        "id_": data.get("id"),
    }


def deserialize_bitrate(data: Dict[str, Any]) -> int:
    """Exact bits/s when the server sent it, otherwise the kb/s figure scaled back up."""
    exact = data.get("bitrate_bps")
    if exact:
        return int(exact)
    return data["bitrate"] * 1000 if data.get("bitrate") else 0


def deserialize_video(data: Dict[str, Any]) -> Video:
    return Video(
        **base_track_kwargs(data),
        codec=enum_get(Video.Codec, data.get("codec")),
        range_=enum_get(Video.Range, data.get("range"), Video.Range.SDR),
        bitrate=deserialize_bitrate(data),
        width=data.get("width") or 0,
        height=data.get("height") or 0,
        fps=data.get("fps"),
        scan_type=enum_get(Video.ScanType, data.get("scan_type")),
        closed_captions=data.get("closed_captions") or None,
        dv_compatible_bitstream=bool(data.get("dv_compatible_bitstream", False)),
    )


def deserialize_audio(data: Dict[str, Any]) -> Audio:
    joc = data.get("joc")
    return Audio(
        **base_track_kwargs(data),
        codec=enum_get(Audio.Codec, data.get("codec")),
        bitrate=deserialize_bitrate(data),
        channels=data.get("channels"),
        joc=joc,
        extra={"atmos": True} if joc is None and data.get("atmos") else None,
        descriptive=data.get("descriptive", False),
    )


def deserialize_subtitle(data: Dict[str, Any]) -> Subtitle:
    return Subtitle(
        **base_track_kwargs(data),
        codec=enum_get(Subtitle.Codec, data.get("codec")),
        cc=data.get("cc", False),
        sdh=data.get("sdh", False),
        forced=data.get("forced", False),
    )


def reconstruct_drm(drm_list: Optional[list]) -> list:
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
        # a dropped entry can leave the track with no DRM, so never skip silently
        except Exception as e:
            log.warning(f"Skipping unparseable {drm_type} DRM entry from server: {e!r}")
            continue
    return result


def build_attachment(data: Dict[str, Any]) -> Optional[Attachment]:
    """Rebuild one attachment. A path-only attachment arrives as base64, which this writes to temp."""
    kwargs = {
        "name": data.get("name"),
        "mime_type": data.get("mime_type"),
        "description": data.get("description"),
    }
    if data.get("url"):
        return Attachment(url=data["url"], **kwargs)

    content = data.get("content")
    if not content:
        return None
    try:
        file_name = Path(str(data.get("file_name") or data.get("name") or "attachment")).name
        target = config.directories.temp / "remote_attachments" / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(content))
        return Attachment(path=target, **kwargs)
    except (OSError, ValueError) as e:
        log.warning(f"Skipping attachment {data.get('name')!r} sent by the server: {e}")
        return None


def build_tracks(data: Dict[str, Any]) -> Tracks:
    tracks = Tracks()
    tracks.videos = [deserialize_video(v) for v in data.get("video", [])]
    tracks.audio = [deserialize_audio(a) for a in data.get("audio", [])]
    tracks.subtitles = [deserialize_subtitle(s) for s in data.get("subtitles", [])]

    for track_data, track_obj in [
        *zip(data.get("video", []), tracks.videos),
        *zip(data.get("audio", []), tracks.audio),
        *zip(data.get("subtitles", []), tracks.subtitles),
    ]:
        drm_objs = reconstruct_drm(track_data.get("drm"))
        if drm_objs:
            track_obj.drm = drm_objs
        preference = track_data.get("drm_preference")
        if preference:
            try:
                track_obj.drm_preference = preference
            except ValueError:
                log.warning(f"Ignoring unknown drm_preference {preference!r} from the server for track {track_obj.id}")
    tracks.attachments = [a for a in map(build_attachment, data.get("attachments", [])) if a]
    return tracks


def apply_service_track_data(tracks: Tracks, data: Dict[str, Any]) -> None:
    """Overlay the service-set ``track.data`` the server sent.

    Runs after the manifest re-parse so a service value wins over the parser's own for
    the same key, as it does on the server.
    """
    by_id = {str(t.id): t for t in list(tracks.videos) + list(tracks.audio) + list(tracks.subtitles)}
    for key in ("video", "audio", "subtitles"):
        for entry in data.get(key, []):
            track = by_id.get(str(entry.get("id")))
            extra = entry.get("data")
            if track is not None and isinstance(extra, dict):
                track.data.update(extra)


def resolve_track_manifests(tracks: Tracks, track_manifests: list) -> None:
    """Fill track.data from the per-track MPD fragments the server sent.

    A service can build an AdaptationSet the served manifest does not contain, so the
    client's re-parse of that manifest cannot find it. The server sends a one-AdaptationSet
    MPD for each of those tracks instead. This runs first, and the whole-manifest re-parse
    then skips every track whose ``data`` already holds a ``dash`` entry.
    """
    import base64 as b64

    from lxml import etree

    from unshackle.core.api.compression import safe_inflate

    if not track_manifests:
        return

    log_m = logging.getLogger("remote_service")
    by_id = {str(t.id): t for t in list(tracks.videos) + list(tracks.audio) + list(tracks.subtitles)}

    for entry in track_manifests:
        track = by_id.get(str(entry.get("track_id")))
        if track is None or entry.get("type") != "dash" or not entry.get("data"):
            continue
        try:
            manifest = etree.fromstring(safe_inflate(b64.b64decode(entry["data"])))
            period = manifest.find("Period")
            adaptation_set = period.find("AdaptationSet")
            representation = adaptation_set.find("Representation")
            if representation is None:
                raise ValueError("fragment has no Representation")
        except Exception as e:
            log_m.warning("Failed to re-parse the track fragment for track %s: %s", track.id, e)
            continue

        track.data["dash"] = {
            "manifest": manifest,
            "period": period,
            "adaptation_set": adaptation_set,
            "representation": representation,
            "representation_id": representation.get("id"),
        }
        track.descriptor = Track.Descriptor.DASH


def resolve_manifest_data(tracks: Tracks, manifests: list) -> None:
    """Re-parse serialized manifests and fill track.data for downloading.

    The server serializes DASH and ISM manifest XML as zlib-compressed base64.
    We decode and decompress locally, re-parse with the appropriate manifest
    parser, then match each remote track to the locally-parsed track by ID
    to copy track.data. We skip HLS, because it re-fetches from track.url.
    """
    import base64 as b64

    from unshackle.core.api.compression import safe_inflate

    if not manifests:
        return

    log_m = logging.getLogger("remote_service")
    all_tracks = list(tracks.videos) + list(tracks.audio) + list(tracks.subtitles)
    local_all: list = []

    for manifest_info in manifests:
        m_type = manifest_info.get("type")
        m_url = manifest_info.get("url")
        m_data = manifest_info.get("data")
        if not m_data or not m_url:
            continue

        try:
            raw = safe_inflate(b64.b64decode(m_data))

            fallback_lang = next(
                (t.language for t in all_tracks if t.language and str(t.language) != "und"),
                None,
            )
            if m_type == "dash":
                from lxml import etree

                from unshackle.core.manifests import DASH

                local_tracks = DASH(etree.fromstring(raw), m_url).to_tracks(language=fallback_lang)
            elif m_type == "ism":
                from lxml import etree

                from unshackle.core.manifests import ISM

                local_tracks = ISM(etree.fromstring(raw), m_url).to_tracks(language=fallback_lang)
            else:
                continue

            local_all += list(local_tracks.videos) + list(local_tracks.audio) + list(local_tracks.subtitles)
        except Exception as e:
            log_m.warning("Failed to re-parse %s manifest from %s: %s", m_type, m_url, e)

    for remote_track in all_tracks:
        if any(remote_track.data.get(key) for key in ("dash", "ism")):
            continue
        matched = match_track(remote_track, local_all)
        if matched and any(matched.data.get(key) for key in ("dash", "ism")):
            remote_track.data.update(matched.data)
            remote_track.descriptor = matched.descriptor
            if matched.drm and not remote_track.drm:
                remote_track.drm = matched.drm


def same_bitrate(local: Optional[int], remote: Optional[int]) -> bool:
    """Equal to the nearest kb/s. A server that predates ``bitrate_bps`` rounds to kb/s in transit."""
    if not local or not remote:
        return not local and not remote
    return round(local / 1000) == round(remote / 1000)


def match_track(remote_track: Track, local_tracks: list) -> Optional[Track]:
    """Find the locally-parsed track that matches a remote track by ID or by attributes."""
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
            if (
                lt.width == remote_track.width
                and lt.height == remote_track.height
                and lt.range == remote_track.range
                and same_bitrate(lt.bitrate, remote_track.bitrate)
            ):
                return lt
        elif hasattr(lt, "channels") and hasattr(remote_track, "channels"):
            if same_bitrate(lt.bitrate, remote_track.bitrate):
                return lt
        elif hasattr(lt, "forced"):
            if lt.forced == remote_track.forced and lt.sdh == remote_track.sdh:
                return lt
    return None


EPISODE_PLACEHOLDER_NAME = re.compile(r"Episode ?#?\d+", re.IGNORECASE)


def episode_name(name: Optional[str], series_title: Optional[str]) -> Optional[str]:
    """Drop a placeholder episode name the way ``Episode.__init__`` does.

    The server sends "Episode 04" for an unnamed episode, so the same filter has to run
    again here, or the placeholder reaches the output file name.
    """
    if not name:
        return None
    name = name.strip()
    if EPISODE_PLACEHOLDER_NAME.match(name):
        return None
    if series_title and name.lower() == series_title.lower():
        return None
    return name


def parse_air_date(value: Any) -> Any:
    """ISO date string back to a date, as ``Episode.__init__`` does; anything else passes through."""
    if isinstance(value, str):
        try:
            return date_.fromisoformat(value[:10])
        except ValueError:
            return value
    return value


def apply_title_fields(title: Title_T, info: Dict[str, Any]) -> None:
    """Copy every serialized title field onto a local title.

    A service can change its title inside ``get_tracks`` (a corrected original language,
    a real episode name, an air date), so the client re-applies the server's view of the
    title after every call that returns one.
    """
    if info.get("language"):
        title.language = Language.get(info["language"])
    if info.get("description") is not None and isinstance(title, (Episode, Movie)):
        title.description = info["description"]
    if info.get("year") is not None:
        title.year = info["year"]

    if isinstance(title, Episode):
        if info.get("series_title"):
            title.title = info["series_title"]
        if "name" in info:
            title.name = episode_name(info["name"], title.title)
        for key in ("season", "number", "part", "absolute"):
            if info.get(key) is not None:
                setattr(title, key, info[key])
        if info.get("air_date") is not None:
            title.air_date = parse_air_date(info["air_date"])
    elif info.get("name"):
        title.name = info["name"]

    for key in ("anime", "daily"):
        if info.get(key) is not None:
            setattr(title, key, info[key])

    extra = {k: info[k] for k in ("date", "cover_url") if info.get(k) is not None}
    if extra:
        if isinstance(title.data, dict):
            title.data.update(extra)
        else:
            title.data = extra


def build_title(info: Dict[str, Any], service_tag: str, fallback_id: str) -> Title_T:
    svc_class = type(service_tag, (), {})
    lang = Language.get(info["language"]) if info.get("language") else None
    title_type = info.get("type")
    title: Title_T
    if title_type == "episode":
        title = Episode(
            id_=info.get("id", fallback_id),
            service=svc_class,
            title=info.get("series_title", "Unknown"),
            season=info.get("season", 0),
            number=info.get("number", 0),
            language=lang,
        )
    elif title_type == "movie":
        title = Movie(
            id_=info.get("id", fallback_id),
            service=svc_class,
            name=info.get("name") or "Unknown",
            language=lang,
        )
    elif title_type == "song":
        title = Song(
            id_=info.get("id", fallback_id),
            service=svc_class,
            name=info.get("name") or "Unknown",
            artist=info.get("artist") or "Unknown",
            album=info.get("album") or "Unknown",
            track=info.get("track") or 1,
            disc=info.get("disc") or 1,
            language=lang,
            album_artist=info.get("album_artist"),
            release_type=info.get("release_type") or "album",
            total_tracks=info.get("total_tracks"),
            total_discs=info.get("total_discs"),
            genre=info.get("genre"),
            explicit=info.get("explicit"),
            isrc=info.get("isrc"),
            upc=info.get("upc"),
            copyright=info.get("copyright"),
            label=info.get("label"),
            lyrics=info.get("lyrics"),
            artwork_url=info.get("artwork_url"),
        )
    else:
        raise click.ClickException(
            f"The remote server sent a title of type {title_type!r}, which this client does not support. "
            "Update unshackle on both ends so they agree on the title types."
        )
    apply_title_fields(title, info)
    return title


def resolve_auth_headers(svc: dict, server_name: str) -> list[str]:
    """Configured header names first, then the defaults as fallbacks."""
    auth_headers = svc.get("auth_headers")
    if auth_headers is None:
        return list(DEFAULT_AUTH_HEADERS)
    if (
        not isinstance(auth_headers, list)
        or not auth_headers
        or not all(isinstance(h, str) and h.strip() for h in auth_headers)
    ):
        raise click.ClickException(
            f"Remote service '{server_name}': 'auth_headers' must be a list of header names, e.g.\n\n"
            '      auth_headers: ["Authorization"]'
        )
    resolved = [h.strip() for h in auth_headers]
    seen = {h.lower() for h in resolved}
    resolved.extend(h for h in DEFAULT_AUTH_HEADERS if h.lower() not in seen)
    return resolved


ANNOUNCED_SERVERS: set[str] = set()


def resolve_server(server_name: Optional[str]) -> tuple[str, str, dict]:
    """Find the server URL, the API key, and the per-service config in remote_services.

    The per-service config carries the ``_auth_headers`` candidate list for RemoteClient.
    """
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
        services["_server_cdm"] = svc.get("server_cdm")
        services["_auth_headers"] = resolve_auth_headers(svc, server_name)
        return svc["url"], svc.get("api_key", ""), services

    if len(remote_services) == 1:
        name, svc = next(iter(remote_services.items()))
        if name not in ANNOUNCED_SERVERS:
            ANNOUNCED_SERVERS.add(name)
            log.info(f"Using remote service: {name}")
        services = svc.get("services", {})
        services["_server_cdm"] = svc.get("server_cdm")
        services["_auth_headers"] = resolve_auth_headers(svc, name)
        return svc["url"], svc.get("api_key", ""), services

    available = ", ".join(remote_services.keys())
    raise click.ClickException(f"Multiple remote services configured. Use --server to select one: {available}")


def load_credentials_for_transport(service_tag: str, profile: Optional[str]) -> Optional[Dict[str, str]]:
    from unshackle.commands.dl import dl

    credential = dl.get_credentials(service_tag, profile)
    if credential:
        result: Dict[str, str] = {"username": credential.username, "password": credential.password}
        if credential.extra:
            result["extra"] = credential.extra
        return result
    return None


def load_cookies_for_transport(service_tag: str, profile: Optional[str]) -> Optional[str]:
    import zlib

    from unshackle.commands.dl import dl

    cookie_path = dl.get_cookie_path(service_tag, profile)
    if cookie_path and cookie_path.exists():
        return base64.b64encode(zlib.compress(cookie_path.read_bytes())).decode("ascii")
    return None


def resolve_proxy_arg(proxy_arg: Optional[str]) -> Optional[str]:
    if not proxy_arg:
        return None

    from unshackle.core.proxies.resolve import initialize_proxy_providers, resolve_proxy

    try:
        providers = initialize_proxy_providers()
        return resolve_proxy(proxy_arg, providers)
    except ValueError as e:
        raise click.ClickException(str(e))


_CACHE_HEX_ID_RE = re.compile(r"[0-9a-f]{32,}", re.IGNORECASE)


def credential_cache_digests(credential: Credential) -> set[str]:
    """Hex digests a service may embed in cache filenames for this credential."""
    digests = set()
    for text in (credential.dumps(), credential.username, credential.password):
        for algo in ("md5", "sha1", "sha256"):
            digests.add(hashlib.new(algo, text.encode()).hexdigest())
    return digests


def _contains_name(stem: str, name: str) -> bool:
    """True when the stem contains the profile name as a separator-delimited run.

    Both sides casefold and normalise separators, so a profile named "us-east"
    matches "tokens_us-east" and "tokens_US_EAST".
    """
    norm = re.sub(r"[._-]", "_", stem.casefold())
    return f"_{re.sub(r'[._-]', '_', name.casefold())}_" in f"_{norm}_"


def cache_stem_is_relevant(
    stem: str, allowed_digests: set[str], active_profile: str, foreign_profiles: set[str]
) -> bool:
    """True when a cache filename provably belongs to the active remote session.

    A stem fails the check when it embeds a hex digest that the active credential
    does not produce, or another configured profile's name. A digest or profile
    name that matches the active credential takes priority over a foreign profile
    name, because region codes in cache filenames can collide with profile names.
    The exception is a longer foreign name that also matches, since the active
    name is then likely a prefix of it. A stem with no identity marker at all holds
    service-global state, so it passes. The check does not detect a digest cut
    to fewer than 32 characters.
    """
    digests = [d.lower() for d in _CACHE_HEX_ID_RE.findall(stem)]
    if any(d not in allowed_digests for d in digests):
        return False
    if digests:
        return True
    matched_foreign = [n for n in foreign_profiles if _contains_name(stem, n)]
    if _contains_name(stem, active_profile):
        return not any(len(n) > len(active_profile) for n in matched_foreign)
    return not matched_foreign


class RemoteService:
    """Service adapter that proxies to a remote unshackle server.

    Implements the same interface dl.py's result() expects without
    subclassing Service (avoids proxy/geofence setup in __init__).
    """

    ALIASES: tuple[str, ...] = ()
    GEOFENCE: tuple[str, ...] = ()
    NO_SUBTITLES: bool = False
    ANIME: bool = False
    DAILY: bool = False

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
        self.client = RemoteClient(server_url, api_key, services_config.get("_auth_headers"))
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
        self._segment_filters: Dict[str, tuple[set[str], set[str]]] = {}
        self._log_seq = 0
        self._log_drain_lock = Lock()

        self._session = requests.Session()
        self._session.headers.update(config.headers)
        self._session.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(total=5, backoff_factor=0.2, status_forcelist=[429, 500, 502, 503, 504]),
                pool_maxsize=64,
                pool_block=True,
            ),
        )
        self._session.mount("http://", self._session.adapters["https://"])

        svc_config = services_config.get(service_tag, {})
        self._server_cdm: bool | None = services_config.get("_server_cdm")
        self._server_accounts: Optional[dict] = services_config.get("_server_accounts")
        self.apply_service_config(svc_config)

    def apply_service_config(self, svc_config: dict) -> None:
        if not svc_config:
            return
        config_maps = {
            "cdm": ("cdm", self.service_tag),
            "decryption": ("decryption_map", self.service_tag),
        }
        for key, (attr, tag) in config_maps.items():
            if svc_config.get(key):
                target = getattr(config, attr, None)
                if target is None:
                    setattr(config, attr, {})
                    target = getattr(config, attr)
                target[tag] = svc_config[key]

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

        if self._server_accounts is None:
            credentials = load_credentials_for_transport(self.service_tag, profile)
            if credentials:
                create_data["credentials"] = credentials

            cookies_text = load_cookies_for_transport(self.service_tag, profile)
            if cookies_text:
                create_data["cookies"] = cookies_text
        else:
            self.credential = None
            self.log.info(
                "The server manages the account for this service" + (" and ignores --profile" if profile else "")
            )

        client_region = None
        if not no_proxy:
            try:
                from unshackle.core.utils.ip_info import get_ip_info

                ip_info = get_ip_info(self._session, cached=True)
                if ip_info and ip_info.get("country"):
                    client_region = ip_info["country"].lower()
            except Exception:
                pass

        if not no_proxy and not proxy and self._server_accounts and not self._server_accounts.get("global"):
            regions = [str(r).lower() for r in self._server_accounts.get("regions") or []]
            if regions and client_region and client_region not in regions:
                try:
                    create_data["proxy"] = resolve_proxy_arg(regions[0])
                    create_data["proxy_region"] = regions[0]
                    self.log.info(f"Using a '{regions[0]}' proxy to match a server account ({', '.join(regions)})")
                except click.ClickException:
                    self.log.warning(
                        f"The server's accounts work in {', '.join(regions)} and you are in {client_region}. "
                        "There is no local proxy for that region. Pass --proxy yourself if the server "
                        "rejects the remote session"
                    )
        elif not no_proxy and proxy:
            resolved_proxy = resolve_proxy_arg(proxy)
            if resolved_proxy:
                create_data["proxy"] = resolved_proxy
                query = (self.ctx.parent.params.get("proxy_query") if self.ctx.parent else None) or proxy
                country = re.match(r"^(?:[a-z]+:)?([a-z]{2})\d*$", query, re.IGNORECASE)
                if country:
                    create_data["proxy_region"] = country.group(1).lower()
        if not no_proxy and not proxy and client_region:
            create_data["client_region"] = client_region
        elif not no_proxy and not proxy and self._server_accounts and not self._server_accounts.get("global"):
            self.log.warning("Could not detect your region; pass --proxy <country> so the server can pick an account")

        if profile:
            create_data["profile"] = profile
        if no_proxy:
            create_data["no_proxy"] = True
        # Forward track selection params so the server fetches the right manifests
        if self.ctx.parent:
            range_ = self.ctx.parent.params.get("range_")
            if range_:
                create_data["range_"] = [r.name for r in range_]
            vcodec = self.ctx.parent.params.get("vcodec")
            if vcodec:
                create_data["vcodec"] = [c.name for c in vcodec]
            quality = self.ctx.parent.params.get("quality")
            if quality:
                create_data["quality"] = list(quality)
            if self.ctx.parent.params.get("best_available"):
                create_data["best_available"] = True

        if self._service_params:
            create_data.update(self._service_params)
            create_data["service_params"] = self._service_params

        cdm = self.ctx.obj.cdm if self.ctx.obj else None
        if cdm is not None:
            from unshackle.core.cdm.detect import is_playready_cdm

            create_data["cdm_type"] = "playready" if is_playready_cdm(cdm) else "widevine"

        cache_data = self.load_cache_files(profile) if self._server_accounts is None else None
        if cache_data:
            create_data["cache"] = cache_data

        from unshackle.core import __version__

        create_data["client"] = {"version": __version__, "platform": sys.platform}

        result = self.client.post("/api/session/create", create_data)
        self._session_id = result["session_id"]

        status = result.get("status", "authenticated")
        if status == "authenticating":
            self.poll_auth_completion()
        self.drain_server_logs()

    def drain_server_logs(self) -> None:
        """Fetch the service's server-side log records for this remote session and re-emit them locally.

        The server mirrors every ``self.log`` call the service makes into a
        per-session buffer. Draining after each remote call shows the client
        why a step failed. A relay failure must never break the operation
        itself, so this method swallows every error.
        """
        if not self._session_id:
            return
        with self._log_drain_lock:
            try:
                resp = self.client.get_optional(f"/api/session/{self._session_id}/logs?since={self._log_seq}")
            except (Exception, SystemExit):
                return
            if not resp:
                return
            level_names = logging.getLevelNamesMapping()
            for record in resp.get("logs", []):
                level = level_names.get(str(record.get("level", "INFO")), logging.INFO)
                self.log.log(level, str(record.get("message", "")))
            self._log_seq = int(resp.get("last_seq", self._log_seq))

    def poll_auth_completion(self, poll_interval: float = 2.0, timeout: float = 600.0) -> None:
        """Poll the server until authentication completes, handling interactive prompts.

        When the server needs user input (OTP, device code, PIN), it returns
        ``pending_input`` with a prompt. We display it locally, collect the
        response, and POST it back. The server resumes its auth flow.
        """
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            resp = self.client.get(f"/api/session/{self._session_id}/prompt")
            status = resp.get("status")
            self.drain_server_logs()

            if status == "authenticated":
                return

            if status == "failed":
                error = resp.get("error", "Authentication failed on server")
                log.error(f"Remote auth failed: {error}")
                raise SystemExit(1)

            if status == "pending_input":
                prompt = resp.get("prompt", "Enter input: ")
                try:
                    user_response = prompt_user(prompt)
                except EOFError:
                    log.error("Remote auth needs interactive input but stdin is closed")
                    raise SystemExit(1) from None
                self.client.post(
                    f"/api/session/{self._session_id}/prompt",
                    {"response": user_response},
                )
                continue

            time.sleep(poll_interval)

        log.error("Remote authentication timed out")
        raise SystemExit(1)

    def get_titles(self) -> Titles_T:
        if self._titles is not None:
            return self._titles
        try:
            result = self.client.get(f"/api/session/{self._session_id}/titles")
        finally:
            self.drain_server_logs()
        titles_list = [build_title(t, self.service_tag, self.title_id) for t in result.get("titles", [])]
        first = titles_list[0] if titles_list else None
        if isinstance(first, Episode):
            self._titles = Series(titles_list)
        elif isinstance(first, Song):
            self._titles = Album(titles_list)
        else:
            self._titles = Movies(titles_list)
        return self._titles

    def get_titles_cached(self, title_id: str = None) -> Titles_T:
        """Apply the client's local title_map to titles fetched from the remote server.

        Lets users rename titles for remote services they do not have installed locally.
        The server sends raw titles. The client's own ``services.<TAG>.title_map`` wins.
        """
        title_map = (config.services.get(self.service_tag) or {}).get("title_map") or {}
        return remap_titles(self.get_titles(), title_map)

    def get_tracks(self, title: Title_T) -> Tracks:
        title_id = str(title.id)
        if title_id in self._tracks_by_title:
            return self._tracks_by_title[title_id]
        try:
            result = self.client.post(f"/api/session/{self._session_id}/tracks", {"title_id": title_id})
        finally:
            self.drain_server_logs()
        tracks = build_tracks(result)

        remote_title = result.get("title") or {}
        if remote_title:
            apply_title_fields(title, remote_title)

        for k, v in result.get("session_headers", {}).items():
            if k.lower() not in ("host", "content-length", "content-type"):
                self._session.headers[k] = v
        for k, v in result.get("session_cookies", {}).items():
            self._session.cookies.set(k, v)

        resolve_track_manifests(tracks, result.get("track_manifests", []))
        resolve_manifest_data(tracks, result.get("manifests", []))
        apply_service_track_data(tracks, result)

        server_cdm = bool(result.get("server_cdm", False))
        if self._server_cdm is None:
            self._server_cdm = server_cdm
        elif self._server_cdm and not server_cdm:
            self._server_cdm = False
            self.log.warning(
                f"{self.service_tag} is not available for server CDM licensing with this API key, "
                "falling back to the local CDM"
            )
        self._server_cdm_type = result.get("server_cdm_type", "widevine")

        for track in [*tracks.videos, *tracks.audio, *tracks.subtitles]:
            if track.descriptor == Track.Descriptor.HLS:
                track.OnSegmentFilter = self.remote_segment_filter(str(track.id))

        self._tracks_by_title[title_id] = tracks
        self._chapters_by_title[title_id] = result.get("chapters", [])

        return tracks

    def remote_segment_filter(self, track_id: str) -> Callable[[Any], bool]:
        """The server's ``OnSegmentFilter`` result for one HLS track, fetched on first use.

        The service keeps its filter; the client gets only the segment URIs to skip.
        A server without the route returns nothing, so the client keeps every segment.
        """

        def segment_filter(segment: Any) -> bool:
            unwanted = self._segment_filters.get(track_id)
            if unwanted is None:
                result = self.client.post_optional(
                    f"/api/session/{self._session_id}/segment_filter", {"track_id": track_id}
                )
                uris = [str(u) for u in (result.get("unwanted") or [])]
                unwanted = (set(uris), {u.split("?")[0] for u in uris})
                self._segment_filters[track_id] = unwanted
            exact, paths = unwanted
            if not exact:
                return False
            uri = str(getattr(segment, "absolute_uri", "") or "")
            return uri in exact or uri.split("?")[0] in paths

        return segment_filter

    def resolve_server_keys(self, title: Title_T) -> None:
        """Get the DRM content keys through the server CDM for all tracks on a title.

        Called by dl.py between track selection and download. The server
        decides which CDM device to use and tells the client through
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
            self.drain_server_logs()
            keys_by_track = resp.get("keys", {})
            server_drm_type = resp.get("drm_type", drm_type)
            drm_types_by_track = resp.get("drm_types", {})
            self._server_cdm_type = server_drm_type
            self.log.debug(f"Server responded with drm_type={server_drm_type}, keys for {len(keys_by_track)} track(s)")

            for track in title.tracks:
                track_keys = keys_by_track.get(str(track.id), {})
                if not track_keys:
                    if str(track.id) in track_ids:
                        self.log.warning(f"Server CDM returned no content keys for track {track.id}")
                    continue

                kid_list = list(track_keys.keys())
                track_drm_type = drm_types_by_track.get(str(track.id), server_drm_type)
                drm_obj = self.create_drm_stub(track_drm_type, kid_list)
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
    def create_drm_stub(drm_type: str, kid_hexes: list[str]) -> Any:
        """Make a DRM object stub that matches the type the server used.

        For server_cdm mode, unshackle uses this stub for display only, because it
        already has the content keys. We assemble a minimal DRM object that holds
        content_keys.
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
        return self.proxy_license(challenge, track, "widevine")

    def get_playready_license(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Optional[Union[bytes, str]]:
        return self.proxy_license(challenge, track, "playready")

    def get_clearkey_license(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Optional[Union[bytes, str, dict]]:
        return None

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
        # a missing certificate is legal (services may not use one); log and continue without
        except Exception as e:
            self.log.debug(f"Service certificate fetch failed: {e!r}")
            return None

    def proxy_license(self, challenge: Union[bytes, str], track: AnyTrack, drm_type: str) -> bytes:
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
            else:
                self.log.warning(f"Track {track.id} has no {drm_type} PSSH to send to the server CDM")
            return challenge

        payload = {
            "track_id": str(track.id),
            "challenge": base64.b64encode(challenge).decode("ascii"),
            "drm_type": drm_type,
        }
        if pssh_b64:
            payload["pssh"] = pssh_b64

        try:
            resp = self.client.post(f"/api/session/{self._session_id}/license", payload)
        finally:
            self.drain_server_logs()
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
                self.save_returned_cache(result.get("cache", {}))
            except Exception as e:
                self.log.warning(f"Failed to clean up remote session: {e}")
            self._session_id = None

    def save_returned_cache(self, cache_data: Dict[str, str]) -> None:
        """Save cache files returned by the server to the local cache directory.

        The server returns updated cache files (e.g. refreshed tokens) on
        session close. Writing them locally means the next remote session
        can forward them back, skipping interactive auth.
        """
        if not cache_data:
            return

        from unshackle.core.api.compression import safe_inflate
        from unshackle.core.api.sanitize import safe_cache_key

        cache_dir = config.directories.cache / self.service_tag
        cache_dir.mkdir(parents=True, exist_ok=True)

        for key, content in cache_data.items():
            safe_name = safe_cache_key(key)
            if not safe_name:
                self.log.warning(f"Rejecting unsafe cache filename from server: {key!r}")
                continue
            try:
                decompressed = safe_inflate(base64.b64decode(content))
                (cache_dir / safe_name).with_suffix(".json").write_bytes(decompressed)
            except Exception as e:
                self.log.warning(f"Failed to save returned cache file '{safe_name}': {e}")

        self.log.info(f"Saved {len(cache_data)} cache file(s) from server")

    def load_cache_files(self, profile: Optional[str] = None) -> Dict[str, str]:
        """Collect the cache files to forward, and withhold other profiles' files.

        The client cannot rely on the server to filter, so it sends only the files
        it can tie to the active credential or profile, plus service-global state.
        At worst, a withheld file makes the server authenticate again.
        """
        import zlib

        from unshackle.commands.dl import dl

        cache_dir = config.directories.cache / self.service_tag
        if not cache_dir.is_dir():
            return {}

        credential = dl.get_credentials(self.service_tag, profile)
        allowed = credential_cache_digests(credential) if credential else set()
        active = profile or "default"
        profiles = config.credentials.get(self.service_tag)
        if isinstance(profiles, dict) and profile and profile not in profiles:
            active = "default"
        foreign = set(profiles) - {active} if isinstance(profiles, dict) else set()

        files: Dict[str, str] = {}
        for f in cache_dir.glob("*.json"):
            if f.stem.startswith("titles_"):
                continue
            if not cache_stem_is_relevant(f.stem, allowed, active, foreign):
                self.log.debug(f"Withholding cache file from the remote server: {f.stem}")
                continue
            files[f.stem] = base64.b64encode(zlib.compress(f.read_bytes())).decode("ascii")
        return files


__all__ = ("RemoteClient", "RemoteService", "resolve_server")
