"""Where a download takes its titles, tracks, chapters and content keys from: a Service, a remote session or an import."""

from __future__ import annotations

import logging
from abc import ABCMeta, abstractmethod
from enum import Enum
from http.cookiejar import CookieJar
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol
from uuid import UUID

import click
import m3u8
import requests

from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.drm import DRM_T
from unshackle.core.title_cacher import TitleCacher, get_account_hash
from unshackle.core.titles import Title_T, Titles_T
from unshackle.core.tracks import Chapters, Tracks
from unshackle.core.utilities import declared_kwargs

if TYPE_CHECKING:
    from unshackle.core.drm.cbcs import SampleDecrypter
    from unshackle.core.remote_service import ServerVault
    from unshackle.core.vault import Vault


class KeyArrival(Enum):
    """How the content keys of a title source reach dl.

    ``CALLBACK``: dl's CDM licenses each track through the source's licence callbacks. ``SERVER``: the server CDM
    licenses, and dl asks ``ServerKeySource.license_track``. ``EXPORT``: the keys come in the export file.
    """

    CALLBACK = "callback"
    SERVER = "server"
    EXPORT = "export"

    @property
    def needs_no_cdm(self) -> bool:
        """True when the keys arrive with no licence from dl's own CDM: from the server or the export."""
        return self is not KeyArrival.CALLBACK


class CacheIdentity(NamedTuple):
    """The cache and the keys that the title and metadata caches file the entries of a source under."""

    cacher: TitleCacher | None
    title_id: str | None
    region: str | None
    account_hash: str | None


class ServerKeySource(Protocol):
    """A title source whose content keys dl gets before ``prepare_drm``: a remote session or an import."""

    @property
    def key_arrival(self) -> KeyArrival:
        """``SERVER`` or ``EXPORT`` when dl licenses with no CDM of its own, else ``CALLBACK``."""

    @property
    def server_cdm_type(self) -> str:
        """``widevine`` or ``playready``: the DRM system the keys came from."""

    @property
    def local_cdm(self) -> Any:
        """The CDM of this machine for a track ``licenses_locally`` names, or None."""

    def resolve_server_keys(self, title: Title_T) -> None:
        """Put the content keys the server or the export holds on the tracks of ``title``."""

    def licenses_locally(self, track: AnyTrack) -> bool:
        """True when this machine's own CDM licenses ``track`` instead of the server."""

    def license_track(self, track: AnyTrack, drm: Any, kid: UUID | None = None) -> dict[UUID, str] | None:
        """The content keys the server licenses for ``drm`` of ``track``, or None when it hands ``track`` to this machine's CDM.

        Call it only for a track ``licenses_locally`` does not name; after None, it names the track. ``kid`` is
        the KID the answer must hold. A failed request returns no keys. A refusal this machine cannot take over
        raises ClickException.
        """

    def license_file(self, track: AnyTrack) -> str:
        """The content key the server licenses for the whole-file cipher of ``track``; a refusal raises."""

    def vault_source(self, kid: UUID, key: str) -> Vault | None:
        """The server vault when it served ``key`` for ``kid``, else None. That content key must pass a decrypt check."""

    @property
    def server_vault(self) -> ServerVault | None:
        """The server's vaults, which take the content keys a decrypt proved, or None."""


