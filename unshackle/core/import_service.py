from __future__ import annotations

import json
import logging
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Optional, Union
from uuid import UUID

import click
import requests
from langcodes import tag_is_valid

from unshackle.core.config import config
from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.drm import drm_from_dict
from unshackle.core.manifests import DASH, HLS, ISM
from unshackle.core.remote_service import RemoteService, build_title, match_track, resolve_proxy_arg
from unshackle.core.titles import Episode, Movies, Series, Title_T, Titles_T, remap_titles
from unshackle.core.tracks import Audio, Chapter, Chapters, Tracks, Video
from unshackle.core.tracks.attachment import Attachment
from unshackle.core.tracks.track import Track

log = logging.getLogger("import")

PARSERS = {"DASH": DASH, "HLS": HLS, "ISM": ISM}
MANIFEST_DATA_KEYS = {"DASH": "dash", "ISM": "ism"}


def fallback_language(language: Any) -> Optional[Any]:
    """The title language if the parsers would accept it, else None so they raise.

    DASH drops an und/invalid fallback but ISM does not, and a truthy 'und' there silently
    labels every track und instead of failing.
    """
    tag = str(language or "").strip()
    if not tag or not tag_is_valid(tag) or tag.startswith("und"):
        return None
    return language


def resolve_import_manifest_data(
    tracks: Tracks,
    manifest_type: Optional[str],
    *,
    session: requests.Session,
    language: Any,
    title: Title_T,
) -> None:
    """Fill ``track.data`` for DASH/ISM tracks rebuilt from export dicts.

    Exports can be without ``manifest_url`` (for example, when the service never set
    ``title.tracks.manifest_url``) or carry one MPD per adaptation set. ``Track.from_dict``
    does not serialise manifest XML, so each exported track's ``url`` is re-fetched and
    matched to a locally parsed representation before download.
    """
    if manifest_type not in MANIFEST_DATA_KEYS:
        return

    parser = PARSERS[manifest_type]
    data_key = MANIFEST_DATA_KEYS[manifest_type]
    pending = [
        track
        for track in [*tracks.videos, *tracks.audio, *tracks.subtitles]
        if track.descriptor.name == manifest_type and not track.data.get(data_key)
    ]
    if not pending:
        return

    fallback_lang = fallback_language(language)
    for url in {str(track.url) for track in pending if track.url}:
        try:
            manifest = parser.from_url(url=url, session=session)
            parsed = manifest.to_tracks(language=fallback_lang)
        except ValueError as e:
            if "Language information could not be derived" in str(e):
                raise click.ClickException(
                    f"No language for '{title}': the {manifest_type} manifest has none and the "
                    f"service did not set Title.language."
                )
            raise click.ClickException(f"Failed to parse the {manifest_type} manifest for '{title}'. ({e})")
        except Exception as e:
            raise click.ClickException(
                f"Failed to fetch the {manifest_type} manifest for '{title}'. "
                f"The manifest URL may have expired since export. ({e})"
            )

        local_tracks = [*parsed.videos, *parsed.audio, *parsed.subtitles]
        for track in pending:
            if track.data.get(data_key) or str(track.url) != url:
                continue
            matched = match_track(track, local_tracks)
            if matched and matched.data.get(data_key):
                track.data.update(matched.data)


