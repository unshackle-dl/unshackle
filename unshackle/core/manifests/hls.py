from __future__ import annotations

import base64
import html
import json
import logging
import os
import shutil
import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import Any, Callable, Optional, Union
from urllib.parse import urljoin
from uuid import UUID
from zlib import crc32

import m3u8
from langcodes import Language, tag_is_valid
from m3u8 import M3U8
from pyplayready.cdm import Cdm as PlayReadyCdm
from pyplayready.system.pssh import PSSH as PR_PSSH
from pywidevine.cdm import Cdm as WidevineCdm
from pywidevine.pssh import PSSH as WV_PSSH
from requests import Session

from unshackle.core import binaries
from unshackle.core.cdm.detect import is_playready_cdm, is_widevine_cdm
from unshackle.core.clients.base import BaseHttpClient
from unshackle.core.clients.exceptions import NetworkURLRequired
from unshackle.core.clients.factory import http_unshackle
from unshackle.core.constants import DOWNLOAD_CANCELLED, DOWNLOAD_LICENCE_ONLY, AnyTrack
from unshackle.core.downloaders import requests as requests_downloader
from unshackle.core.drm import DRM_T, ClearKey, MonaLisa, PlayReady, Widevine
from unshackle.core.events import events
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video
from unshackle.core.utilities import get_debug_logger, get_extension, is_close_match, try_ensure_utf8


