import base64
import html
import logging
import re
import shutil
import subprocess
from collections import defaultdict
from copy import copy
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union
from uuid import UUID
from zlib import crc32

from langcodes import Language
from requests import Response, Session
from requests.adapters import HTTPAdapter, Retry

from unshackle.core import binaries
from unshackle.core.cdm.detect import is_playready_cdm, is_widevine_cdm
from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED, DOWNLOAD_LICENCE_ONLY
from unshackle.core.downloaders import requests
from unshackle.core.drm import DRM_T, ClearKeyCENC, PlayReady, Widevine
from unshackle.core.events import events
from unshackle.core.session import RnetSession
from unshackle.core.tracks import resume
from unshackle.core.utilities import get_boxes, log_event, try_ensure_utf8
from unshackle.core.utils.subprocess import ffprobe

DRM_PREFERENCE_TYPES: dict[str, Union[type[Widevine], type[PlayReady]]] = {
    "wv": Widevine,
    "widevine": Widevine,
    "pr": PlayReady,
    "playready": PlayReady,
}


def direct_session(session: Union[Session, "RnetSession"]) -> Session:
    """requests.Session with copied headers/cookies and no proxy."""
    new = Session()
    headers = getattr(session, "headers", None)
    if headers is not None:
        try:
            new.headers.update(dict(headers))
        except Exception:
            pass
    cookies = getattr(session, "cookies", None)
    if cookies is not None:
        jar = getattr(cookies, "jar", None)
        try:
            new.cookies.update(jar if jar is not None else cookies)
        except Exception:
            pass
    new.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(total=5, backoff_factor=0.2, status_forcelist=[429, 500, 502, 503, 504]),
            pool_maxsize=64,
            pool_block=True,
        ),
    )
    new.mount("http://", new.adapters["https://"])
    return new


def read_top_level_box(path: Path, box_type: bytes) -> Optional[bytes]:
    """Read a single top-level box from an MP4 file.

    This function walks only the box headers, so the wanted box is the only payload it reads into memory.
    Returns None if the file has no such top-level box, or if a box header is truncated or declares an
    impossible size.
    """
    file_size = path.stat().st_size
    with path.open("rb") as f:
        start = 0
        while start + 8 <= file_size:
            header = f.read(8)
            if len(header) < 8:
                return None
            size = int.from_bytes(header[:4], "big")
            if size == 1:
                large = f.read(8)
                if len(large) < 8:
                    return None
                size = int.from_bytes(large, "big")
            elif size == 0:
                size = file_size - start
            if size < 8:
                return None
            if header[4:8] == box_type:
                f.seek(start)
                return f.read(size)
            start += size
            f.seek(start)
    return None


def has_encrypted_sample_entry(path: Path) -> bool:
    """True if the MP4's moov still carries an encrypted sample entry (encv/enca).

    A faithful decrypt rewrites encv/enca back to the real codec 4CC through frma, so a
    survivor means the decrypt tool skipped/failed the track. This is a 4CC scan
    scoped to the moov box (where sample entries live), not a structural parse.
    The scoping avoids chance byte collisions in mdat. Any read error -> False.
    """
    try:
        moov = read_top_level_box(path, b"moov")
    except OSError:
        return False
    if not moov:
        return False
    for tok in (b"encv", b"enca"):
        i = moov.find(tok, 4)
        while i != -1:
            # require a plausible sample-entry box size right before the 4CC
            size = int.from_bytes(moov[i - 4 : i], "big")
            if 16 <= size <= len(moov) - (i - 4):
                return True
            i = moov.find(tok, i + 1)
    return False


def senc_protects_samples(buf: bytes, body: int, end: int) -> bool:
    """True if a senc/PIFF-uuid box describes protected samples.

    An empty senc (sample_count 0) sits on a genuinely clear fragment, so treating it as
    a skipped one would raise on a healthy file. Field order follows
    read_per_sample_iv_size.
    """
    flags = int.from_bytes(buf[body + 1 : body + 4], "big")
    pos = body + 4 + (20 if flags & 0x1 else 0)  # PIFF override: AlgorithmID(3)+IV_size(1)+KID(16)
    if pos + 4 > end:
        return False
    return int.from_bytes(buf[pos : pos + 4], "big") > 0


