from __future__ import annotations

import base64
import hashlib
import html
import logging
import re
import shutil
import struct
import urllib.parse
from typing import Any, Optional, Union

import requests
from langcodes import Language, tag_is_valid
from lxml.etree import Element
from pyplayready.system.pssh import PSSH as PR_PSSH
from pywidevine.pssh import PSSH
from requests import Session

from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED, DOWNLOAD_LICENCE_ONLY, AnyTrack
from unshackle.core.drm import DRM_T, PlayReady, Widevine
from unshackle.core.events import events
from unshackle.core.manifests.ism_init import (
    build_init_segment,
    parse_codec_private_data_vui,
    piff_senc_to_cenc,
    read_per_sample_iv_size,
    read_track_id,
)
from unshackle.core.session import RnetSession
from unshackle.core.tracks import Audio, DownloadContext, Subtitle, Track, Tracks, Video, resume
from unshackle.core.tracks.track import assert_fragments_decrypted
from unshackle.core.utilities import log_event, try_ensure_utf8
from unshackle.core.utils.redact import safe_display_url
from unshackle.core.utils.xml import load_xml

# MS-SSTR: FourCC may be absent; AudioTag carries the WAVE format tag instead.
AUDIO_TAG_FOURCC = {"255": "AACL", "65534": "EC-3"}

# Smooth FourCCs that Codec.from_mime (RFC 6381 names) doesn't know directly.
FOURCC_MIME = {"H264": "avc1", "H265": "hvc1", "HEVC": "hvc1", "AACL": "mp4a", "AACH": "mp4a", "AACP": "mp4a"}