class HLS:
    def __init__(self, manifest: M3U8, session: Optional[BaseHttpClient] = None):
        if not manifest:
            raise ValueError("HLS manifest must be provided.")
        if not isinstance(manifest, M3U8):
            raise TypeError(f"Expected manifest to be a {M3U8}, not {manifest!r}")
        if not manifest.is_variant:
            raise ValueError("Expected the M3U(8) manifest to be a Variant Playlist.")

        self.manifest = manifest
        self.session = session or Session()

    @classmethod
    def from_url(cls, url: str, session: Optional[BaseHttpClient] = None, **args: Any) -> HLS:
        if not url:
            raise NetworkURLRequired("HLS manifest URL must be provided.")
        if not isinstance(url, str):
            raise TypeError(f"Expected url to be a {str}, not {url!r}")

        if not session:
            session = http_unshackle.new('hls')
        elif not isinstance(session, BaseHttpClient):
            raise TypeError(f"Expected session to be a {BaseHttpClient}, not {session!r}")

        try:
            res = session.get(url, **args)
        except Exception as e:
            raise RuntimeError("Failed to request the M3U(8) document.") from e
        content = res.text
        master = m3u8.loads(content, uri=url)

        return cls(master, session)

    @classmethod
    def from_text(cls, text: str, url: str) -> HLS:
        if not text:
            raise ValueError("HLS manifest Text must be provided.")
        if not isinstance(text, str):
            raise TypeError(f"Expected text to be a {str}, not {text!r}")

        if not url:
            raise NetworkURLRequired("HLS manifest URL must be provided for relative path computations.")
        if not isinstance(url, str):
            raise TypeError(f"Expected url to be a {str}, not {url!r}")

        master = m3u8.loads(text, uri=url)

        return cls(master)

    def to_tracks(self, language: Union[str, Language]) -> Tracks:
        """
        Convert a Variant Playlist M3U(8) document to Video, Audio and Subtitle Track objects.

        Parameters:
            language: Language you expect the Primary Track to be in.

        All Track objects' URL will be to another M3U(8) document. However, these documents
        will be Invariant Playlists and contain the list of segments URIs among other metadata.
        """
        session_keys = list(self.manifest.session_keys or [])
        if not session_keys:
            session_keys = HLS.parse_session_data_keys(self.manifest, self.session)

        session_drm = HLS.get_all_drm(session_keys)

        audio_codecs_by_group_id: dict[str, Audio.Codec] = {}
        tracks = Tracks()

        for playlist in self.manifest.playlists:
            audio_group = playlist.stream_info.audio
            audio_codec: Optional[Audio.Codec] = None
            if audio_group and playlist.stream_info.codecs:
                try:
                    audio_codec = Audio.Codec.from_codecs(playlist.stream_info.codecs)
                except ValueError:
                    audio_codec = None
                if audio_codec:
                    audio_codecs_by_group_id[audio_group] = audio_codec

            try:
                # TODO: Any better way to figure out the primary track type?
                if playlist.stream_info.codecs:
                    Video.Codec.from_codecs(playlist.stream_info.codecs)
            except ValueError:
                primary_track_type = Audio
            else:
                primary_track_type = Video

            tracks.add(
                primary_track_type(
                    id_=hex(crc32(str(playlist).encode()))[2:],
                    url=urljoin(playlist.base_uri, playlist.uri),
                    codec=(
                        primary_track_type.Codec.from_codecs(playlist.stream_info.codecs)
                        if playlist.stream_info.codecs
                        else None
                    ),
                    language=language,  # HLS manifests do not seem to have language info
                    is_original_lang=True,  # TODO: All we can do is assume Yes
                    bitrate=playlist.stream_info.average_bandwidth or playlist.stream_info.bandwidth,
                    descriptor=Video.Descriptor.HLS,
                    drm=session_drm,
                    data={"hls": {"playlist": playlist}},
                    # video track args
                    **(
                        dict(
                            range_=Video.Range.DV
                            if any(
                                codec.split(".")[0] in ("dva1", "dvav", "dvhe", "dvh1")
                                for codec in (playlist.stream_info.codecs or "").lower().split(",")
                            )
                            else Video.Range.from_m3u_range_tag(playlist.stream_info.video_range),
                            width=playlist.stream_info.resolution[0] if playlist.stream_info.resolution else None,
                            height=playlist.stream_info.resolution[1] if playlist.stream_info.resolution else None,
                            fps=playlist.stream_info.frame_rate,
                        )
                        if primary_track_type is Video
                        else {}
                    ),
                )
            )

        for media in self.manifest.media:
            if not media.uri:
                continue

            joc = 0
            if media.type == "AUDIO":
                track_type = Audio
                codec = audio_codecs_by_group_id.get(media.group_id)
                if media.channels and media.channels.endswith("/JOC"):
                    joc = int(media.channels.split("/JOC")[0])
                    media.channels = "5.1"
            else:
                track_type = Subtitle
                codec = Subtitle.Codec.WebVTT  # assuming WebVTT, codec info isn't shown

            track_lang = next(
                (
                    Language.get(option)
                    for x in (media.language, language)
                    for option in [(str(x) or "").strip()]
                    if tag_is_valid(option) and not option.startswith("und")
                ),
                None,
            )
            if not track_lang:
                msg = "Language information could not be derived for a media."
                if language is None:
                    msg += " No fallback language was provided when calling HLS.to_tracks()."
                elif not tag_is_valid((str(language) or "").strip()) or str(language).startswith("und"):
                    msg += f" The fallback language provided is also invalid: {language}"
                raise ValueError(msg)

            tracks.add(
                track_type(
                    id_=hex(crc32(str(media).encode()))[2:],
                    url=urljoin(media.base_uri, media.uri),
                    codec=codec,
                    language=track_lang,  # HLS media may not have language info, fallback if needed
                    is_original_lang=bool(language and is_close_match(track_lang, [language])),
                    descriptor=Audio.Descriptor.HLS,
                    drm=session_drm if media.type == "AUDIO" else None,
                    data={"hls": {"media": media}},
                    # audio track args
                    **(
                        dict(
                            bitrate=0,  # TODO: M3U doesn't seem to state bitrate?
                            channels=media.channels,
                            joc=joc,
                            descriptive="public.accessibility.describes-video" in (media.characteristics or ""),
                        )
                        if track_type is Audio
                        else dict(
                            forced=media.forced == "YES",
                            sdh="public.accessibility.describes-music-and-sound" in (media.characteristics or ""),
                        )
                        if track_type is Subtitle
                        else {}
                    ),
                )
            )

        return tracks

    @staticmethod
    def _finalize_n_m3u8dl_re_output(*, track: AnyTrack, save_dir: Path, save_path: Path) -> Path:
        """
        Finalize output from N_m3u8DL-RE.

        We call N_m3u8DL-RE with `--save-name track.id`, so the final file should be `{track.id}.*` under `save_dir`.
        This moves that output to `save_path` (preserving the real suffix) and, for subtitles, updates `track.codec`
        to match the produced file extension.
        """
        matches = [p for p in save_dir.rglob(f"{track.id}.*") if p.is_file()]
        if not matches:
            raise FileNotFoundError(f"No output files produced by N_m3u8DL-RE for save-name={track.id} in: {save_dir}")

        primary = max(matches, key=lambda p: p.stat().st_size)

        final_save_path = save_path.with_suffix(primary.suffix) if primary.suffix else save_path

        final_save_path.parent.mkdir(parents=True, exist_ok=True)
        if primary.absolute() != final_save_path.absolute():
            final_save_path.unlink(missing_ok=True)
            shutil.move(str(primary), str(final_save_path))

        if isinstance(track, Subtitle):
            ext = final_save_path.suffix.lower().lstrip(".")
            try:
                track.codec = Subtitle.Codec.from_mime(ext)
            except ValueError:
                pass

        shutil.rmtree(save_dir, ignore_errors=True)

        return final_save_path

    @staticmethod
    def download_track(
        track: AnyTrack,
        save_path: Path,
        save_dir: Path,
        progress: partial,
        session: Optional[BaseHttpClient] = None,
        proxy: Optional[str] = None,
        max_workers: Optional[int] = None,
        license_widevine: Optional[Callable] = None,
        *,
        cdm: Optional[object] = None,
    ) -> None:
        if not session:
            session = http_unshackle.new('hls')
        elif not isinstance(session, BaseHttpClient):
            raise TypeError(f"Expected session to be a {BaseHttpClient}, not {session!r}")

        if proxy:
            # Handle proxies differently based on session type
            if isinstance(session, Session):
                session.proxies.update({"all": proxy})

        log = logging.getLogger("HLS")

        if track.from_file:
            master = m3u8.load(str(track.from_file))
        else:
            # Get the playlist text and handle both session types
            try:
                response = session.get(track.url)
            except Exception as e:
                raise RuntimeError(f"Failed to request the invariant M3U8 playlist") from e
            playlist_text = response.text
            master = m3u8.loads(playlist_text, uri=track.url)

        if not master.segments:
            log.error("Track's HLS playlist has no segments, expecting an invariant M3U8 playlist.")
            sys.exit(1)

        # Get session DRM as fallback but prefer media playlist keys for accurate KID matching
        if track.drm:
            session_drm = track.get_drm_for_cdm(cdm)
        else:
            session_drm = None

        initial_drm_licensed = False
        initial_drm_key = None  # Track the EXT-X-KEY used for initial licensing
        media_keys = [k for k in (master.keys or []) if k is not None]
        if media_keys:
            cdm_media_keys = HLS.filter_keys_for_cdm(media_keys, cdm)
            media_playlist_key = HLS.get_supported_key(cdm_media_keys) if cdm_media_keys else None

            if media_playlist_key:
                media_drm = HLS.get_drm(media_playlist_key, session)
                if isinstance(media_drm, (Widevine, PlayReady)):
                    track_kid = HLS.get_track_kid_from_init(master, track, session) or media_drm.kid
                    try:
                        if not license_widevine:
                            raise ValueError("license_widevine func must be supplied to use DRM")
                        progress(downloaded="LICENSING")
                        license_widevine(media_drm, track_kid=track_kid)
                        progress(downloaded="[yellow]LICENSED")
                        initial_drm_licensed = True
                        initial_drm_key = media_playlist_key
                        track.drm = [media_drm]
                        session_drm = media_drm
                    except Exception:  # noqa
                        DOWNLOAD_CANCELLED.set()  # skip pending track downloads
                        progress(downloaded="[red]FAILED")
                        raise

        # Fall back to session DRM if media playlist has no matching keys
        if not initial_drm_licensed and session_drm and isinstance(session_drm, (Widevine, PlayReady)):
            try:
                if not license_widevine:
                    raise ValueError("license_widevine func must be supplied to use DRM")
                progress(downloaded="LICENSING")
                license_widevine(session_drm)
                progress(downloaded="[yellow]LICENSED")
            except Exception:  # noqa
                DOWNLOAD_CANCELLED.set()  # skip pending track downloads
                progress(downloaded="[red]FAILED")
                raise

        if not initial_drm_licensed and session_drm and isinstance(session_drm, MonaLisa):
            try:
                if not license_widevine:
                    raise ValueError("license_widevine func must be supplied to use DRM")
                progress(downloaded="LICENSING")
                license_widevine(session_drm)
                progress(downloaded="[yellow]LICENSED")
            except Exception:  # noqa
                DOWNLOAD_CANCELLED.set()  # skip pending track downloads
                progress(downloaded="[red]FAILED")
                raise

        if DOWNLOAD_LICENCE_ONLY.is_set():
            progress(downloaded="[yellow]SKIPPED")
            return

        unwanted_segments = [
            segment for segment in master.segments if callable(track.OnSegmentFilter) and track.OnSegmentFilter(segment)
        ]

        total_segments = len(master.segments) - len(unwanted_segments)
        progress(total=total_segments)

        downloader = track.downloader
        if downloader.__name__ == "aria2c" and any(x.byterange for x in master.segments if x not in unwanted_segments):
            downloader = requests_downloader
            log.warning("Falling back to the requests downloader as aria2(c) doesn't support the Range header")

        urls: list[dict[str, Any]] = []
        segment_durations: list[int] = []

        range_offset = 0
        for segment in master.segments:
            if segment in unwanted_segments:
                continue

            segment_durations.append(int(segment.duration))

            if segment.byterange:
                byte_range = HLS.calculate_byte_range(segment.byterange, range_offset)
                range_offset = int(byte_range.split("-")[0])
            else:
                byte_range = None

            urls.append(
                {
                    "url": urljoin(segment.base_uri, segment.uri),
                    "headers": {"Range": f"bytes={byte_range}"} if byte_range else {},
                }
            )

        track.data["hls"]["segment_durations"] = segment_durations

        segment_save_dir = save_dir / "segments"

        skip_merge = False
        downloader_args = dict(
            urls=urls,
            output_dir=segment_save_dir,
            filename="{i:0%d}{ext}" % len(str(len(urls))),
            headers=session.headers,
            cookies=session.cookies,
            proxy=proxy,
            max_workers=max_workers,
        )

        if downloader.__name__ == "n_m3u8dl_re":
            skip_merge = True
            # session_drm already has correct content_keys from initial licensing above
            n_m3u8dl_content_keys = session_drm.content_keys if session_drm else None

            downloader_args.update(
                {
                    "output_dir": save_dir,
                    "filename": track.id,
                    "track": track,
                    "content_keys": n_m3u8dl_content_keys,
                }
            )

        debug_logger = get_debug_logger()
        if debug_logger:
            debug_logger.log(
                level="DEBUG",
                operation="manifest_hls_download_start",
                message="Starting HLS manifest download",
                context={
                    "track_id": getattr(track, "id", None),
                    "track_type": track.__class__.__name__,
                    "total_segments": total_segments,
                    "downloader": downloader.__name__,
                    "has_drm": bool(session_drm),
                    "drm_type": session_drm.__class__.__name__ if session_drm else None,
                    "skip_merge": skip_merge,
                    "save_path": str(save_path),
                },
            )

        for status_update in downloader(**downloader_args):
            file_downloaded = status_update.get("file_downloaded")
            if file_downloaded:
                events.emit(events.Types.SEGMENT_DOWNLOADED, track=track, segment=file_downloaded)
            else:
                downloaded = status_update.get("downloaded")
                if downloaded and downloaded.endswith("/s"):
                    status_update["downloaded"] = f"HLS {downloaded}"
                progress(**status_update)

        # see https://github.com/devine-dl/devine/issues/71
        for control_file in segment_save_dir.glob("*.aria2__temp"):
            control_file.unlink()

        if skip_merge:
            final_save_path = HLS._finalize_n_m3u8dl_re_output(track=track, save_dir=save_dir, save_path=save_path)
            progress(downloaded="Downloaded")
            track.path = final_save_path
            events.emit(events.Types.TRACK_DOWNLOADED, track=track)
            return

        progress(total=total_segments, completed=0, downloaded="Merging")

        name_len = len(str(total_segments))
        discon_i = 0
        range_offset = 0
        map_data: Optional[tuple[m3u8.model.InitializationSection, bytes]] = None
        if session_drm:
            encryption_data: Optional[tuple[Optional[m3u8.Key], DRM_T]] = (initial_drm_key, session_drm)
        else:
            encryption_data: Optional[tuple[Optional[m3u8.Key], DRM_T]] = None

        i = -1
        for real_i, segment in enumerate(master.segments):
            if segment not in unwanted_segments:
                i += 1

            is_last_segment = (real_i + 1) == len(master.segments)

            def merge(to: Path, via: list[Path], delete: bool = False, include_map_data: bool = False):
                """
                Merge all files to a given path, optionally including map data.

                Parameters:
                    to: The output file with all merged data.
                    via: List of files to merge, in sequence.
                    delete: Delete the file once it's been merged.
                    include_map_data: Whether to include the init map data.
                """
                with open(to, "wb") as x:
                    if include_map_data and map_data and map_data[1]:
                        x.write(map_data[1])
                    for file in via:
                        x.write(file.read_bytes())
                        x.flush()
                        if delete:
                            file.unlink()

            def decrypt(include_this_segment: bool) -> Path:
                """
                Decrypt all segments that uses the currently set DRM.

                All segments that will be decrypted with this DRM will be merged together
                in sequence, prefixed with the init data (if any), and then deleted. Once
                merged they will be decrypted. The merged and decrypted file names state
                the range of segments that were used.

                Parameters:
                    include_this_segment: Whether to include the current segment in the
                        list of segments to merge and decrypt. This should be False if
                        decrypting on EXT-X-KEY changes, or True when decrypting on the
                        last segment.

                Returns the decrypted path.
                """
                drm = encryption_data[1]
                first_segment_i = next(
                    int(file.stem) for file in sorted(segment_save_dir.iterdir()) if file.stem.isdigit()
                )
                last_segment_i = max(0, i - int(not include_this_segment))
                range_len = (last_segment_i - first_segment_i) + 1

                segment_range = f"{str(first_segment_i).zfill(name_len)}-{str(last_segment_i).zfill(name_len)}"
                merged_path = segment_save_dir / f"{segment_range}{get_extension(master.segments[last_segment_i].uri)}"
                decrypted_path = segment_save_dir / f"{merged_path.stem}_decrypted{merged_path.suffix}"

                files = [
                    file
                    for file in sorted(segment_save_dir.iterdir())
                    if file.stem.isdigit() and first_segment_i <= int(file.stem) <= last_segment_i
                ]
                if not files:
                    raise ValueError(f"None of the segment files for {segment_range} exist...")
                elif len(files) != range_len:
                    raise ValueError(f"Missing {range_len - len(files)} segment files for {segment_range}...")

                if isinstance(drm, (Widevine, PlayReady)):
                    # with widevine we can merge all segments and decrypt once
                    merge(to=merged_path, via=files, delete=True, include_map_data=True)
                    drm.decrypt(merged_path)
                    merged_path.rename(decrypted_path)
                else:
                    # with other drm we must decrypt separately and then merge them
                    # for aes this is because each segment likely has 16-byte padding
                    for file in files:
                        drm.decrypt(file)
                    merge(to=merged_path, via=files, delete=True, include_map_data=True)

                events.emit(events.Types.TRACK_DECRYPTED, track=track, drm=drm, segment=decrypted_path)

                return decrypted_path

            def merge_discontinuity(include_this_segment: bool, include_map_data: bool = True):
                """
                Merge all segments of the discontinuity.

                All segment files for this discontinuity must already be downloaded and
                already decrypted (if it needs to be decrypted).

                Parameters:
                    include_this_segment: Whether to include the current segment in the
                        list of segments to merge and decrypt. This should be False if
                        decrypting on EXT-X-KEY changes, or True when decrypting on the
                        last segment.
                    include_map_data: Whether to prepend the init map data before the
                        segment files when merging.
                """
                last_segment_i = max(0, i - int(not include_this_segment))

                files = [
                    file
                    for file in sorted(segment_save_dir.iterdir())
                    if int(file.stem.replace("_decrypted", "").split("-")[-1]) <= last_segment_i
                ]
                if files:
                    to_dir = segment_save_dir.parent
                    to_path = to_dir / f"{str(discon_i).zfill(name_len)}{files[-1].suffix}"
                    merge(to=to_path, via=files, delete=True, include_map_data=include_map_data)

            if segment not in unwanted_segments:
                if isinstance(track, Subtitle):
                    segment_file_ext = get_extension(segment.uri)
                    segment_file_path = segment_save_dir / f"{str(i).zfill(name_len)}{segment_file_ext}"
                    segment_data = try_ensure_utf8(segment_file_path.read_bytes())
                    if track.codec not in (Subtitle.Codec.fVTT, Subtitle.Codec.fTTML):
                        segment_data = (
                            segment_data.decode("utf8")
                            .replace("&lrm;", html.unescape("&lrm;"))
                            .replace("&rlm;", html.unescape("&rlm;"))
                            .encode("utf8")
                        )
                    segment_file_path.write_bytes(segment_data)

                if segment.discontinuity and i != 0:
                    if encryption_data:
                        decrypt(include_this_segment=False)
                    merge_discontinuity(
                        include_this_segment=False, include_map_data=not encryption_data or not encryption_data[1]
                    )

                    discon_i += 1
                    range_offset = 0  # TODO: Should this be reset or not?
                    map_data = None

                if segment.init_section and (not map_data or segment.init_section != map_data[0]):
                    if segment.init_section.byterange:
                        init_byte_range = HLS.calculate_byte_range(segment.init_section.byterange, range_offset)
                        range_offset = int(init_byte_range.split("-")[0])
                        init_range_header = {"Range": f"bytes={init_byte_range}"}
                    else:
                        init_range_header = {}

                    # Handle both session types for init section request
                    res = session.get(
                        url=urljoin(segment.init_section.base_uri, segment.init_section.uri),
                        headers=init_range_header,
                    )
                    init_content = res.content
                    map_data = (segment.init_section, init_content)

            segment_keys = getattr(segment, "keys", None)
            if segment_keys:
                if cdm:
                    cdm_segment_keys = HLS.filter_keys_for_cdm(segment_keys, cdm)
                    key = (
                        HLS.get_supported_key(cdm_segment_keys)
                        if cdm_segment_keys
                        else HLS.get_supported_key(segment_keys)
                    )
                else:
                    key = HLS.get_supported_key(segment_keys)
                if encryption_data and encryption_data[0] != key and i != 0 and segment not in unwanted_segments:
                    decrypt(include_this_segment=False)

                if key is None:
                    encryption_data = None
                elif not encryption_data or encryption_data[0] != key:
                    drm = HLS.get_drm(key, session)
                    if isinstance(drm, (Widevine, PlayReady)):
                        try:
                            if map_data:
                                track_kid = track.get_key_id(map_data[1])
                            else:
                                track_kid = None
                            if not track_kid:
                                track_kid = drm.kid
                            progress(downloaded="LICENSING")
                            license_widevine(drm, track_kid=track_kid)
                            progress(downloaded="[yellow]LICENSED")
                        except Exception:  # noqa
                            DOWNLOAD_CANCELLED.set()  # skip pending track downloads
                            progress(downloaded="[red]FAILED")
                            raise
                    encryption_data = (key, drm)

            if DOWNLOAD_LICENCE_ONLY.is_set():
                continue

            if is_last_segment:
                # required as it won't end with EXT-X-DISCONTINUITY nor a new key
                if encryption_data:
                    decrypt(include_this_segment=True)
                merge_discontinuity(
                    include_this_segment=True, include_map_data=not encryption_data or not encryption_data[1]
                )

            progress(advance=1)

        if DOWNLOAD_LICENCE_ONLY.is_set():
            return

        def find_segments_recursively(directory: Path) -> list[Path]:
            """Find all segment files recursively in any directory structure created by downloaders."""
            segments = []

            # First check direct files in the directory
            if directory.exists():
                segments.extend([x for x in directory.iterdir() if x.is_file()])

                # If no direct files, recursively search subdirectories
                if not segments:
                    for subdir in directory.iterdir():
                        if subdir.is_dir():
                            segments.extend(find_segments_recursively(subdir))

            return sorted(segments)

        # finally merge all the discontinuity save files together to the final path
        segments_to_merge = find_segments_recursively(save_dir)

        if debug_logger:
            debug_logger.log(
                level="DEBUG",
                operation="manifest_hls_download_complete",
                message="HLS download complete, preparing to merge",
                context={
                    "track_id": getattr(track, "id", None),
                    "track_type": track.__class__.__name__,
                    "save_dir": str(save_dir),
                    "save_dir_exists": save_dir.exists(),
                    "segments_found": len(segments_to_merge),
                    "segment_files": [f.name for f in segments_to_merge[:10]],  # Limit to first 10
                    "downloader": downloader.__name__,
                    "skip_merge": skip_merge,
                },
            )

        if not segments_to_merge:
            error_msg = f"No segment files found in output directory: {save_dir}"
            if debug_logger:
                all_contents = list(save_dir.iterdir()) if save_dir.exists() else []
                debug_logger.log(
                    level="ERROR",
                    operation="manifest_hls_download_no_segments",
                    message=error_msg,
                    context={
                        "track_id": getattr(track, "id", None),
                        "track_type": track.__class__.__name__,
                        "save_dir": str(save_dir),
                        "save_dir_exists": save_dir.exists(),
                        "directory_contents": [str(p) for p in all_contents],
                        "downloader": downloader.__name__,
                        "skip_merge": skip_merge,
                    },
                )
            raise FileNotFoundError(error_msg)

        if len(segments_to_merge) == 1:
            shutil.move(segments_to_merge[0], save_path)
        else:
            progress(downloaded="Merging")
            if isinstance(track, (Video, Audio)):
                HLS.merge_segments(segments=segments_to_merge, save_path=save_path)
            else:
                with open(save_path, "wb") as f:
                    for discontinuity_file in segments_to_merge:
                        discontinuity_data = discontinuity_file.read_bytes()
                        f.write(discontinuity_data)
                        f.flush()
                        os.fsync(f.fileno())
                        discontinuity_file.unlink()

        # Clean up empty segment directory
        if save_dir.exists() and save_dir.name.endswith("_segments"):
            try:
                save_dir.rmdir()
            except OSError:
                # Directory might not be empty, try removing recursively
                shutil.rmtree(save_dir, ignore_errors=True)

        progress(downloaded="Downloaded")

        track.path = save_path
        events.emit(events.Types.TRACK_DOWNLOADED, track=track)

    @staticmethod
    def merge_segments(segments: list[Path], save_path: Path) -> int:
        """
        Concatenate Segments using FFmpeg concat with binary fallback.

        Returns the file size of the merged file.
        """
        # Track segment directories for cleanup
        segment_dirs = set()
        for segment in segments:
            # Track all parent directories that contain segments
            current_dir = segment.parent
            while current_dir.name and "_segments" in str(current_dir):
                segment_dirs.add(current_dir)
                current_dir = current_dir.parent

        def cleanup_segments_and_dirs():
            """Clean up segments and directories after successful merge."""
            for segment in segments:
                segment.unlink(missing_ok=True)
            for segment_dir in segment_dirs:
                if segment_dir.exists():
                    try:
                        shutil.rmtree(segment_dir)
                    except OSError:
                        pass  # Directory cleanup failed, but merge succeeded

        # Try FFmpeg concat first (preferred method)
        if binaries.FFMPEG:
            try:
                demuxer_file = save_path.parent / f"ffmpeg_concat_demuxer_{save_path.stem}.txt"
                demuxer_file.write_text("\n".join([f"file '{segment.absolute()}'" for segment in segments]))

                subprocess.check_call(
                    [
                        binaries.FFMPEG,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        demuxer_file,
                        "-map",
                        "0",
                        "-c",
                        "copy",
                        save_path,
                    ],
                    timeout=300,  # 5 minute timeout
                )
                demuxer_file.unlink(missing_ok=True)
                cleanup_segments_and_dirs()
                return save_path.stat().st_size

            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                # FFmpeg failed, clean up demuxer file and fall back to binary concat
                logging.getLogger("HLS").debug(f"FFmpeg concat failed ({e}), falling back to binary concatenation")
                demuxer_file.unlink(missing_ok=True)
                # Remove partial output file if it exists
                save_path.unlink(missing_ok=True)

        # Fallback: Binary concatenation
        logging.getLogger("HLS").debug(f"Using binary concatenation for {len(segments)} segments")
        with open(save_path, "wb") as output_file:
            for segment in segments:
                with open(segment, "rb") as segment_file:
                    output_file.write(segment_file.read())

        cleanup_segments_and_dirs()
        return save_path.stat().st_size

    @staticmethod
    def parse_session_data_keys(
        manifest: M3U8, session: Optional[BaseHttpClient] = None
    ) -> list[m3u8.model.Key]:
        """Parse `com.apple.hls.keys` session data and return Key objects."""
        keys: list[m3u8.model.Key] = []

        for data in getattr(manifest, "session_data", []) or []:
            if getattr(data, "data_id", None) != "com.apple.hls.keys":
                continue

            value = getattr(data, "value", None)
            if not value and data.uri:
                if not session:
                    session = http_unshackle.new('hls')
                res = session.get(urljoin(manifest.base_uri or "", data.uri))
                value = res.text

            if not value:
                continue

            try:
                decoded = base64.b64decode(value).decode()
            except Exception:
                decoded = value

            try:
                items = json.loads(decoded)
            except Exception:
                continue

            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                key = m3u8.model.Key(
                    method=item.get("method"),
                    base_uri=manifest.base_uri or "",
                    uri=item.get("uri"),
                    keyformat=item.get("keyformat"),
                    keyformatversions=",".join(item.get("keyformatversion") or item.get("keyformatversions") or []),
                )
                if key.method in {"AES-128", "ISO-23001-7"} or (
                    key.keyformat
                    and key.keyformat.lower()
                    in {
                        WidevineCdm.urn,
                        PlayReadyCdm,
                        "com.microsoft.playready",
                    }
                ):
                    keys.append(key)

        return keys

    @staticmethod
    def filter_keys_for_cdm(
        keys: list[Union[m3u8.model.SessionKey, m3u8.model.Key]],
        cdm: object,
    ) -> list[Union[m3u8.model.SessionKey, m3u8.model.Key]]:
        """
        Filter EXT-X-KEY entries to only include those matching the CDM type.

        This ensures we select the correct DRM system (Widevine vs PlayReady)
        based on what CDM is configured, avoiding license request failures.
        """
        playready_urn = f"urn:uuid:{PR_PSSH.SYSTEM_ID}"
        playready_keyformats = {playready_urn, "com.microsoft.playready"}
        if is_widevine_cdm(cdm):
            return [k for k in keys if k.keyformat and k.keyformat.lower() == WidevineCdm.urn]
        elif is_playready_cdm(cdm):
            return [k for k in keys if k.keyformat and k.keyformat.lower() in playready_keyformats]
        return keys

    @staticmethod
    def get_track_kid_from_init(
        master: M3U8,
        track: AnyTrack,
        session: BaseHttpClient,
    ) -> Optional[UUID]:
        """
        Extract the track's Key ID from its init segment (EXT-X-MAP).

        Returns None if no init segment exists or KID extraction fails.
        The caller should fall back to drm.kid from the PSSH if this returns None.
        """
        map_section = next((seg.init_section for seg in master.segments if seg.init_section), None)
        if not map_section:
            return None

        map_uri = urljoin(map_section.base_uri or master.base_uri or "", map_section.uri)
        try:
            if map_section.byterange:
                byte_range = HLS.calculate_byte_range(map_section.byterange, 0)
                headers = {"Range": f"bytes={byte_range}"}
            else:
                headers = {}
            map_res = session.get(url=map_uri, headers=headers)
            if map_res.ok:
                return track.get_key_id(map_res.content)
        except Exception:
            pass
        return None

    @staticmethod
    def get_supported_key(keys: list[Union[m3u8.model.SessionKey, m3u8.model.Key]]) -> Optional[m3u8.Key]:
        """
        Get a support Key System from a list of Key systems.

        Note that the key systems are chosen in an opinionated order.

        Returns None if one of the key systems is method=NONE, which means all segments
        from hence forth should be treated as plain text until another key system is
        encountered, unless it's also method=NONE.

        Raises NotImplementedError if none of the key systems are supported.
        """
        if any(key.method == "NONE" for key in keys):
            return None

        unsupported_systems = []
        for key in keys:
            if not key:
                continue
            # TODO: Add a way to specify which supported key system to use
            # TODO: Add support for 'SAMPLE-AES', 'AES-CTR', 'AES-CBC', 'ClearKey'
            elif key.method == "AES-128":
                return key
            elif key.method == "ISO-23001-7":
                return key
            elif key.keyformat and key.keyformat.lower() == WidevineCdm.urn:
                return key
            elif key.keyformat and key.keyformat.lower() in {
                f"urn:uuid:{PR_PSSH.SYSTEM_ID}",
                "com.microsoft.playready",
            }:
                return key
            else:
                unsupported_systems.append(key.method + (f" ({key.keyformat})" if key.keyformat else ""))
        else:
            raise NotImplementedError(f"None of the key systems are supported: {', '.join(unsupported_systems)}")

    @staticmethod
    def get_drm(
        key: Union[m3u8.model.SessionKey, m3u8.model.Key],
        session: Optional[BaseHttpClient] = None,
    ) -> DRM_T:
        """
        Convert HLS EXT-X-KEY data to an initialized DRM object.

        Parameters:
            key: m3u8 key system (EXT-X-KEY) object.
            session: Optional session used to request AES-128 URIs.
                Useful to set headers, proxies, cookies, and so forth.

        Raises a NotImplementedError if the key system is not supported.
        """
        if not session:
            session = http_unshackle.new('hls')
        elif not isinstance(session, BaseHttpClient):
            raise TypeError(f"Expected session to be a {BaseHttpClient}, not {type(session)}")

        # TODO: Add support for 'SAMPLE-AES', 'AES-CTR', 'AES-CBC', 'ClearKey'
        if key.method == "AES-128":
            drm = ClearKey.from_m3u_key(key, session)
        elif key.method == "ISO-23001-7":
            drm = Widevine(pssh=WV_PSSH.new(key_ids=[key.uri.split(",")[-1]], system_id=WV_PSSH.SystemId.Widevine))
        elif key.keyformat and key.keyformat.lower() == WidevineCdm.urn:
            drm = Widevine(
                pssh=WV_PSSH(key.uri.split(",")[-1]),
                **key._extra_params,  # noqa
            )
        elif key.keyformat and key.keyformat.lower() in {f"urn:uuid:{PR_PSSH.SYSTEM_ID}", "com.microsoft.playready"}:
            drm = PlayReady(
                pssh=PR_PSSH(key.uri.split(",")[-1]),
                pssh_b64=key.uri.split(",")[-1],
            )
        else:
            raise NotImplementedError(f"The key system is not supported: {key}")

        return drm

    @staticmethod
    def get_all_drm(
        keys: list[Union[m3u8.model.SessionKey, m3u8.model.Key]], proxy: Optional[str] = None
    ) -> list[DRM_T]:
        """
        Convert HLS EXT-X-KEY data to initialized DRM objects.

        Parameters:
            keys: m3u8 key system (EXT-X-KEY) objects.
            proxy: Optional proxy string used for requesting AES-128 URIs.

        Raises a NotImplementedError if none of the key systems are supported.
        """
        unsupported_keys: list[m3u8.Key] = []
        drm_objects: list[DRM_T] = []

        if any(key.method == "NONE" for key in keys):
            return []

        for key in keys:
            try:
                drm = HLS.get_drm(key, proxy)
                drm_objects.append(drm)
            except NotImplementedError:
                unsupported_keys.append(key)

        if not drm_objects and unsupported_keys:
            logging.debug(
                "Ignoring unsupported key systems: %s",
                ", ".join([str(k.keyformat or k.method) for k in unsupported_keys]),
            )
            return []

        return drm_objects

    @staticmethod
    def calculate_byte_range(m3u_range: str, fallback_offset: int = 0) -> str:
        """
        Convert a HLS EXT-X-BYTERANGE value to a more traditional range value.
        E.g., '1433@0' -> '0-1432', '357392@1433' -> '1433-358824'.
        """
        parts = [int(x) for x in m3u_range.split("@")]
        if len(parts) != 2:
            parts.append(fallback_offset)
        length, offset = parts
        return f"{offset}-{offset + length - 1}"


__all__ = ("HLS",)
