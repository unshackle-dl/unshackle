from __future__ import annotations

import logging
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Optional, Union
from uuid import UUID

import click
import mediaexport
import requests
from langcodes import tag_is_valid

from unshackle.core.config import config
from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.drm import drm_from_dict, real_kids
from unshackle.core.manifests import DASH, HLS, ISM
from unshackle.core.remote_service import RemoteService, build_title, match_track, resolve_proxy_arg
from unshackle.core.titles import Episode, Movies, Series, Title_T, Titles_T, remap_titles
from unshackle.core.tracks import Audio, Chapter, Chapters, Subtitle, Tracks, Video
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
    GEOBLOCK: tuple[str, ...] = ()
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

        try:
            self.doc = mediaexport.read(export_path)
        except mediaexport.ExportError as e:
            raise click.ClickException(f"Cannot import {export_path.name}: {e}")
        for refused in self.doc.refused:
            self.log.warning(f"Skipping exported title {refused.raw.get('id')!r}: {refused.reason}")
        if not self.doc.titles:
            raise click.ClickException(f"Cannot import {export_path.name}: it has no title unshackle can use")

        self.titles_data: dict[str, Any] = {e.id: self.legacy_entry(e) for e in self.doc.titles}
        self.region: Optional[str] = self.doc.region or None
        self.titles: Optional[Titles_T] = None
        self.tracks_by_title: dict[str, Tracks] = {}

        self._server_cdm = True
        self._server_cdm_type = "widevine"

        first = self.doc.titles[0].primary if self.doc.titles else None
        headers = first.headers if first else {}
        self.session = self.build_session(ctx, self.region, headers)

    @staticmethod
    def legacy_entry(entry: mediaexport.Entry) -> dict[str, Any]:
        """The per-title dict the rest of this class reads, from a mediaexport entry.

        unshackle's own exports carry the full title meta and track dicts under
        ``x-unshackle``; a file from another tool has only the shared fields, so the meta is
        rebuilt from those and the track map stays empty (the manifest is parsed instead).
        """
        x = entry.extensions.get("x-unshackle") or {}
        meta = x.get("meta") or {
            "type": entry.kind if entry.kind in ("movie", "episode", "song") else "movie",
            "id": entry.id,
            "name": entry.title,
            "series_title": entry.series,
            "season": entry.season,
            "number": entry.episode,
            "year": entry.year,
            "language": entry.language or None,
            "artist": entry.artist,
            "album": entry.album,
            "track": entry.track_number,
        }
        primary = entry.primary
        manifest_type = x.get("manifest_type") or (
            (primary.type or mediaexport.guess_type(primary.url)).upper() if primary else ""
        )
        return {
            "meta": meta,
            "manifest_url": primary.url if primary else None,
            "manifest_type": manifest_type or None,
            "tracks": x.get("tracks") or {},
            "chapters": entry.chapters,
            "attachments": x.get("attachments") or [],
        }

    @staticmethod
    def build_session(
        ctx: click.Context, region: Optional[str] = None, headers: Optional[dict[str, str]] = None
    ) -> requests.Session:
        """HTTP session for re-fetching the manifest.

        Exported request headers go over the config defaults, since a CDN can gate the
        manifest on the User-Agent the exporter used. Honours the importer's ``--proxy``.
        Without one, it falls back to the export region as a geofence. An explicit proxy
        that unshackle cannot find raises an error. A region fallback gives a warning.
        """
        session = requests.Session()
        session.headers.update(config.headers)
        session.headers.update(headers or {})

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
        # An export from another tool has no track dicts, so HLS is parsed from the manifest too
        if manifest_url and parser is not None and (manifest_type in ("DASH", "ISM") or not tracks_map):
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

        # a side-load from another tool is only a tracks[] row with a url, not a track dict
        for sub in self.sideloaded_subtitles(title_id, skip=set(tracks_map)):
            tracks.add(sub, warn_only=True)

        for track_dict in track_dicts:
            try:
                track = Track.from_dict(track_dict)
            except Exception as e:
                self.log.warning(f"Skipping exported track {track_dict.get('id')!r}: {e}")
                continue
            drm = self.rebuild_drm(track_dict, title_id)
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

    def sideloaded_subtitles(self, title_id: str, skip: set[str]) -> list[Subtitle]:
        """Subtitle tracks for the ``tracks[]`` rows with a ``url``, except the ids in ``skip``.

        The row names the file format as its codec, or nothing, in which case the download
        finds it with MediaInfo. A row with no language cannot be muxed, so it is skipped.
        """
        entry = self.doc.get(title_id)
        out = []
        for row in entry.tracks if entry else []:
            if row.get("type") != "subtitle" or not row.get("url") or str(row.get("id")) in skip:
                continue
            language = row.get("language") or (entry.language if entry else "")
            if not language:
                self.log.warning(f"Skipping exported subtitle {row.get('id')!r}: it has no language")
                continue
            codec = None
            try:
                codec = Subtitle.Codec.from_mime(str(row.get("codec") or ""))
            except ValueError:
                pass
            out.append(
                Subtitle(
                    id_=str(row.get("id") or ""),
                    url=str(row["url"]),
                    language=str(language),
                    codec=codec,
                    sdh=bool(row.get("sdh")),
                    forced=bool(row.get("forced")),
                    cc=bool(row.get("cc")),
                )
            )
        return out

    def key_pool(self, title_id: Optional[str] = None) -> dict[UUID, str]:
        """Exported KID:KEY pairs as {UUID: key_hex}, for one title or across every title."""
        entry = self.doc.get(title_id) if title_id else None
        keys = entry.keys if entry else self.doc.key_pool()
        return {UUID(hex=kid): key for kid, key in keys.items()}

    def title_drm_dicts(self, title_id: str) -> list[dict[str, Any]]:
        """The title's DRM entries in ``drm_from_dict`` form.

        unshackle's own export keeps the exact ``to_dict`` output under ``x-unshackle``, so a
        ClearKeyCENC licence URL or a PlayReady PSSH comes back byte for byte.
        """
        entry = self.doc.get(title_id)
        if entry is None:
            return []
        own = (entry.extensions.get("x-unshackle") or {}).get("drm")
        if own:
            return list(own)
        out = []
        for d in entry.drm:
            system = {"widevine": "Widevine", "playready": "PlayReady", "clearkey": "ClearKeyCENC"}.get(d.system)
            if system:
                out.append({"system": system, "pssh_b64": d.pssh, "kids": list(entry.keys)})
        return out

    def exported_kids(self, title_id: str, track_id: str) -> Optional[set[str]]:
        """Return the KIDs (hex) the export's row for the track lists, or None when it lists none."""
        entry = self.doc.get(title_id)
        row = next((r for r in (entry.tracks if entry else []) if str(r.get("id", "")) == track_id), None)
        return set(row["kids"]) if row and row.get("kids") else None

    def exported_keys(self, title_id: str, track_id: str) -> dict[UUID, str]:
        """Return the exported content keys for the KIDs the row of the track lists, else all of the title."""
        entry = self.doc.get(title_id)
        return {UUID(hex=kid): key for kid, key in (entry.keys_for(track_id) if entry else {}).items()}

    def rebuild_drm(self, track_dict: dict[str, Any], title_id: str) -> Optional[list[Any]]:
        """Rebuild a DRM object (from stored PSSH, falling back to a stub) with the exported keys.

        Keys and DRM are title-level in the shared format; a legacy v2 track dict that still
        carries its own is honoured first. The track's row in the export can list its KIDs;
        then the track gets only their keys, and the DRM entry that names them. Without that
        list, a title with more than one DRM entry does not say which entry is this track's,
        so the track gets a stub that names every exported KID instead of a guess. A rebuilt
        PSSH holds only the keys for the KIDs it names.
        """
        own_keys = track_dict.get("keys")
        own_drm = track_dict.get("drm")
        row_kids = None if own_keys else self.exported_kids(title_id, str(track_dict.get("id", "")))
        if own_keys:
            keys = own_keys
        else:
            keys = {kid.hex: key for kid, key in self.exported_keys(title_id, str(track_dict.get("id", ""))).items()}
        drm_dicts = own_drm or self.title_drm_dicts(title_id)
        if row_kids and not own_drm:
            drm_dicts = [d for d in drm_dicts if row_kids & {str(kid).lower() for kid in d.get("kids") or []}]
        if not drm_dicts and not keys and not row_kids:
            return None

        drm_obj = None
        if drm_dicts and (own_drm or len(drm_dicts) == 1):
            try:
                drm_obj = drm_from_dict(drm_dicts[0])
            except Exception as e:
                self.log.debug(f"Falling back to DRM stub (PSSH rebuild failed: {e})")
            if drm_obj is not None and not own_keys and not row_kids:
                declared = {kid.hex for kid in real_kids(drm_obj)}
                if declared:
                    keys = {kid_hex: key for kid_hex, key in keys.items() if kid_hex in declared}

        stub_kids = list(keys) or sorted(row_kids or [])
        if drm_obj is None and stub_kids:
            drm_type = (drm_dicts[0].get("system", "Widevine").lower()) if drm_dicts else "widevine"
            drm_obj = RemoteService.create_drm_stub(drm_type, stub_kids)

        if drm_obj is None:
            return None

        for kid_hex, key in keys.items():
            drm_obj.content_keys[UUID(hex=kid_hex)] = key
        return [drm_obj]

    def resolve_server_keys(self, title: Title_T) -> None:
        """Inject exported keys into the selected encrypted tracks by KID (no network).

        dl.py calls this method after selection. It touches only encrypted video and audio
        tracks. A track gets only the content keys for the KIDs it declares: first the KIDs
        its row in the export lists, which needs no probe, then the KIDs of its DRM, manifest
        or init segment. It gets the whole title pool only when it declares no KID. Encrypted
        DASH tracks (no DRM at parse time)
        get a stub holding the content keys, which DASH.download_track preserves. The
        decrypters then match each content key to the media by KID, and give the all-zero KID
        only the content key of the track.
        """
        pool = self.key_pool(str(title.id)) or self.key_pool()
        if not pool:
            return

        system = self.exported_drm_system()
        title_id = str(title.id)

        for track in title.tracks:
            if not isinstance(track, (Video, Audio)):
                continue
            row_kids = self.exported_kids(title_id, str(track.id))
            if row_kids:
                declared: Optional[set[UUID]] = {UUID(hex=kid) for kid in row_kids}
                keys = self.exported_keys(title_id, str(track.id))
            else:
                declared = self.track_kids(track, self.session)
                if declared is None:
                    continue
                keys = {kid: key for kid, key in pool.items() if kid in declared} if declared else pool
            if track.drm:
                drm_obj = track.drm[0]
            else:
                drm_obj = RemoteService.create_drm_stub(system, [kid.hex for kid in (declared or pool)])
            for kid, key in keys.items():
                drm_obj.content_keys[kid] = key
            track.drm = [drm_obj]
            self._server_cdm_type = drm_obj.__class__.__name__.lower()

    @classmethod
    def track_is_encrypted(cls, track: Any, session: Optional[requests.Session] = None) -> bool:
        """True if the track carries DRM, its manifest declares protection, or its init segment does."""
        return cls.track_kids(track, session) is not None

    @staticmethod
    def track_kids(track: Any, session: Optional[requests.Session] = None) -> Optional[set[UUID]]:
        """Return the KIDs an encrypted track declares, or None when the track is not encrypted.

        An empty set means that the track is encrypted but declares no KID. The KIDs come from
        the track's DRM, then from the DASH ContentProtection default_KID, then from the DRM in
        the init segment. This method examines ISM as well as DASH, because ISM.download_track
        reads only ``track.drm``. A rung that misses content key injection here downloads
        encrypted and muxes without error. A DASH manifest with no ContentProtection can still
        describe an encrypted track, with the PSSH only in the init segment, so that case
        probes the init segment with `session`.
        """
        if track.drm:
            return {kid for drm in track.drm for kid in real_kids(drm)}
        data = getattr(track, "data", None) or {}
        dash = data.get("dash")
        if dash:
            protections = [
                cp
                for element in (dash.get("representation"), dash.get("adaptation_set"))
                if element is not None
                for cp in element.findall("ContentProtection")
            ]
            if protections:
                kids: set[UUID] = set()
                for cp in protections:
                    value = cp.get("{urn:mpeg:cenc:2013}default_KID") or cp.get("default_KID")
                    try:
                        kid = UUID(value) if value else None
                    except ValueError:
                        kid = None
                    if kid and kid.int:
                        kids.add(kid)
                return kids
            from unshackle.core.api.handlers import drm_from_init_segment

            init_drm = drm_from_init_segment(track, session)
            if init_drm:
                return {kid for drm in init_drm for kid in real_kids(drm)}
        ism = data.get("ism")
        if ism:
            manifest = ism.get("manifest")
            if manifest is not None and manifest.findall(".//ProtectionHeader"):
                return set()
        return None

    def exported_drm_system(self) -> str:
        """The DRM system the exporter licensed (e.g. 'playready'), defaulting to widevine."""
        for entry in self.doc.titles:
            for d in entry.drm:
                if d.system:
                    return d.system
        return "widevine"

    def get_chapters(self, title: Title_T) -> Chapters:
        entry = self.doc.get(str(title.id))
        return Chapters(
            [
                Chapter(int(ch["start_ms"]), ch.get("title") or None)
                for ch in (entry.chapters if entry else [])
                if ch.get("start_ms") is not None
            ]
        )

    def get_widevine_service_certificate(self, **_: Any) -> Optional[str]:
        return None

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        raise self.missing_key(track)

    def get_playready_license(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Optional[Union[bytes, str]]:
        raise self.missing_key(track)

    def get_clearkey_license(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Optional[Union[bytes, str, dict]]:
        raise self.missing_key(track)

    @staticmethod
    def missing_key(track: AnyTrack) -> click.ClickException:
        """Return the error for a content key that neither the export nor a vault holds.

        An import uses only the content keys in the export and in the vaults, and never
        licenses through a CDM. dl then asks for a licence, and this error stops the download.
        """
        kids = sorted(
            {
                kid.hex
                for drm in getattr(track, "drm", None) or []
                for kid in real_kids(drm)
                if kid not in (getattr(drm, "content_keys", None) or {})
            }
        )
        subject = f"KID {', '.join(kids)}" if kids else f"track {track}"
        return click.ClickException(
            f"The export has no content key for {subject}, and no vault has it. An import does not "
            "license through a CDM: export the title again with the tracks you need selected."
        )

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