class ISM:
    def __init__(self, manifest: Element, url: str) -> None:
        if manifest.tag != "SmoothStreamingMedia":
            raise TypeError(f"Expected 'SmoothStreamingMedia' document, got '{manifest.tag}'")
        if not url:
            raise requests.URLRequired("ISM manifest URL must be provided for relative paths")
        self.manifest = manifest
        self.url = url

    @classmethod
    def from_url(cls, url: str, session: Optional[Union[Session, RnetSession]] = None, **kwargs: Any) -> "ISM":
        if not url:
            raise requests.URLRequired("ISM manifest URL must be provided")
        if not session:
            session = Session()
        elif not isinstance(session, (Session, RnetSession)):
            raise TypeError(f"Expected session to be a {Session} or {RnetSession}, not {session!r}")
        res = session.get(url, **kwargs)
        if res.url != url:
            url = res.url
        res.raise_for_status()
        log_event(
            "manifest_ism_fetch",
            level="DEBUG",
            message=f"Fetched ISM manifest ({len(res.content)} bytes)",
            context={"url": safe_display_url(url), "size": len(res.content)},
        )
        return cls(load_xml(res.content), url)

    @classmethod
    def from_text(cls, text: str, url: str) -> "ISM":
        if not text:
            raise ValueError("ISM manifest text must be provided")
        if not url:
            raise requests.URLRequired("ISM manifest URL must be provided for relative paths")
        return cls(load_xml(text), url)

    @staticmethod
    def get_drm(headers: list[Element]) -> list[DRM_T]:
        drm: list[DRM_T] = []
        for header in headers:
            # MS-SSTR manifests often write the GUID in registry format ({...}).
            system_id = (header.get("SystemID") or header.get("SystemId") or "").strip().strip("{}").lower()
            data = "".join(header.itertext()).strip()
            if not data:
                continue
            if system_id == "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed":
                try:
                    pssh = PSSH(base64.b64decode(data))
                # a dropped header can leave the track with no DRM, so never skip silently
                except Exception as e:
                    logging.getLogger("ISM").warning(f"Skipping unparseable Widevine ProtectionHeader: {e!r}")
                    continue
                kid = next(iter(pssh.key_ids), None)
                drm.append(Widevine(pssh=pssh, kid=kid))
            elif system_id == "9a04f079-9840-4286-ab92-e65be0885f95":
                try:
                    pr_pssh = PR_PSSH(data)
                except Exception as e:
                    logging.getLogger("ISM").warning(f"Skipping unparseable PlayReady ProtectionHeader: {e!r}")
                    continue
                drm.append(PlayReady(pssh=pr_pssh, pssh_b64=data))
        return drm

    @staticmethod
    def get_video_range_and_fps(fourcc: str, codec_private_data: str) -> tuple[Video.Range, Optional[float]]:
        """Derive colour range and fps from the SPS VUI in CodecPrivateData,
        since Smooth manifests carry neither as attributes. Range soft-fails to
        SDR. fps is None for non-HEVC codecs and VUIs without timing info."""
        fourcc = (fourcc or "").upper()
        try:
            cpd = bytes.fromhex(codec_private_data or "")
        except ValueError:
            cpd = b""
        cicp, fps = parse_codec_private_data_vui(fourcc, cpd)
        if fourcc in ("DVHE", "DVH1"):
            return Video.Range.DV, fps
        if not cicp:
            return Video.Range.SDR, fps
        return Video.Range.from_cicp(*cicp), fps

    @staticmethod
    def get_video_range(fourcc: str, codec_private_data: str) -> Video.Range:
        return ISM.get_video_range_and_fps(fourcc, codec_private_data)[0]

    @staticmethod
    def init_segment(
        track: AnyTrack, session_drm: Optional[DRM_T], first_segment: Optional[bytes] = None
    ) -> Optional[bytes]:
        # Smooth fragments are moof+mdat only; rebuild the ftyp+moov init box from
        # the manifest CodecPrivateData (and KID, when encrypted) so the merged file
        # is a valid MP4 that shaka/mp4decrypt can parse.
        ism = track.data.get("ism") if isinstance(getattr(track, "data", None), dict) else None
        if not ism:
            return None
        stream_index = ism.get("stream_index")
        quality_level = ism.get("quality_level")
        manifest = ism.get("manifest")
        if stream_index is None or quality_level is None:
            return None
        # CodecPrivateData may legitimately be empty (AAC config is synthesized,
        # EC-3 decoders sync from the frames); the builder handles each case.
        cpd = quality_level.get("CodecPrivateData") or ""
        fourcc = quality_level.get("FourCC") or AUDIO_TAG_FOURCC.get(quality_level.get("AudioTag") or "") or ""

        root_timescale = manifest.get("TimeScale") if manifest is not None else None
        timescale = int(stream_index.get("TimeScale") or root_timescale or 10000000)
        duration = int((manifest.get("Duration") if manifest is not None else 0) or 0)
        # mdhd needs a 3-letter ISO-639-2 code; manifests often carry 2-letter tags.
        lang_attr = (stream_index.get("Language") or "").strip()
        language = "und"
        if lang_attr and tag_is_valid(lang_attr):
            try:
                language = Language.get(lang_attr).to_alpha3()
            except LookupError:
                language = "und"

        kid: Optional[bytes] = None
        if session_drm is not None:
            kid_uuid = next(iter(getattr(session_drm, "kids", None) or []), None)
            if kid_uuid is not None:
                kid = bytes.fromhex(kid_uuid.hex)

        # Match the moov track_ID to the fragment's tfhd, else the muxer drops samples.
        track_id = (read_track_id(first_segment) if first_segment else None) or 1
        # NALUnitLengthField: bytes per NAL length prefix, default 4.
        nal_length_size = int(quality_level.get("NALUnitLengthField") or stream_index.get("NALUnitLengthField") or 4)
        # Per-sample IV size derived from the fragment senc/saiz (PIFF default 8).
        iv_size = (read_per_sample_iv_size(first_segment) if first_segment and kid else None) or 8

        try:
            if isinstance(track, Subtitle):
                if track.codec != Subtitle.Codec.fTTML:
                    return None  # plain-text subtitle formats concatenate fine
                return build_init_segment(
                    stream_type="text",
                    fourcc="TTML",
                    codec_private_data="",
                    timescale=timescale,
                    duration=duration,
                    language=language,
                    track_id=track_id,
                )
            if isinstance(track, Video):
                return build_init_segment(
                    stream_type="video",
                    fourcc=fourcc,
                    codec_private_data=cpd,
                    timescale=timescale,
                    duration=duration,
                    language=language,
                    width=int(quality_level.get("MaxWidth") or stream_index.get("MaxWidth") or 0),
                    height=int(quality_level.get("MaxHeight") or stream_index.get("MaxHeight") or 0),
                    track_id=track_id,
                    nal_length_size=nal_length_size,
                    kid=kid,
                    iv_size=iv_size,
                )
            return build_init_segment(
                stream_type="audio",
                fourcc=fourcc,
                codec_private_data=cpd,
                timescale=timescale,
                duration=duration,
                language=language,
                channels=int(quality_level.get("Channels") or 2),
                bits_per_sample=int(quality_level.get("BitsPerSample") or 16),
                sampling_rate=int(quality_level.get("SamplingRate") or 48000),
                track_id=track_id,
                kid=kid,
                iv_size=iv_size,
            )
        except (NotImplementedError, ValueError, struct.error) as e:
            # Unsupported codec, malformed CodecPrivateData or out-of-range field:
            # fall back to raw concatenation rather than aborting the download.
            log_event(
                "manifest_ism_init_unsupported",
                level="WARNING",
                message=f"Could not synthesize ISM init segment ({fourcc}): {e}",
                context={"track_id": getattr(track, "id", None), "fourcc": fourcc},
            )
            return None

    def to_tracks(self, language: Optional[Union[str, Language]] = None) -> Tracks:
        if (self.manifest.get("IsLive") or "").upper() == "TRUE":
            raise ValueError("Live Smooth Streaming manifests are not supported")
        tracks = Tracks()
        base_url = self.url
        duration = int(self.manifest.get("Duration") or 0)
        drm = self.get_drm(self.manifest.xpath(".//ProtectionHeader"))

        for stream_index in self.manifest.findall("StreamIndex"):
            content_type = stream_index.get("Type")
            if not content_type:
                raise ValueError("No content type value could be found")
            for ql in stream_index.findall("QualityLevel"):
                codec = ql.get("FourCC") or AUDIO_TAG_FOURCC.get(ql.get("AudioTag") or "")
                if codec == "TTML":
                    codec = "STPP"
                track_lang = None
                lang = (stream_index.get("Language") or "").strip()
                if lang and tag_is_valid(lang) and not lang.startswith("und"):
                    track_lang = Language.get(lang)
                if not track_lang and not language:
                    # Language is optional in MS-SSTR; video streams commonly omit it.
                    raise ValueError(
                        "Language information could not be derived from the manifest and no fallback "
                        "language was provided when calling ISM.to_tracks()."
                    )

                track_urls: list[str] = []
                fragment_time = 0
                fragments = stream_index.findall("c")
                # MS-SSTR UrlPattern; regex over str.format so {Bitrate}/{start_time}
                # spellings work and unknown placeholders like {CustomAttributes}
                # or stray braces in query strings don't raise.
                url_template = urllib.parse.urljoin(
                    base_url,
                    re.sub(r"\{[Bb]itrate\}", str(ql.get("Bitrate") or 0), stream_index.get("Url") or ""),
                )
                # Some manifests omit the first fragment in the <c> list but
                # still expect a request for start time 0 which contains the
                # initialization segment. If the first declared fragment is not
                # at time 0, prepend the missing initialization URL.
                if fragments:
                    first_time = int(fragments[0].get("t") or 0)
                    if first_time != 0:
                        track_urls.append(re.sub(r"\{start[ _]time\}", "0", url_template))

                for idx, frag in enumerate(fragments):
                    fragment_time = int(frag.get("t", fragment_time))
                    repeat = int(frag.get("r", 1))
                    duration_frag = int(frag.get("d") or 0)
                    if not duration_frag:
                        try:
                            next_time = int(fragments[idx + 1].get("t"))
                        except (IndexError, TypeError):
                            # no next fragment, or the next <c> omits t (get returns None)
                            next_time = duration
                        # floor division: float times would corrupt segment URLs;
                        # any drift is reset by the next fragment's explicit t.
                        duration_frag = (next_time - fragment_time) // repeat
                    for _ in range(repeat):
                        track_urls.append(re.sub(r"\{start[ _]time\}", str(fragment_time), url_template))
                        fragment_time += duration_frag

                track_id = hashlib.md5(
                    "{codec}-{lang}-{bitrate}-{index}-{name}-{url}".format(
                        codec=codec,
                        lang=track_lang,
                        bitrate=ql.get("Bitrate") or 0,
                        index=ql.get("Index") or 0,
                        name=stream_index.get("Name") or "",
                        url=stream_index.get("Url") or "",
                    ).encode()
                ).hexdigest()

                data = {
                    "ism": {
                        "manifest": self.manifest,
                        "stream_index": stream_index,
                        "quality_level": ql,
                        "segments": track_urls,
                    }
                }

                if content_type == "video":
                    try:
                        vcodec = Video.Codec.from_mime(FOURCC_MIME.get(codec.upper(), codec)) if codec else None
                    except ValueError:
                        vcodec = None
                    range_, fps = self.get_video_range_and_fps(codec or "", ql.get("CodecPrivateData") or "")
                    tracks.add(
                        Video(
                            id_=track_id,
                            url=self.url,
                            codec=vcodec,
                            range_=range_,
                            fps=fps,
                            language=track_lang or language,
                            is_original_lang=bool(language and track_lang and str(track_lang) == str(language)),
                            bitrate=ql.get("Bitrate"),
                            # Width/Height are non-spec but common when Max* are absent
                            width=int(ql.get("MaxWidth") or ql.get("Width") or 0)
                            or int(stream_index.get("MaxWidth") or stream_index.get("Width") or 0),
                            height=int(ql.get("MaxHeight") or ql.get("Height") or 0)
                            or int(stream_index.get("MaxHeight") or stream_index.get("Height") or 0),
                            descriptor=Video.Descriptor.ISM,
                            drm=drm,
                            data=data,
                        )
                    )
                elif content_type == "audio":
                    try:
                        acodec = Audio.Codec.from_mime(FOURCC_MIME.get(codec.upper(), codec)) if codec else None
                    except ValueError:
                        acodec = None
                    tracks.add(
                        Audio(
                            id_=track_id,
                            url=self.url,
                            codec=acodec,
                            language=track_lang or language,
                            is_original_lang=bool(language and track_lang and str(track_lang) == str(language)),
                            bitrate=ql.get("Bitrate"),
                            channels=ql.get("Channels"),
                            descriptor=Track.Descriptor.ISM,
                            drm=drm,
                            data=data,
                        )
                    )
                else:
                    try:
                        scodec = Subtitle.Codec.from_mime(codec) if codec else None
                    except ValueError:
                        scodec = None
                    tracks.add(
                        Subtitle(
                            id_=track_id,
                            url=self.url,
                            codec=scodec,
                            language=track_lang or language,
                            is_original_lang=bool(language and track_lang and str(track_lang) == str(language)),
                            descriptor=Track.Descriptor.ISM,
                            drm=drm,
                            data=data,
                        )
                    )
        tracks.manifest_url = self.url

        log_event(
            "manifest_ism_parse",
            level="INFO",
            message=(
                f"Parsed ISM manifest: {len(tracks.videos)} video, "
                f"{len(tracks.audio)} audio, {len(tracks.subtitles)} subtitle track(s)"
            ),
            context={
                "videos": len(tracks.videos),
                "audio": len(tracks.audio),
                "subtitles": len(tracks.subtitles),
                "ranges": sorted({str(v.range) for v in tracks.videos}),
                "vcodecs": sorted({str(v.codec) for v in tracks.videos}),
            },
        )
        return tracks

    @staticmethod
    def download_track(track: AnyTrack, ctx: DownloadContext) -> None:
        session = ctx.ensure_session()
        save_path = ctx.save_path
        save_dir = ctx.save_dir
        progress = ctx.progress
        proxy = ctx.proxy
        max_workers = ctx.max_workers
        adaptive = ctx.adaptive_workers
        processes = ctx.download_processes
        license_widevine = ctx.license_widevine
        cdm = ctx.cdm

        if proxy:
            session.proxies.update({"all": proxy})

        segments: list[str] = track.data["ism"]["segments"]

        session_drm = None
        if track.drm:
            # Mirror HLS.download_track: pick the DRM matching the provided CDM
            # (or the first available) and license it if supported.
            session_drm = track.get_drm_for_cdm(cdm)
            if isinstance(session_drm, (Widevine, PlayReady)):
                try:
                    if not license_widevine:
                        raise ValueError("license_widevine func must be supplied to use DRM")
                    progress(downloaded="LICENSING")
                    license_widevine(session_drm)
                    progress(downloaded="[yellow]LICENSED")
                except Exception:
                    DOWNLOAD_CANCELLED.set()
                    progress(downloaded="[red]FAILED")
                    raise

        if DOWNLOAD_LICENCE_ONLY.is_set():
            progress(downloaded="[yellow]SKIPPED")
            return

        progress(total=len(segments))

        downloader = track.downloader

        digest = resume.fingerprint(track.url, [(url, None) for url in segments])
        if not (config.continue_downloads and resume.reusable(save_dir, digest)):
            shutil.rmtree(save_dir, ignore_errors=True)
        save_dir.mkdir(parents=True, exist_ok=True)
        if config.continue_downloads:
            resume.write_sidecar(save_dir, digest)

        downloader_args = dict(
            urls=[{"url": url} for url in segments],
            output_dir=save_dir,
            filename="{i:0%d}.mp4" % len(str(len(segments))),
            headers=session.headers,
            cookies=session.cookies,
            proxy=proxy,
            max_workers=max_workers,
            session=session,
            adaptive=adaptive,
            processes=processes,
        )

        log_event(
            "manifest_ism_download_start",
            level="DEBUG",
            message="Starting ISM manifest download",
            context={
                "track_id": getattr(track, "id", None),
                "track_type": track.__class__.__name__,
                "total_segments": len(segments),
                "downloader": "requests",
                "has_drm": bool(session_drm),
                "drm_type": session_drm.__class__.__name__ if session_drm else None,
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
                    status_update["downloaded"] = f"ISM {downloaded}"
                progress(**status_update)

        if not save_dir.exists():
            error_msg = f"Output directory does not exist: {save_dir}"
            log_event(
                "manifest_ism_download_output_missing",
                level="ERROR",
                message=error_msg,
                context={
                    "track_id": getattr(track, "id", None),
                    "track_type": track.__class__.__name__,
                    "save_dir": str(save_dir),
                    "save_path": str(save_path),
                    "downloader": "requests",
                },
            )
            raise FileNotFoundError(error_msg)

        for control_file in save_dir.glob("*.!dev"):
            control_file.unlink(missing_ok=True)

        segments_to_merge = [x for x in sorted(save_dir.iterdir()) if x.is_file()]

        log_event(
            "manifest_ism_download_complete",
            level="DEBUG",
            message="ISM download complete, preparing to merge",
            context={
                "track_id": getattr(track, "id", None),
                "track_type": track.__class__.__name__,
                "save_dir": str(save_dir),
                "save_dir_exists": save_dir.exists(),
                "segments_found": len(segments_to_merge),
                "segment_files": [f.name for f in segments_to_merge[:10]],
                "downloader": "requests",
            },
        )

        if not segments_to_merge:
            error_msg = f"No segment files found in output directory: {save_dir}"
            all_contents = list(save_dir.iterdir()) if save_dir.exists() else []
            log_event(
                "manifest_ism_download_no_segments",
                level="ERROR",
                message=error_msg,
                context={
                    "track_id": getattr(track, "id", None),
                    "track_type": track.__class__.__name__,
                    "save_dir": str(save_dir),
                    "directory_contents": [str(p) for p in all_contents],
                    "downloader": "requests",
                },
            )
            raise FileNotFoundError(error_msg)

        is_text_subtitle = (
            not session_drm
            and isinstance(track, Subtitle)
            and track.codec not in (Subtitle.Codec.fVTT, Subtitle.Codec.fTTML)
        )
        progress(downloaded="Merging", completed=0, total=len(segments_to_merge))
        with open(save_path, "wb") as f:
            first_segment = segments_to_merge[0].read_bytes() if segments_to_merge else None
            init_segment = ISM.init_segment(track, session_drm, first_segment)
            if init_segment:
                f.write(init_segment)
            iv_size = (read_per_sample_iv_size(first_segment) if session_drm and first_segment else None) or 8
            for index, segment_file in enumerate(segments_to_merge):
                if is_text_subtitle:
                    # first segment was already read for the init synthesis, reuse it
                    segment_data = first_segment if index == 0 and first_segment else segment_file.read_bytes()
                    segment_data = try_ensure_utf8(segment_data)
                    segment_data = (
                        segment_data.decode("utf8")
                        .replace("&lrm;", html.unescape("&lrm;"))
                        .replace("&rlm;", html.unescape("&rlm;"))
                        .encode("utf8")
                    )
                    f.write(segment_data)
                elif session_drm:
                    segment_data = first_segment if index == 0 and first_segment else segment_file.read_bytes()
                    f.write(piff_senc_to_cenc(segment_data, iv_size))
                elif index == 0 and first_segment:
                    f.write(first_segment)
                else:
                    with open(segment_file, "rb") as src:
                        shutil.copyfileobj(src, f, 1024 * 1024)
                segment_file.unlink()
                progress(advance=1)

        track.path = save_path
        events.emit(events.Types.TRACK_DOWNLOADED, track=track)

        if session_drm:
            progress(downloaded="Decrypting", completed=0, total=None)
            session_drm.decrypt(save_path)
            assert_fragments_decrypted(save_path)
            track.drm = None
            events.emit(events.Types.TRACK_DECRYPTED, track=track, drm=session_drm, segment=None)
            progress(downloaded="Decrypted", completed=100, total=100)

        try:
            save_dir.rmdir()
        except OSError:
            # a superseded hedge download may still drop a .!dev file here
            shutil.rmtree(save_dir, ignore_errors=True)
        resume.clear_sidecar(save_dir)
        progress(downloaded="Downloaded")


__all__ = ("ISM",)