class ImportService:
    """Reconstructs a download from an export JSON.

    ImportService does not authenticate and does not license. It rebuilds the tracks from
    the export and injects the keys directly. ``_server_cdm``/``_server_cdm_type`` keep their
    underscores: dl.py reads them through getattr as the server-CDM contract that skips
    client licensing.
    """

    ALIASES: tuple[str, ...] = ()
    GEOFENCE: tuple[str, ...] = ()
    NO_SUBTITLES: bool = False

    def __init__(self, ctx: click.Context, service_tag: str, title: str, import_file: Optional[str]) -> None:
        self.__class__.__name__ = service_tag
        self.service_tag = service_tag
        self.title_id = title
        self.ctx = ctx
        self.log = logging.getLogger(service_tag)
        self.credential: Optional[Credential] = None
        self.current_region: Optional[str] = None
        self.title_cache = None

        if not import_file:
            raise click.ClickException("No export file was provided to import from.")
        export_path = Path(import_file)
        if not export_path.is_file():
            raise click.ClickException(f"Export file not found: {export_path}")

        self.data: dict[str, Any] = json.loads(export_path.read_text(encoding="utf8"))
        version = self.data.get("version")
        if version != 2:
            raise click.ClickException(
                f"Unsupported export version {version!r}. Re-create the export with a current build."
            )

        self.titles_data: dict[str, Any] = self.data.get("titles", {})
        self.region: Optional[str] = self.data.get("region")
        self.titles: Optional[Titles_T] = None
        self.tracks_by_title: dict[str, Tracks] = {}

        self._server_cdm = True
        self._server_cdm_type = "widevine"

        self.session = self.build_session(ctx, self.region)

    @staticmethod
    def build_session(ctx: click.Context, region: Optional[str] = None) -> requests.Session:
        """HTTP session for re-fetching the manifest.

        Honours the importer's ``--proxy``. Without one, it falls back to the export region
        as a geofence. An explicit proxy that unshackle cannot find raises an error. A region
        fallback gives a warning.
        """
        session = requests.Session()
        session.headers.update(config.headers)

        params = ctx.parent.params if ctx.parent else {}
        if params.get("no_proxy"):
            return session

        explicit = params.get("proxy")
        proxy_query = explicit or region
        if not proxy_query:
            return session

        try:
            proxy = resolve_proxy_arg(proxy_query)
        except Exception as e:
            if explicit:
                raise click.ClickException(f"Failed to resolve proxy '{proxy_query}': {e}")
            log.warning(f"Could not auto-select a proxy for export region '{region}': {e}. Continuing without proxy.")
            proxy = None

        if proxy:
            session.proxies.update({"all": proxy})
            if not explicit:
                log.info(f"No --proxy given; using export region '{region}' via your proxy provider.")
        return session

    @property
    def title(self) -> str:
        return self.title_id

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        self.credential = credential

    def get_titles(self) -> Titles_T:
        if self.titles is not None:
            return self.titles
        titles_list = [
            build_title(entry.get("meta", {}), self.service_tag, fallback_id=title_id)
            for title_id, entry in self.titles_data.items()
        ]
        self.titles = (
            Series(titles_list) if titles_list and isinstance(titles_list[0], Episode) else Movies(titles_list)
        )
        return self.titles

    def get_titles_cached(self, title_id: Optional[str] = None) -> Titles_T:
        """Apply the service's title_map to titles reconstructed from the export sidecar."""
        title_map = (config.services.get(self.service_tag) or {}).get("title_map") or {}
        return remap_titles(self.get_titles(), title_map)

    def get_tracks(self, title: Title_T) -> Tracks:
        """Reconstruct the title's tracks from the export.

        DASH/ISM: re-fetch and re-parse ``manifest_url`` for the full ladder on that MPD (the importer
        picks quality with normal dl flags, and injects the keys by KID later), then merge in exported
        DASH/ISM tracks from other MPDs (e.g. a separate HEVC manifest) and direct-URL side-loads.
        A service side-loads its own subtitles when the manifest's are the worse copy, so any
        direct-URL subtitle in the export means unshackle drops the whole re-parsed subtitle
        set in favour of the exported one.
        HLS/URL: rebuild from the stored per-track dicts, since the variant is re-fetched from
        track.url at download time and a master playlist can hand out a new token on every fetch.
        """
        title_id = str(title.id)
        if title_id in self.tracks_by_title:
            return self.tracks_by_title[title_id]

        entry = self.titles_data.get(title_id, {})
        tracks_map: dict[str, Any] = entry.get("tracks") or {}
        manifest_url = entry.get("manifest_url")
        manifest_type = entry.get("manifest_type")

        tracks = Tracks()
        tracks.manifest_url = manifest_url

        parser = PARSERS.get(manifest_type or "")
        if manifest_url and parser is not None and manifest_type in ("DASH", "ISM"):
            try:
                manifest = parser.from_url(url=manifest_url, session=self.session)
            except Exception as e:
                raise click.ClickException(
                    f"Failed to fetch the {manifest_type} manifest for '{title}'. "
                    f"The manifest URL may have expired since export. ({e})"
                )
            try:
                parsed = manifest.to_tracks(language=fallback_language(title.language))
            except ValueError as e:
                if "Language information could not be derived" in str(e):
                    raise click.ClickException(
                        f"No language for '{title}': the {manifest_type} manifest has none and the "
                        f"service did not set Title.language."
                    )
                raise click.ClickException(f"Failed to parse the {manifest_type} manifest for '{title}'. ({e})")
            except Exception as e:
                raise click.ClickException(f"Failed to parse the {manifest_type} manifest for '{title}'. ({e})")
            for track in parsed:
                tracks.add(track)
            parsed_ids = {str(t.id) for t in tracks}
            url_dicts = [t for t in tracks_map.values() if t.get("descriptor", "URL") == "URL"]
            # Merge back only tracks from another manifest, such as a separate HEVC one; the
            # re-parse already covers manifest_url's own. Query stripped when comparing, since a
            # service can sign the URL it fetches and register the bare one for the same manifest.
            manifest_base = str(manifest_url).split("?")[0]
            foreign_dicts = [
                t
                for t in tracks_map.values()
                if t.get("descriptor") == manifest_type
                and str(t.get("url")).split("?")[0] != manifest_base
                and str(t.get("id")) not in parsed_ids
            ]
            track_dicts = [*url_dicts, *foreign_dicts]
            # Only a side-loaded subtitle replaces the re-parsed set, since the service chose its
            # own copy over the manifest's. A subtitle on another manifest is an addition.
            if any(t.get("type") == "Subtitle" for t in url_dicts):
                tracks.subtitles.clear()
        else:
            track_dicts = list(tracks_map.values())

        for track_dict in track_dicts:
            try:
                track = Track.from_dict(track_dict)
            except Exception as e:
                self.log.warning(f"Skipping exported track {track_dict.get('id')!r}: {e}")
                continue
            drm = self.rebuild_drm(track_dict)
            if drm:
                track.drm = drm
            tracks.add(track, warn_only=True)

        resolve_import_manifest_data(
            tracks,
            manifest_type,
            session=self.session,
            language=title.language,
            title=title,
        )

        for attachment in entry.get("attachments") or []:
            url = attachment.get("url")
            if not url:
                continue
            try:
                tracks.attachments.append(
                    Attachment.from_url(
                        url,
                        name=attachment.get("name"),
                        mime_type=attachment.get("mime_type"),
                        description=attachment.get("description"),
                        session=self.session,
                    )
                )
            except Exception as e:
                self.log.warning(f"Skipping attachment '{attachment.get('name')}': {e}")

        self.tracks_by_title[title_id] = tracks
        return tracks

    def key_pool(self) -> dict[UUID, str]:
        """All exported KID:KEY pairs across every title, as {UUID: key_hex}."""
        pool: dict[UUID, str] = {}
        for entry in self.titles_data.values():
            for track_dict in (entry.get("tracks") or {}).values():
                for kid_hex, key in (track_dict.get("keys") or {}).items():
                    pool[UUID(hex=kid_hex)] = key
        return pool

    def rebuild_drm(self, track_dict: dict[str, Any]) -> Optional[list[Any]]:
        """Rebuild a DRM object (from stored PSSH, falling back to a stub) with the exported keys."""
        keys = track_dict.get("keys") or {}
        drm_dicts = track_dict.get("drm") or []
        if not drm_dicts and not keys:
            return None

        drm_obj = None
        if drm_dicts:
            try:
                drm_obj = drm_from_dict(drm_dicts[0])
            except Exception as e:
                self.log.debug(f"Falling back to DRM stub (PSSH rebuild failed: {e})")

        if drm_obj is None and keys:
            drm_type = (drm_dicts[0].get("system", "Widevine").lower()) if drm_dicts else "widevine"
            drm_obj = RemoteService.create_drm_stub(drm_type, list(keys.keys()))

        if drm_obj is None:
            return None

        for kid_hex, key in keys.items():
            drm_obj.content_keys[UUID(hex=kid_hex)] = key
        return [drm_obj]

    def resolve_server_keys(self, title: Title_T) -> None:
        """Inject exported keys into the selected encrypted tracks by KID (no network).

        dl.py calls this method after selection. It touches only encrypted video and audio
        tracks. Encrypted DASH tracks (no DRM at parse time) get a stub holding the keys,
        which DASH.download_track preserves. decrypt() applies the content key whose KID
        matches the media.
        """
        pool = self.key_pool()
        if not pool:
            return

        system = self.exported_drm_system()
        kid_hexes = [kid.hex for kid in pool]

        for track in title.tracks:
            if not isinstance(track, (Video, Audio)) or not self.track_is_encrypted(track):
                continue
            drm_obj = track.drm[0] if track.drm else RemoteService.create_drm_stub(system, kid_hexes)
            for kid, key in pool.items():
                drm_obj.content_keys[kid] = key
            track.drm = [drm_obj]
            self._server_cdm_type = drm_obj.__class__.__name__.lower()

    @staticmethod
    def track_is_encrypted(track: Any) -> bool:
        """True if the track carries DRM or its manifest declares protection.

        This method examines ISM as well as DASH, because ISM.download_track reads only
        ``track.drm``. A rung that misses content key injection here downloads encrypted and
        muxes without error. DASH re-derives
        its DRM from the manifest elements, so it survives the same omission.
        """
        if track.drm:
            return True
        data = getattr(track, "data", None) or {}
        dash = data.get("dash")
        if dash:
            for element in (dash.get("representation"), dash.get("adaptation_set")):
                if element is not None and element.findall("ContentProtection"):
                    return True
        ism = data.get("ism")
        if ism:
            manifest = ism.get("manifest")
            if manifest is not None and manifest.findall(".//ProtectionHeader"):
                return True
        return False

    def exported_drm_system(self) -> str:
        """The DRM system the exporter licensed (e.g. 'playready'), defaulting to widevine."""
        for entry in self.titles_data.values():
            for track_dict in (entry.get("tracks") or {}).values():
                for drm_dict in track_dict.get("drm") or []:
                    if drm_dict.get("system"):
                        return drm_dict["system"].lower()
        return "widevine"

    def get_chapters(self, title: Title_T) -> Chapters:
        entry = self.titles_data.get(str(title.id), {})
        return Chapters(
            [Chapter(ch["timestamp"], ch.get("name")) for ch in (entry.get("chapters") or []) if ch.get("timestamp")]
        )

    def get_widevine_service_certificate(self, **_: Any) -> Optional[str]:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        raise RuntimeError("ImportService should not request a license; keys come from the export.")

    def get_playready_license(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Optional[Union[bytes, str]]:
        raise RuntimeError("ImportService should not request a license; keys come from the export.")

    def get_clearkey_license(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Optional[Union[bytes, str, dict]]:
        raise RuntimeError("ImportService should not request a license; keys come from the export.")

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
        try:
            self.session.close()
        except Exception:
            pass
