import base64
import html
import logging
import re
import shutil
import subprocess
from collections import defaultdict
from copy import copy
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union
from uuid import UUID
from zlib import crc32

from curl_cffi.requests import Session as CurlSession
from langcodes import Language
from requests import Session

from unshackle.core import binaries
from unshackle.core.cdm.detect import is_playready_cdm, is_widevine_cdm
from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED, DOWNLOAD_LICENCE_ONLY
from unshackle.core.downloaders import aria2c, curl_impersonate, n_m3u8dl_re, requests
from unshackle.core.drm import DRM_T, PlayReady, Widevine
from unshackle.core.events import events
from unshackle.core.utilities import get_boxes, get_extension, try_ensure_utf8
from unshackle.core.utils.subprocess import ffprobe


class Track:
    class Descriptor(Enum):
        URL = 1  # Direct URL, nothing fancy
        HLS = 2  # https://en.wikipedia.org/wiki/HTTP_Live_Streaming
        DASH = 3  # https://en.wikipedia.org/wiki/Dynamic_Adaptive_Streaming_over_HTTP
        ISM = 4  # https://learn.microsoft.com/en-us/silverlight/smooth-streaming

    def __init__(
        self,
        url: Union[str, list[str]],
        language: Union[Language, str],
        is_original_lang: bool = False,
        descriptor: Descriptor = Descriptor.URL,
        needs_repack: bool = False,
        name: Optional[str] = None,
        drm: Optional[Iterable[DRM_T]] = None,
        edition: Optional[str] = None,
        downloader: Optional[Callable] = None,
        downloader_args: Optional[dict] = None,
        from_file: Optional[Path] = None,
        data: Optional[Union[dict, defaultdict]] = None,
        id_: Optional[str] = None,
        extra: Optional[Any] = None,
    ) -> None:
        if not isinstance(url, (str, list)):
            raise TypeError(f"Expected url to be a {str}, or list of {str}, not {type(url)}")
        if not isinstance(language, (Language, str)):
            raise TypeError(f"Expected language to be a {Language} or {str}, not {type(language)}")
        if not isinstance(is_original_lang, bool):
            raise TypeError(f"Expected is_original_lang to be a {bool}, not {type(is_original_lang)}")
        if not isinstance(descriptor, Track.Descriptor):
            raise TypeError(f"Expected descriptor to be a {Track.Descriptor}, not {type(descriptor)}")
        if not isinstance(needs_repack, bool):
            raise TypeError(f"Expected needs_repack to be a {bool}, not {type(needs_repack)}")
        if not isinstance(name, (str, type(None))):
            raise TypeError(f"Expected name to be a {str}, not {type(name)}")
        if not isinstance(id_, (str, type(None))):
            raise TypeError(f"Expected id_ to be a {str}, not {type(id_)}")
        if not isinstance(edition, (str, type(None))):
            raise TypeError(f"Expected edition to be a {str}, not {type(edition)}")
        if not isinstance(downloader, (Callable, type(None))):
            raise TypeError(f"Expected downloader to be a {Callable}, not {type(downloader)}")
        if not isinstance(downloader_args, (dict, type(None))):
            raise TypeError(f"Expected downloader_args to be a {dict}, not {type(downloader_args)}")
        if not isinstance(from_file, (Path, type(None))):
            raise TypeError(f"Expected from_file to be a {Path}, not {type(from_file)}")
        if not isinstance(data, (dict, defaultdict, type(None))):
            raise TypeError(f"Expected data to be a {dict} or {defaultdict}, not {type(data)}")

        invalid_urls = ", ".join(set(type(x) for x in url if not isinstance(x, str)))
        if invalid_urls:
            raise TypeError(f"Expected all items in url to be a {str}, but found {invalid_urls}")

        if drm is not None:
            try:
                iter(drm)
            except TypeError:
                raise TypeError(f"Expected drm to be an iterable, not {type(drm)}")

        if downloader is None:
            downloader = {
                "aria2c": aria2c,
                "curl_impersonate": curl_impersonate,
                "requests": requests,
                "n_m3u8dl_re": n_m3u8dl_re,
            }[config.downloader]

        self.path: Optional[Path] = None
        self.url = url
        self.language = Language.get(language)
        self.is_original_lang = is_original_lang
        self.descriptor = descriptor
        self.needs_repack = needs_repack
        self.name = name
        self.drm = drm
        self.edition: str = edition
        self.downloader = downloader
        self.downloader_args = downloader_args
        self.from_file = from_file
        self._data: defaultdict[Any, Any] = defaultdict(dict)
        self.data = data or {}
        self.extra: Any = extra or {}  # allow anything for extra, but default to a dict

        if self.name is None:
            lang = Language.get(self.language)
            if (lang.language or "").lower() == (lang.territory or "").lower():
                lang.territory = None  # e.g. en-en, de-DE
            reduced = lang.simplify_script()
            extra_parts = []
            if reduced.script is not None:
                script = reduced.script_name(max_distance=25)
                if script and script != "Zzzz":
                    extra_parts.append(script)
            if reduced.territory is not None:
                territory = reduced.territory_name(max_distance=25)
                if territory and territory != "ZZ":
                    territory = territory.removesuffix(" SAR China")
                    extra_parts.append(territory)
            self.name = ", ".join(extra_parts) or None

        if not id_:
            this = copy(self)
            this.url = self.url.rsplit("?", maxsplit=1)[0]
            checksum = crc32(repr(this).encode("utf8"))
            id_ = hex(checksum)[2:]

        self.id = id_

        # TODO: Currently using OnFoo event naming, change to just segment_filter
        self.OnSegmentFilter: Optional[Callable] = None

    def __repr__(self) -> str:
        return "{name}({items})".format(
            name=self.__class__.__name__, items=", ".join([f"{k}={repr(v)}" for k, v in self.__dict__.items()])
        )

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Track) and self.id == other.id

    @property
    def data(self) -> defaultdict[Any, Any]:
        """
        Arbitrary track data dictionary.

        A defaultdict is used with a dict as the factory for easier
        nested saving and safer exists-checks.

        Reserved keys:

        - "hls" used by the HLS class.
          - playlist: m3u8.model.Playlist - The primary track information.
          - media: m3u8.model.Media - The audio/subtitle track information.
          - segment_durations: list[int] - A list of each segment's duration.
        - "dash" used by the DASH class.
          - manifest: lxml.ElementTree - DASH MPD manifest.
          - period: lxml.Element - The period of this track.
          - adaptation_set: lxml.Element - The adaptation set of this track.
          - representation: lxml.Element - The representation of this track.
          - timescale: int - The timescale of the track's segments.
          - segment_durations: list[int] - A list of each segment's duration.

        You should not add, change, or remove any data within reserved keys.
        You may use their data but do note that the values of them may change
        or be removed at any point.
        """
        return self._data

    @data.setter
    def data(self, value: Union[dict, defaultdict]) -> None:
        if not isinstance(value, (dict, defaultdict)):
            raise TypeError(f"Expected data to be a {dict} or {defaultdict}, not {type(value)}")
        if isinstance(value, dict):
            value = defaultdict(dict, **value)
        self._data = value

    def download(
        self,
        session: Session,
        prepare_drm: partial,
        max_workers: Optional[int] = None,
        progress: Optional[partial] = None,
        *,
        cdm: Optional[object] = None,
    ):
        """Download and optionally Decrypt this Track."""
        from unshackle.core.manifests import DASH, HLS, ISM

        if DOWNLOAD_LICENCE_ONLY.is_set():
            progress(downloaded="[yellow]SKIPPING")

        if DOWNLOAD_CANCELLED.is_set():
            progress(downloaded="[yellow]SKIPPED")
            return

        log = logging.getLogger("track")

        proxy = next(iter(session.proxies.values()), None)

        track_type = self.__class__.__name__
        save_path = config.directories.temp / f"{track_type}_{self.id}.mp4"
        if track_type == "Subtitle":
            save_path = save_path.with_suffix(f".{self.codec.extension}")
            # n_m3u8dl_re doesn't support directly downloading subtitles from URLs
            # or when the subtitle has a direct file extension
            if self.downloader.__name__ == "n_m3u8dl_re" and (
                self.descriptor == self.Descriptor.URL
                or get_extension(self.url)
                in {
                    ".srt",
                    ".vtt",
                    ".ttml",
                    ".ssa",
                    ".ass",
                    ".stpp",
                    ".wvtt",
                    ".xml",
                }
            ):
                self.downloader = requests

        if self.descriptor != self.Descriptor.URL:
            save_dir = save_path.with_name(save_path.name + "_segments")
        else:
            save_dir = save_path.parent

        def cleanup():
            # track file (e.g., "foo.mp4")
            save_path.unlink(missing_ok=True)
            # aria2c control file (e.g., "foo.mp4.aria2" or "foo.mp4.aria2__temp")
            save_path.with_suffix(f"{save_path.suffix}.aria2").unlink(missing_ok=True)
            save_path.with_suffix(f"{save_path.suffix}.aria2__temp").unlink(missing_ok=True)
            if save_dir.exists() and save_dir.name.endswith("_segments"):
                shutil.rmtree(save_dir)

        if not DOWNLOAD_LICENCE_ONLY.is_set():
            if config.directories.temp.is_file():
                raise ValueError(f"Temp Directory '{config.directories.temp}' must be a Directory, not a file")

            config.directories.temp.mkdir(parents=True, exist_ok=True)

            # Delete any pre-existing temp files matching this track.
            # We can't re-use or continue downloading these tracks as they do not use a
            # lock file. Or at least the majority don't. Even if they did I've encountered
            # corruptions caused by sudden interruptions to the lock file.
            cleanup()

        try:
            if self.descriptor == self.Descriptor.HLS:
                HLS.download_track(
                    track=self,
                    save_path=save_path,
                    save_dir=save_dir,
                    progress=progress,
                    session=session,
                    proxy=proxy,
                    max_workers=max_workers,
                    license_widevine=prepare_drm,
                    cdm=cdm,
                )
            elif self.descriptor == self.Descriptor.DASH:
                DASH.download_track(
                    track=self,
                    save_path=save_path,
                    save_dir=save_dir,
                    progress=progress,
                    session=session,
                    proxy=proxy,
                    max_workers=max_workers,
                    license_widevine=prepare_drm,
                    cdm=cdm,
                )
            elif self.descriptor == self.Descriptor.ISM:
                ISM.download_track(
                    track=self,
                    save_path=save_path,
                    save_dir=save_dir,
                    progress=progress,
                    session=session,
                    proxy=proxy,
                    max_workers=max_workers,
                    license_widevine=prepare_drm,
                    cdm=cdm,
                )
            elif self.descriptor == self.Descriptor.URL:
                try:
                    if not self.drm and track_type in ("Video", "Audio"):
                        # the service might not have explicitly defined the `drm` property
                        # try find DRM information from the init data of URL based on CDM type
                        if is_playready_cdm(cdm):
                            try:
                                self.drm = [PlayReady.from_track(self, session)]
                            except PlayReady.Exceptions.PSSHNotFound:
                                try:
                                    self.drm = [Widevine.from_track(self, session)]
                                except Widevine.Exceptions.PSSHNotFound:
                                    log.warning(
                                        "No PlayReady or Widevine PSSH was found for this track, is it DRM free?"
                                    )
                        else:
                            try:
                                self.drm = [Widevine.from_track(self, session)]
                            except Widevine.Exceptions.PSSHNotFound:
                                try:
                                    self.drm = [PlayReady.from_track(self, session)]
                                except PlayReady.Exceptions.PSSHNotFound:
                                    log.warning(
                                        "No Widevine or PlayReady PSSH was found for this track, is it DRM free?"
                                    )

                    if self.drm:
                        track_kid = self.get_key_id(session=session)
                        drm = self.get_drm_for_cdm(cdm)
                        if isinstance(drm, Widevine):
                            # license and grab content keys
                            if not prepare_drm:
                                raise ValueError("prepare_drm func must be supplied to use Widevine DRM")
                            progress(downloaded="LICENSING")
                            prepare_drm(drm, track_kid=track_kid)
                            progress(downloaded="[yellow]LICENSED")
                        elif isinstance(drm, PlayReady):
                            # license and grab content keys
                            if not prepare_drm:
                                raise ValueError("prepare_drm func must be supplied to use PlayReady DRM")
                            progress(downloaded="LICENSING")
                            prepare_drm(drm, track_kid=track_kid)
                            progress(downloaded="[yellow]LICENSED")
                    else:
                        drm = None

                    if DOWNLOAD_LICENCE_ONLY.is_set():
                        progress(downloaded="[yellow]SKIPPED")
                    elif track_type != "Subtitle" and self.downloader.__name__ == "n_m3u8dl_re":
                        progress(downloaded="[red]FAILED")
                        error = f"[N_m3u8DL-RE]: {self.descriptor} is currently not supported"
                        raise ValueError(error)
                    else:
                        for status_update in self.downloader(
                            urls=self.url,
                            output_dir=save_path.parent,
                            filename=save_path.name,
                            headers=session.headers,
                            cookies=session.cookies,
                            proxy=proxy,
                            max_workers=max_workers,
                        ):
                            file_downloaded = status_update.get("file_downloaded")
                            if not file_downloaded:
                                progress(**status_update)

                        # see https://github.com/devine-dl/devine/issues/71
                        save_path.with_suffix(f"{save_path.suffix}.aria2__temp").unlink(missing_ok=True)

                        self.path = save_path
                        events.emit(events.Types.TRACK_DOWNLOADED, track=self)

                        if drm:
                            progress(downloaded="Decrypting", completed=0, total=100)
                            drm.decrypt(save_path)
                            self.drm = None
                            events.emit(events.Types.TRACK_DECRYPTED, track=self, drm=drm, segment=None)
                            progress(downloaded="Decrypted", completed=100)

                        if track_type == "Subtitle" and self.codec.name not in ("fVTT", "fTTML"):
                            track_data = self.path.read_bytes()
                            track_data = try_ensure_utf8(track_data)
                            track_data = (
                                track_data.decode("utf8")
                                .replace("&lrm;", html.unescape("&lrm;"))
                                .replace("&rlm;", html.unescape("&rlm;"))
                                .encode("utf8")
                            )
                            self.path.write_bytes(track_data)

                        progress(downloaded="Downloaded")
                except KeyboardInterrupt:
                    DOWNLOAD_CANCELLED.set()
                    progress(downloaded="[yellow]CANCELLED")
                    raise
                except Exception:
                    DOWNLOAD_CANCELLED.set()
                    progress(downloaded="[red]FAILED")
                    raise
        except (Exception, KeyboardInterrupt):
            if not DOWNLOAD_LICENCE_ONLY.is_set():
                cleanup()
            raise

        if DOWNLOAD_CANCELLED.is_set():
            # we stopped during the download, let's exit
            return

        if not DOWNLOAD_LICENCE_ONLY.is_set():
            if self.path.stat().st_size <= 3:  # Empty UTF-8 BOM == 3 bytes
                raise IOError("Download failed, the downloaded file is empty.")

        events.emit(events.Types.TRACK_DOWNLOADED, track=self)

    def delete(self) -> None:
        if self.path:
            self.path.unlink()
            self.path = None

    def move(self, target: Union[Path, str]) -> Path:
        """
        Move the Track's file from current location, to target location.
        This will overwrite anything at the target path.

        Raises:
            TypeError: If the target argument is not the expected type.
            ValueError: If track has no file to move, or the target does not exist.
            OSError: If the file somehow failed to move.

        Returns the new location of the track.
        """
        if not isinstance(target, (str, Path)):
            raise TypeError(f"Expected {target} to be a {Path} or {str}, not {type(target)}")

        if not self.path:
            raise ValueError("Track has no file to move")

        if not isinstance(target, Path):
            target = Path(target)

        if not target.exists():
            raise ValueError(f"Target file {repr(target)} does not exist")

        moved_to = Path(shutil.move(self.path, target))
        if moved_to.resolve() != target.resolve():
            raise OSError(f"Failed to move {self.path} to {target}")

        self.path = target
        return target

    def get_track_name(self) -> Optional[str]:
        """Get the Track Name."""
        return self.name

    def get_drm_for_cdm(self, cdm: Optional[object]) -> Optional[DRM_T]:
        """Return the DRM matching the provided CDM, if available."""
        if not self.drm:
            return None

        if is_widevine_cdm(cdm):
            for drm in self.drm:
                if isinstance(drm, Widevine):
                    return drm
        elif is_playready_cdm(cdm):
            for drm in self.drm:
                if isinstance(drm, PlayReady):
                    return drm

        return self.drm[0]

    def get_key_id(self, init_data: Optional[bytes] = None, *args, **kwargs) -> Optional[UUID]:
        """
        Probe the DRM encryption Key ID (KID) for this specific track.

        It currently supports finding the Key ID by probing the track's stream
        with ffprobe for `enc_key_id` data, as well as for mp4 `tenc` (Track
        Encryption) boxes.

        It explicitly ignores PSSH information like the `PSSH` box, as the box
        is likely to contain multiple Key IDs that may or may not be for this
        specific track.

        To retrieve the initialization segment, this method calls :meth:`get_init_segment`
        with the positional and keyword arguments. The return value of `get_init_segment`
        is then used to determine the Key ID.

        Returns:
            The Key ID as a UUID object, or None if the Key ID could not be determined.
        """
        if not init_data:
            init_data = self.get_init_segment(*args, **kwargs)
        if not isinstance(init_data, bytes):
            raise TypeError(f"Expected init_data to be bytes, not {init_data!r}")

        probe = ffprobe(init_data)
        if probe:
            for stream in probe.get("streams") or []:
                enc_key_id = stream.get("tags", {}).get("enc_key_id")
                if enc_key_id:
                    return UUID(bytes=base64.b64decode(enc_key_id))

        for tenc in get_boxes(init_data, b"tenc"):
            if tenc.key_ID.int != 0:
                return tenc.key_ID

        for uuid_box in get_boxes(init_data, b"uuid"):
            if uuid_box.extended_type == UUID("8974dbce-7be7-4c51-84f9-7148f9882554"):  # tenc
                tenc = uuid_box.data
                if tenc.key_ID.int != 0:
                    return tenc.key_ID

    def load_drm_if_needed(self, service=None) -> bool:
        """
        Load DRM information for this track if it was deferred during parsing.

        Args:
            service: Service instance that can fetch track-specific DRM info

        Returns:
            True if DRM was loaded or already present, False if failed
        """
        if not getattr(self, "needs_drm_loading", False):
            return bool(self.drm)

        if self.drm:
            self.needs_drm_loading = False
            return True

        if not service or not hasattr(service, "get_track_drm"):
            return self.load_drm_from_playlist()

        try:
            track_drm = service.get_track_drm(self)
            if track_drm:
                self.drm = track_drm if isinstance(track_drm, list) else [track_drm]
                self.needs_drm_loading = False
                return True
        except Exception as e:
            raise ValueError(f"Failed to load DRM from service for track {self.id}: {e}")

        return self.load_drm_from_playlist()

    def load_drm_from_playlist(self) -> bool:
        """
        Fallback method to load DRM by fetching this track's individual playlist.
        """
        if self.drm:
            self.needs_drm_loading = False
            return True

        try:
            import m3u8
            from pyplayready.system.pssh import PSSH as PR_PSSH
            from pywidevine.cdm import Cdm as WidevineCdm
            from pywidevine.pssh import PSSH as WV_PSSH

            session = getattr(self, "session", None) or Session()

            response = session.get(self.url)
            playlist = m3u8.loads(response.text, self.url)

            drm_list = []

            for key in playlist.keys or []:
                if not key or not key.keyformat:
                    continue

                fmt = key.keyformat.lower()
                if fmt == WidevineCdm.urn:
                    pssh_b64 = key.uri.split(",")[-1]
                    drm = Widevine(pssh=WV_PSSH(pssh_b64))
                    drm_list.append(drm)
                elif fmt in {f"urn:uuid:{PR_PSSH.SYSTEM_ID}", "com.microsoft.playready"}:
                    pssh_b64 = key.uri.split(",")[-1]
                    drm = PlayReady(pssh=PR_PSSH(pssh_b64), pssh_b64=pssh_b64)
                    drm_list.append(drm)

            if drm_list:
                self.drm = drm_list
                self.needs_drm_loading = False
                return True

        except Exception as e:
            raise ValueError(f"Failed to load DRM from playlist for track {self.id}: {e}")

        return False

    def get_init_segment(
        self,
        maximum_size: int = 20000,
        url: Optional[str] = None,
        byte_range: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> bytes:
        """
        Get the Track's Initial Segment Data Stream.

        HLS and DASH tracks must explicitly provide a URL to the init segment or file.
        Providing the byte-range for the init segment is recommended where possible.

        If `byte_range` is not set, it will make a HEAD request and check the size of
        the file. If the size could not be determined, it will download up to the first
        20KB only, which should contain the entirety of the init segment. You may
        override this by changing the `maximum_size`.

        The default maximum_size of 20000 (20KB) is a tried-and-tested value that
        seems to work well across the board.

        Parameters:
            maximum_size: Size to assume as the content length if byte-range is not
                used, the content size could not be determined, or the content size
                is larger than it. A value of 20000 (20KB) or higher is recommended.
            url: Explicit init map or file URL to probe from.
            byte_range: Range of bytes to download from the explicit or implicit URL.
            session: Session context, e.g., authorization and headers.
        """
        if not isinstance(maximum_size, int):
            raise TypeError(f"Expected maximum_size to be an {int}, not {type(maximum_size)}")
        if not isinstance(url, (str, type(None))):
            raise TypeError(f"Expected url to be a {str}, not {type(url)}")
        if not isinstance(byte_range, (str, type(None))):
            raise TypeError(f"Expected byte_range to be a {str}, not {type(byte_range)}")
        if not isinstance(session, (Session, CurlSession, type(None))):
            raise TypeError(f"Expected session to be a {Session} or {CurlSession}, not {type(session)}")

        if not url:
            if self.descriptor != self.Descriptor.URL:
                raise ValueError(f"An explicit URL must be provided for {self.descriptor.name} tracks")
            if not self.url:
                raise ValueError("An explicit URL must be provided as the track has no URL")
            url = self.url

        if not session:
            session = Session()

        content_length = maximum_size

        if byte_range:
            if not isinstance(byte_range, str):
                raise TypeError(f"Expected byte_range to be a str, not {byte_range!r}")
            if not re.match(r"^\d+-\d+$", byte_range):
                raise ValueError(f"The value of byte_range is unrecognized: '{byte_range}'")
            start, end = byte_range.split("-")
            if start > end:
                raise ValueError(f"The start range cannot be greater than the end range: {start}>{end}")
        else:
            size_test = session.head(url)
            if "Content-Length" in size_test.headers:
                content_length_header = int(size_test.headers["Content-Length"])
                if content_length_header > 0:
                    content_length = min(content_length_header, maximum_size)
            range_test = session.head(url, headers={"Range": "bytes=0-1"})
            if range_test.status_code == 206:
                byte_range = f"0-{content_length - 1}"

        if byte_range:
            res = session.get(url=url, headers={"Range": f"bytes={byte_range}"})
            res.raise_for_status()
            init_data = res.content
        else:
            init_data = None
            with session.get(url, stream=True) as s:
                for chunk in s.iter_content(content_length):
                    init_data = chunk
                    break
            if not init_data:
                raise ValueError(f"Failed to read {content_length} bytes from the track URI.")

        return init_data
    
    def repackage(self) -> None:
        if not self.path or not self.path.exists():
            raise ValueError("Cannot repackage a Track that has not been downloaded.")

        if not binaries.FFMPEG:
            raise EnvironmentError('FFmpeg executable "ffmpeg" was not found but is required for this call.')

        dovi_tool = shutil.which("dovi_tool")
        original_path = self.path
        output_path = original_path.with_stem(f"{original_path.stem}_repack")
        
        is_dovi = False
        rpu_path = original_path.with_suffix(".rpu")

        # Detect Dolby Vision (ffprobe)
        if dovi_tool:
            probe_data = ffprobe(original_path)
            if probe_data:
                for stream in probe_data.get("streams", []):
                    if stream.get("codec_type") == "video":
                        side_data_list = stream.get("side_data_list", [])
                        for side_data in side_data_list:
                            if "DOVI" in side_data.get("side_data_type", ""):
                                is_dovi = True
                                break
                    if is_dovi:
                        break

        # Extract RPU
        if is_dovi:
            try:
                subprocess.run(
                    [binaries.DoviTool, "extract-rpu", "-i", str(original_path), "-o", str(rpu_path)],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except subprocess.CalledProcessError:
                # if fali
                is_dovi = False
                if rpu_path.exists():
                    rpu_path.unlink()

        def _ffmpeg(extra_args: list[str] = None):
            args = [
                binaries.FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                original_path,
                *(extra_args or []),
            ]

            if hasattr(self, "data") and self.data.get("audio_language"):
                audio_lang = self.data["audio_language"]
                audio_name = self.data.get("audio_language_name", audio_lang)
                args.extend(
                    [
                        "-metadata:s:a:0",
                        f"language={audio_lang}",
                        "-metadata:s:a:0",
                        f"title={audio_name}",
                        "-metadata:s:a:0",
                        f"handler_name={audio_name}",
                    ]
                )

            args.extend(
                [
                    # Following are very important!
                    "-map_metadata",
                    "-1",  # don't transfer metadata to output file
                    "-fflags",
                    "bitexact",  # only have minimal tag data, reproducible mux
                    "-codec",
                    "copy",
                    str(output_path),
                ]
            )

            subprocess.run(
                args,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        try:
            _ffmpeg()
        except subprocess.CalledProcessError as e:
            if b"Malformed AAC bitstream detected" in e.stderr:
                # e.g., TruTV's dodgy encodes
                _ffmpeg(["-y", "-bsf:a", "aac_adtstoasc"])
            else:
                if is_dovi and rpu_path.exists():
                    rpu_path.unlink()
                raise

        # Inject RPU
        if is_dovi and rpu_path.exists():
            injected_output = output_path.with_name(f"{output_path.stem}_injected.mp4")
            try:
                subprocess.run(
                    [
                        binaries.DoviTool, 
                        "inject-rpu", 
                        "-i", str(output_path), 
                        "--rpu-in", str(rpu_path), 
                        "-o", str(injected_output)
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                
                output_path.unlink()
                injected_output.rename(output_path)
                
            except subprocess.CalledProcessError:
                if injected_output.exists():
                    injected_output.unlink()
            finally:
                # Delete temp RPU
                rpu_path.unlink(missing_ok=True)

        original_path.unlink()
        self.path = output_path


__all__ = ("Track",)