def moof_still_encrypted(moof: bytes) -> bool:
    # senc only: a decrypter detaches just the atom it consumed, so a PIFF uuid can
    # outlive a good decrypt. ISM arrives as senc via piff_senc_to_cenc.
    # structural walk: a 4CC byte scan collides with trun payload
    from unshackle.core.manifests.ism_init import iter_boxes

    body = 16 if int.from_bytes(moof[:4], "big") == 1 else 8
    for box_type, _, traf_body, traf_end in iter_boxes(moof, body, len(moof)):
        if box_type != b"traf":
            continue
        for child, _usertype, child_body, child_end in iter_boxes(moof, traf_body, traf_end):
            if child == b"senc" and senc_protects_samples(moof, child_body, child_end):
                return True
    return False


def assert_fragments_decrypted(path: Path) -> None:
    """Raise if any fragment survived decryption still carrying a sample-encryption box.

    mp4decrypt strips senc from every fragment it processes and leaves it on the ones it
    skips, so a survivor marks a silent skip. One cause is a tfhd.sample_description_index
    dangling past the stsd entry count, which mp4decrypt reports as success (exit 0, no
    stderr) while leaving the payload as ciphertext. Unlike has_encrypted_sample_entry
    (moov-scoped) this is fragment-scoped. The moov comes out clean in that failure.

    Only a standard senc counts as evidence. Some DASH titles ship a PIFF uuid beside it,
    and a decrypter detaches only the atom it consumed, so that uuid outlives a good
    decrypt. Counting it would condemn a healthy file.

    The guarantee runs in one direction: a survivor proves a skip, while a clean pass is
    only as good as the decrypter's own behaviour. shaka-packager (the default
    `decryption` backend) remuxes into a single fragment and drops senc whether or not it
    decrypted, so this cannot fire on shaka output. The walk does not use a buffer and reads
    only box headers. It seeks over mdat and never reads it. A malformed box size aborts the
    walk with a warning and returns normally, so a file this function cannot parse is
    never escalated into a raise. The logged warning is the only signal of that. A caller
    sees the same silent return it gets from a verified clean file.
    """
    surviving = 0
    total = 0
    first_offset = None
    try:
        file_size = path.stat().st_size
        with path.open("rb", buffering=0) as f:
            start = 0
            while start + 8 <= file_size:
                f.seek(start)
                header = f.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[:4], "big")
                box_header = 8
                if size == 1:
                    large = f.read(8)
                    if len(large) < 8:
                        break
                    size = int.from_bytes(large, "big")
                    box_header = 16
                elif size == 0:
                    size = file_size - start
                if size < box_header or start + size > file_size:
                    logging.getLogger("track").warning(
                        f"{path.name}: malformed box size at offset {start}; cannot verify the "
                        "remaining fragments were decrypted."
                    )
                    break
                if header[4:8] == b"moof":
                    total += 1
                    f.seek(start)
                    if moof_still_encrypted(f.read(size)):
                        surviving += 1
                        if first_offset is None:
                            first_offset = start
                start += size
    except Exception as e:
        logging.getLogger("track").warning(f"{path.name}: fragment decryption check did not complete ({e}).")
        return
    if surviving:
        log_event(
            "decrypt_fragments_still_encrypted",
            level="ERROR",
            message=f"{surviving}/{total} fragments still encrypted after decryption",
            file=path.name,
            surviving=surviving,
            total=total,
            first_offset=first_offset,
        )
        raise ValueError(
            f"{path.name}: {surviving}/{total} fragment(s) still encrypted after decryption (first at "
            f"byte {first_offset}). The decrypt tool skipped them silently, so check "
            f"tfhd.sample_description_index against the stsd entry count in the init segment."
        )


@dataclass
class DownloadContext:
    """Shared arguments passed to each manifest's ``download_track``."""

    save_path: Path
    save_dir: Path
    progress: partial
    session: Optional[Union[Session, "RnetSession"]] = None
    proxy: Optional[str] = None
    max_workers: Optional[int] = None
    adaptive_workers: bool = False
    download_processes: int = 1
    license_widevine: Optional[Callable] = None
    cdm: Optional[object] = None

    def ensure_session(self) -> Union[Session, "RnetSession"]:
        """Return the HTTP session, or a new ``Session`` if none was set."""
        session = self.session
        if not session:
            session = Session()
        elif not isinstance(session, (Session, RnetSession)):
            raise TypeError(f"Expected session to be a {Session} or {RnetSession}, not {session!r}")
        return session