class TitleSource(metaclass=ABCMeta):
    """The titles, tracks, chapters and content keys of one download, as dl reads them.

    Its three adapters are ``Service``, the remote client (``RemoteService``) and the import (``ImportService``).
    dl asks the source what it can do (``key_arrival``, ``key_source``, ``cache_identity``, ``login_cookies``).
    """

    NO_SUBTITLES: bool = False  # the source has no subtitles; dl skips the subtitle and caption extraction.
    ANIME: bool = False  # service catalogue is anime; metadata lookups prefer AniList. Title.anime overrides per title.
    DAILY: bool = False  # catalog is daily/date-based. episodes are named by air date. Title.daily overrides per title.

    ctx: click.Context
    log: logging.Logger
    session: requests.Session
    credential: Credential | None = None
    current_region: str | None = None
    title_cache: TitleCacher | None = None

    @property
    def key_arrival(self) -> KeyArrival:
        """How the content keys of this source reach dl."""
        return KeyArrival.CALLBACK

    @property
    def key_source(self) -> ServerKeySource | None:
        """The source dl asks for content keys and vault answers before ``prepare_drm``, or None when only the licence callbacks give keys."""
        return None

    @property
    def login_cookies(self) -> CookieJar | None:
        """The cookies dl saves to the profile's cookie file after each title, or None when the login is not on this machine."""
        return None

    def cache_identity(self) -> CacheIdentity:
        """The cache, and the title ID, region and account that this source files its cached titles and metadata under.

        A service keeps its title ID in ``title`` or ``title_id``, whichever it sets.
        """
        if hasattr(self, "title"):
            title_id = self.title
        elif hasattr(self, "title_id"):
            title_id = self.title_id
        else:
            title_id = None
        return CacheIdentity(self.title_cache, title_id, self.current_region, get_account_hash(self.credential))

    @abstractmethod
    def authenticate(self, cookies: CookieJar | None = None, credential: Credential | None = None) -> None:
        """Log in, before any title, track or chapter call."""

    def get_widevine_service_certificate(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> bytes | str | None:
        """
        Get the Widevine Service Certificate used for Privacy Mode.

        :param challenge: The service challenge, providing this to a License endpoint should return the
            privacy certificate that the service uses.
        :param title: The current `Title` from get_titles that unshackle processes now. unshackle
            gives this in case it holds data you need, for example for an HTTP request.
        :param track: The current `Track` needing decryption. Provided for same reason as `title`.
        :return: The Service Privacy Certificate as Bytes or a Base64 string. Do not Base64 Encode or
            Decode the data, return as is to reduce unnecessary computations.
        """

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes | str | None:
        """
        Get a Widevine License message by sending a License Request (challenge).

        This License message contains the encrypted content keys and will be
        read by the Cdm and decrypted.

        This is a very important request to get correct. A bad, unexpected, or missing
        value in the request can cause the service to detect your CDM device.
        The service can then ban, revoke, disable, or downgrade that device.

        :param challenge: The license challenge from the Widevine CDM.
        :param title: The current `Title` from get_titles that unshackle processes now. unshackle
            gives this in case it holds data you need, for example for an HTTP request.
        :param track: The current `Track` needing decryption. Provided for same reason as `title`.
        :return: The License response as Bytes or a Base64 string. Do not Base64 Encode or
            Decode the data, return as is to reduce unnecessary computations.
        """

    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes | str | None:
        """
        Get a PlayReady License message by sending a License Request (challenge).

        This License message contains the encrypted content keys and will be
        read by the CDM and decrypted.

        This is a very important request to get correct. A bad, unexpected, or missing
        value in the request can cause the service to detect your CDM device.
        The service can then ban, revoke, disable, or downgrade that device.

        :param challenge: The license challenge from the PlayReady CDM.
        :param title: The current `Title` from get_titles that unshackle processes now. unshackle
            gives this in case it holds data you need, for example for an HTTP request.
        :param track: The current `Track` needing decryption. Provided for same reason as `title`.
        :return: The License response as Bytes or a Base64 string. Do not Base64 Encode or
            Decode the data, return as is to reduce unnecessary computations.
        """
        # A service that implements Widevine licensing only still gets PlayReady, with the
        # arguments its own signature declares.
        return self.get_widevine_license(
            **declared_kwargs(self.get_widevine_license, {"challenge": challenge, "title": title, "track": track})
        )

    def get_clearkey_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes | str | dict | None:
        """
        Get a W3C ClearKey License (JWK Set) by sending a License Request (challenge).

        Used for DASH `org.w3.clearkey` tracks. unshackle uses no CDM here: the challenge is
        the W3C EME JSON license request, e.g. ``{"kids": ["<base64url>"], "type": "temporary"}``,
        and the license is a JWK Set, e.g. ``{"keys": [{"kty": "oct", "k": "...", "kid": "..."}]}``.

        :param challenge: The JSON license request bytes to POST to the license server.
        :param title: The current `Title` from get_titles that unshackle processes now. unshackle
            gives this in case it holds data you need, for example for an HTTP request.
        :param track: The current `Track` needing decryption. Provided for same reason as `title`.
        :return: The JWK Set license as a dict, JSON str, or raw bytes. Return None (the default)
            to let the framework POST the challenge to the manifest-provided Laurl, if any.
            Services with no license server can instead pre-populate the DRM object's
            `signatures` in get_tracks.
        """
        return None

    @abstractmethod
    def get_titles(self) -> Titles_T:
        """The titles of the title ID."""

    def get_titles_cached(self, title_id: str | None = None) -> Titles_T:
        """The titles as dl uses them: through the title cache and the ``title_map`` where the adapter has them."""
        return self.get_titles()

    @abstractmethod
    def get_tracks(self, title: Title_T) -> Tracks:
        """The tracks of ``title``."""

    @abstractmethod
    def get_chapters(self, title: Title_T) -> Chapters:
        """The chapters of ``title``."""

    def close(self) -> None:
        """Release what the source holds open once dl has finished every title."""

    # Optional sample decryption

    def decrypts_samples(self, track: AnyTrack) -> bool:
        """Whether this service decrypts the cbcs file of `track` after the download, through `get_sample_decrypter`."""
        return False

    def get_sample_decrypter(self, track: AnyTrack, entry: int) -> bytes | SampleDecrypter:
        """The content key, or a `cbcs.SampleDecrypter`, for `stsd` entry `entry` (1-based) of `track`.

        unshackle calls this only for a track `decrypts_samples` names. A remote session runs it on the
        server, so a decrypter that needs a local process works for remote clients too.
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not decrypt samples")

    # Optional Event methods

    def on_segment_downloaded(self, track: AnyTrack, segment: Path) -> None:
        """
        Called when one of a Track's Segments has finished downloading.

        Parameters:
            track: The Track object that had a Segment downloaded.
            segment: The Path to the downloaded Segment.
        """

    def on_track_downloaded(self, track: AnyTrack) -> None:
        """
        Called when a Track has finished downloading.

        Parameters:
            track: The downloaded Track object.
        """

    def on_track_decrypted(self, track: AnyTrack, drm: DRM_T, segment: m3u8.Segment | None = None) -> None:
        """
        Called when a Track has finished decrypting.

        Parameters:
            track: The decrypted Track object.
            drm: The DRM object it decrypted with.
            segment: The decrypted HLS segment information. None for DASH and ISM tracks and
                for an HLS track that merged during the download (merge_segments).
        """

    def on_track_repacked(self, track: AnyTrack) -> None:
        """
        Called when a Track has finished repacking.

        Parameters:
            track: The repacked Track object.
        """

    def on_track_multiplex(self, track: AnyTrack) -> None:
        """
        Called immediately before unshackle multiplexes a Track into a Container.

        Note: Right now unshackle multiplexes only MKV containers. In the future
        unshackle can also call this when it multiplexes to other containers like
        MP4 with FFmpeg/mp4box.

        Parameters:
            track: The repacked Track object.
        """


__all__ = ("CacheIdentity", "KeyArrival", "ServerKeySource", "TitleSource")