class Track:
    class Descriptor(Enum):
        URL = 1
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
        session: Optional[Union[Session, "RnetSession"]] = None,
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
        if not isinstance(edition, (str, list, type(None))):
            raise TypeError(f"Expected edition to be a {str}, {list}, or None, not {type(edition)}")
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
            downloader = requests

        self.path: Optional[Path] = None
        self.url = url
        self.language = Language.get(language)
        self.is_original_lang = is_original_lang
        self.descriptor = descriptor
        self.needs_repack = needs_repack
        self.name = name
        self.drm = drm
        self._drm_preference: Optional[str] = None
        self.edition: list[str] = [edition] if isinstance(edition, str) else (edition or [])
        self.session = session
        self.downloader = downloader
        self.downloader_args = downloader_args
        self.from_file = from_file
        self._data: defaultdict[Any, Any] = defaultdict(dict)
        self.data = data or {}
        self.extra: Any = extra or {}  # allow anything for extra, but default to a dict

        if self.name is None:
            lang = Language.get(self.language)
            if (lang.language or "").lower() == (lang.territory or "").lower():
                lang.territory = None
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

        This uses a defaultdict with a dict as the factory for easier
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
        You may use their data, but note that these values can change or be removed
        at any point.
        """
        return self._data

    @data.setter
    def data(self, value: Union[dict, defaultdict]) -> None:
        if not isinstance(value, (dict, defaultdict)):
            raise TypeError(f"Expected data to be a {dict} or {defaultdict}, not {type(value)}")
        if isinstance(value, dict):
            value = defaultdict(dict, **value)
        self._data = value

    @property
    def drm_preference(self) -> Optional[str]:
        """
        DRM system this track must license with, one of the names in DRM_PREFERENCE_TYPES.

        None (the default) lets the loaded CDM choose. Set it when the manifest advertises more
        than one DRM system but only one of them licenses this track.
        """
        return self._drm_preference

    @drm_preference.setter
    def drm_preference(self, value: Optional[str]) -> None:
        if value is None:
            self._drm_preference = None
            return
        if not isinstance(value, str) or value.lower() not in DRM_PREFERENCE_TYPES:
            raise ValueError(
                f"Expected drm_preference to be one of {sorted(DRM_PREFERENCE_TYPES)} or None, not {value!r}"
            )
        self._drm_preference = value.lower()

    def prefers_playready(self, cdm: Optional[object]) -> bool:
        """Whether to try PlayReady before Widevine. The track's preference wins over the loaded CDM."""
        if self._drm_preference:
            return DRM_PREFERENCE_TYPES[self._drm_preference] is PlayReady
        return is_playready_cdm(cdm)

    def download(
        self,
        session: Union[Session, "RnetSession"],
        prepare_drm: partial,
        max_workers: Optional[int] = None,
        progress: Optional[partial] = None,
        *,
        cdm: Optional[object] = None,
        no_proxy_download: bool = False,
        adaptive_workers: bool = False,
        download_processes: int = 1,
    ):
        """Download and optionally Decrypt this Track.

        For a URL-descriptor Video or Audio track with no `drm` set, unshackle probes the DRM from the
        track's init data and stores it on the track, so a service need not declare `drm` itself.
        """
        from unshackle.core.manifests import DASH, HLS, ISM

        if DOWNLOAD_LICENCE_ONLY.is_set():
            progress(downloaded="[yellow]SKIPPING")

        if DOWNLOAD_CANCELLED.is_set():
            progress(downloaded="[yellow]SKIPPED")
            return

        log = logging.getLogger("track")

        proxy = next(iter(session.proxies.values()), None)

        dl_session = session
        if no_proxy_download and proxy:
            dl_session = direct_session(session)
            proxy = None

        track_type = self.__class__.__name__
        save_path = config.directories.temp / f"{track_type}_{self.id}.mp4"
        if track_type == "Subtitle":
            save_path = save_path.with_suffix(f".{self.codec.extension}")

        if self.descriptor != self.Descriptor.URL:
            save_dir = save_path.with_name(save_path.name + "_segments")
        else:
            save_dir = save_path.parent

        keep_segments = config.continue_downloads and self.descriptor != self.Descriptor.URL

        def cleanup() -> None:
            save_path.unlink(missing_ok=True)
            if save_dir.name.endswith("_segments"):
                if keep_segments:
                    if save_dir.exists():
                        for partial in save_dir.rglob("*.!dev"):
                            partial.unlink(missing_ok=True)
                else:
                    if save_dir.exists():
                        shutil.rmtree(save_dir)
                    resume.clear_sidecar(save_dir)

        if not DOWNLOAD_LICENCE_ONLY.is_set():
            if config.directories.temp.is_file():
                raise ValueError(f"Temp Directory '{config.directories.temp}' must be a Directory, not a file")

            config.directories.temp.mkdir(parents=True, exist_ok=True)

            # completed segments are reusable once the parser proves the segmentation
            # unchanged (resume sidecar); partial .!dev files never survive a run boundary
            cleanup()

        try:
            manifest_parsers = {
                self.Descriptor.HLS: HLS,
                self.Descriptor.DASH: DASH,
                self.Descriptor.ISM: ISM,
            }
            if self.descriptor in manifest_parsers:
                ctx = DownloadContext(
                    save_path=save_path,
                    save_dir=save_dir,
                    progress=progress,
                    session=dl_session,
                    proxy=proxy,
                    max_workers=max_workers,
                    adaptive_workers=adaptive_workers,
                    download_processes=download_processes,
                    license_widevine=prepare_drm,
                    cdm=cdm,
                )
                manifest_parsers[self.descriptor].download_track(track=self, ctx=ctx)
            elif self.descriptor == self.Descriptor.URL:
                try:
                    if not self.drm and track_type in ("Video", "Audio"):
                        if self.prefers_playready(cdm):
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
                            if not prepare_drm:
                                raise ValueError("prepare_drm func must be supplied to use Widevine DRM")
                            progress(downloaded="LICENSING")
                            prepare_drm(drm, track_kid=track_kid)
                            progress(downloaded="[yellow]LICENSED")
                        elif isinstance(drm, PlayReady):
                            if not prepare_drm:
                                raise ValueError("prepare_drm func must be supplied to use PlayReady DRM")
                            progress(downloaded="LICENSING")
                            prepare_drm(drm, track_kid=track_kid)
                            progress(downloaded="[yellow]LICENSED")
                        elif isinstance(drm, ClearKeyCENC):
                            if not prepare_drm:
                                raise ValueError("prepare_drm func must be supplied to use ClearKey DRM")
                            progress(downloaded="LICENSING")
                            prepare_drm(drm, track_kid=track_kid)
                            progress(downloaded="[yellow]LICENSED")
                    else:
                        drm = None

                    if DOWNLOAD_LICENCE_ONLY.is_set():
                        progress(downloaded="[yellow]SKIPPED")
                    else:
                        for status_update in self.downloader(
                            urls=self.url,
                            output_dir=save_path.parent,
                            filename=save_path.name,
                            headers=dl_session.headers,
                            cookies=dl_session.cookies,
                            proxy=proxy,
                            max_workers=max_workers,
                            session=dl_session,
                            adaptive=adaptive_workers,
                            processes=download_processes,
                        ):
                            file_downloaded = status_update.get("file_downloaded")
                            if not file_downloaded:
                                downloaded = status_update.get("downloaded")
                                if downloaded and downloaded.endswith("/s"):
                                    status_update["downloaded"] = f"URL {downloaded}"
                                progress(**status_update)

                        self.path = save_path
                        events.emit(events.Types.TRACK_DOWNLOADED, track=self)

                        if drm:
                            progress(downloaded="Decrypting", completed=0, total=None)
                            drm.decrypt(save_path)
                            assert_fragments_decrypted(save_path)
                            self.drm = None
                            events.emit(events.Types.TRACK_DECRYPTED, track=self, drm=drm, segment=None)
                            progress(downloaded="Decrypted", completed=100, total=100)
                            # residual encv/enca => decrypt tool skipped/failed; force a repack so the
                            # muxer isn't first-FourCC-locked to a generic (encrypted) codec. OR-in only.
                            if has_encrypted_sample_entry(save_path):
                                self.needs_repack = True

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

    def to_dict(self) -> dict[str, Any]:
        """Serialise the track for export/import (identity/URL/descriptor/language).

        DRM is not serialised here. The export writer attaches the licensed DRM + keys.
        Subclasses add their own codec/quality fields.
        """
        data: dict[str, Any] = {
            "type": self.__class__.__name__,
            "id": self.id,
            "url": self.url,
            "language": str(self.language),
            "is_original_lang": self.is_original_lang,
            "descriptor": self.descriptor.name,
            "needs_repack": self.needs_repack,
            "name": self.name,
            "edition": self.edition,
        }
        return data

    @staticmethod
    def base_kwargs_from_dict(data: dict[str, Any]) -> dict[str, Any]:
        """Assemble the shared Track constructor kwargs from a ``to_dict()`` payload.

        DRM is not reconstructed here: ``to_dict`` does not serialise it, and the import
        flow attaches the licensed DRM + content keys separately.
        """
        return {
            "url": data["url"],
            "language": data.get("language") or "und",
            "is_original_lang": data.get("is_original_lang", False),
            "descriptor": Track.Descriptor[data.get("descriptor", "URL")],
            "needs_repack": data.get("needs_repack", False),
            "name": data.get("name"),
            "edition": data.get("edition") or None,
            "id_": data.get("id"),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Track":
        """Reconstruct the correct Track subclass from a ``to_dict()`` payload."""
        from unshackle.core.tracks.audio import Audio
        from unshackle.core.tracks.subtitle import Subtitle
        from unshackle.core.tracks.video import Video

        track_type = data.get("type")
        builders = {"Video": Video, "Audio": Audio, "Subtitle": Subtitle}
        builder = builders.get(track_type)
        if builder is None:
            raise ValueError(f"Cannot reconstruct unsupported track type: {track_type!r}")
        return builder.from_dict(data)

    def get_track_name(self) -> Optional[str]:
        """Get the Track Name."""
        return self.name

    def get_drm_for_cdm(self, cdm: Optional[object]) -> Optional[DRM_T]:
        """Return the DRM matching the provided CDM, if available."""
        if not self.drm:
            return None

        if self._drm_preference:
            wanted = DRM_PREFERENCE_TYPES[self._drm_preference]
            for drm in self.drm:
                if isinstance(drm, wanted):
                    return drm

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

        It can find the Key ID by probing the track with FFprobe for
        `enc_key_id` data, as well as for mp4 `tenc` (Track Encryption)
        boxes.

        It explicitly ignores PSSH information like the `PSSH` box, as the box
        is likely to contain multiple Key IDs that may or may not be for this
        specific track.

        To retrieve the initialization segment, this method calls :meth:`get_init_segment`
        with the positional and keyword arguments. This method then uses the return value
        of `get_init_segment` to find the Key ID.

        Returns:
            The Key ID as a UUID object, or None if unshackle cannot find the Key ID.
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
            if uuid_box.extended_type == UUID("8974dbce-7be7-4c51-84f9-7148f9882554"):
                tenc = uuid_box.data
                if tenc.key_ID.int != 0:
                    return tenc.key_ID

    def load_drm_if_needed(self, service=None) -> bool:
        """
        Load DRM information for this track if the parser deferred it.

        Args:
            service (Service | None): Service instance that can fetch track-specific DRM info

        Returns:
            True if the DRM loaded or is already present, False if the load failed
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
            if isinstance(response, Response):
                response.encoding = response.encoding or "utf-8"
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
        Get the Track's initial segment data.

        HLS and DASH tracks must explicitly give a URL to the init segment or file.
        Give the byte-range for the init segment where possible.

        If `byte_range` is not set, it will make a HEAD request and examine the size of
        the file. If it cannot find the size, it will download up to the first
        20KB only, which should contain the entirety of the init segment. You may
        override this by changing the `maximum_size`.

        The default maximum_size of 20000 (20KB) is a tried-and-tested value that
        seems to work well across the board.

        Parameters:
            maximum_size: Size to assume as the response body length if byte-range is
                not used, if unshackle cannot find the body size, or if the body size
                is larger than it. Use a value of 20000 (20KB) or higher.
            url: Explicit init map or file URL to probe from.
            byte_range: Range of bytes to download from the explicit or implicit URL.
            session: HTTP session context, for example authorization and headers.
        """
        if not isinstance(maximum_size, int):
            raise TypeError(f"Expected maximum_size to be an {int}, not {type(maximum_size)}")
        if not isinstance(url, (str, type(None))):
            raise TypeError(f"Expected url to be a {str}, not {type(url)}")
        if not isinstance(byte_range, (str, type(None))):
            raise TypeError(f"Expected byte_range to be a {str}, not {type(byte_range)}")
        if not isinstance(session, (Session, RnetSession, type(None))):
            raise TypeError(f"Expected session to be a {Session} or {RnetSession}, not {type(session)}")

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
            s = session.get(url, stream=True)
            for chunk in s.iter_content(content_length):
                init_data = chunk
                break
            s.close()
            if not init_data:
                raise ValueError(f"Failed to read {content_length} bytes from the track URI.")

        return init_data

    def repackage(self, bsf_v: Optional[str] = None) -> bool:
        """Remux the track with FFmpeg ``-c copy``.

        A given ``bsf_v`` goes into the same pass as ``-bsf:v``, which normalises video VUI
        colour metadata without a second full-file remux. Repackaging is mandatory. The
        bitstream filter is best-effort: if the combined pass fails, and it is not the
        AAC-retry case, unshackle tries it again once without ``bsf_v`` so the remux still
        succeeds. Returns True if unshackle applied the requested ``bsf_v`` (always False
        when ``bsf_v`` is None, because the caller requested nothing).
        """
        if not self.path or not self.path.exists():
            raise ValueError("Cannot repackage a Track that has not been downloaded.")

        if not binaries.FFMPEG:
            raise EnvironmentError('FFmpeg executable "ffmpeg" was not found but is required for this call.')

        original_path = self.path
        output_path = original_path.with_stem(f"{original_path.stem}_repack")

        def ffmpeg(extra_args: list[str] = None, bsf: Optional[str] = None):
            args = [
                binaries.FFMPEG,
                "-nostdin",
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
                    "-map_metadata",
                    "-1",
                    "-fflags",
                    "bitexact",
                    "-codec",
                    "copy",
                ]
            )
            if bsf:
                # ffmpeg exits 0 after dropping packets the bsf cannot parse
                args.extend(["-bsf:v", bsf, "-xerror"])
            args.append(str(output_path))

            subprocess.run(
                args,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        log_event(
            "repackage",
            level="DEBUG",
            message=f"Repackaging {self.__class__.__name__} {original_path.name} with ffmpeg",
            context={"track_type": self.__class__.__name__, "id": self.id, "file": original_path.name},
        )

        bsf_applied = False
        try:
            ffmpeg(bsf=bsf_v)
            bsf_applied = bsf_v is not None
        except subprocess.CalledProcessError as e:
            if b"Malformed AAC bitstream detected" in e.stderr:
                ffmpeg(["-y", "-bsf:a", "aac_adtstoasc"], bsf=bsf_v)
                bsf_applied = bsf_v is not None
            elif bsf_v is not None:
                # Repack is mandatory, the VUI bitstream filter is best-effort: retry
                # without it so the remux still succeeds; caller falls back to normalize_vui.
                output_path.unlink(missing_ok=True)
                ffmpeg()
            else:
                raise

        original_path.unlink()
        self.path = output_path

        log_event(
            "repackage_complete",
            level="DEBUG",
            message=f"Repackaged {self.__class__.__name__} -> {output_path.name}",
            context={
                "track_type": self.__class__.__name__,
                "id": self.id,
                "output": output_path.name,
                "output_size": output_path.stat().st_size if output_path.exists() else 0,
            },
        )
        return bsf_applied


__all__ = ("Track", "DownloadContext")
