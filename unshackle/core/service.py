import base64
import logging
from abc import ABCMeta, abstractmethod
from collections.abc import Generator
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse, urlunparse

import click
import m3u8
import requests
from requests.adapters import HTTPAdapter, Retry
from rich.padding import Padding
from rich.rule import Rule

from unshackle.core.cacher import Cacher
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.drm import DRM_T
from unshackle.core.search_result import SearchResult
from unshackle.core.title_cacher import TitleCacher, get_account_hash, get_region_from_proxy
from unshackle.core.titles import Title_T, Titles_T
from unshackle.core.tracks import Chapters, Tracks
from unshackle.core.utilities import get_cached_ip_info, get_ip_info


def sanitize_proxy_for_log(uri: Optional[str]) -> Optional[str]:
    """
    Sanitize a proxy URI for logs by redacting any embedded userinfo (username/password).

    Examples:
      - http://user:pass@host:8080 -> http://REDACTED@host:8080
      - socks5h://user@host:1080   -> socks5h://REDACTED@host:1080
    """
    if uri is None:
        return None
    if not isinstance(uri, str):
        return str(uri)
    if not uri:
        return uri

    try:
        parsed = urlparse(uri)

        # Handle schemeless proxies like "user:pass@host:port"
        if not parsed.scheme and not parsed.netloc and "@" in uri and "://" not in uri:
            # Parse as netloc using a dummy scheme, then strip scheme back out.
            dummy = urlparse(f"http://{uri}")
            netloc = dummy.netloc
            if "@" in netloc:
                netloc = f"REDACTED@{netloc.split('@', 1)[1]}"
            # urlparse("http://...") sets path to "" for typical netloc-only strings; keep it just in case.
            return f"{netloc}{dummy.path}"

        netloc = parsed.netloc
        if "@" in netloc:
            netloc = f"REDACTED@{netloc.split('@', 1)[1]}"

        return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        if "@" in uri:
            return f"REDACTED@{uri.split('@', 1)[1]}"
        return uri


class Service(metaclass=ABCMeta):
    """The Service Base Class."""

    # Abstract class variables
    ALIASES: tuple[str, ...] = ()  # list of aliases for the service; alternatives to the service tag.
    GEOFENCE: tuple[str, ...] = ()  # list of ip regions required to use the service. empty list == no specific region.

    def __init__(self, ctx: click.Context):
        console.print(Padding(Rule(f"[rule.text]Service: {self.__class__.__name__}"), (1, 2)))

        self.config = ctx.obj.config

        self.log = logging.getLogger(self.__class__.__name__)

        self.session = self.get_session()
        self.cache = Cacher(self.__class__.__name__)
        self.title_cache = TitleCacher(self.__class__.__name__)

        # Store context for cache control flags and credential
        self.ctx = ctx
        self.credential = None  # Will be set in authenticate()
        self.current_region = None  # Will be set based on proxy/geolocation

        if not ctx.parent or not ctx.parent.params.get("no_proxy"):
            if ctx.parent:
                proxy = ctx.parent.params["proxy"]
                proxy_query = ctx.parent.params.get("proxy_query")
                proxy_provider_name = ctx.parent.params.get("proxy_provider")
            else:
                proxy = None
                proxy_query = None
                proxy_provider_name = None

            # Check for service-specific proxy mapping
            service_name = self.__class__.__name__
            service_config_dict = config.services.get(service_name, {})
            proxy_map = service_config_dict.get("proxy_map", {})

            if proxy_map and proxy_query:
                # Build the full proxy query key (e.g., "nordvpn:ca" or "us")
                if proxy_provider_name:
                    full_proxy_key = f"{proxy_provider_name}:{proxy_query}"
                else:
                    full_proxy_key = proxy_query

                # Check if there's a mapping for this query
                mapped_value = proxy_map.get(full_proxy_key)
                if mapped_value:
                    self.log.info(
                        f"Found service-specific proxy mapping: {full_proxy_key} -> {sanitize_proxy_for_log(mapped_value)}"
                    )
                    # Query the proxy provider with the mapped value
                    if proxy_provider_name:
                        # Specific provider requested
                        proxy_provider = next(
                            (x for x in ctx.obj.proxy_providers if x.__class__.__name__.lower() == proxy_provider_name),
                            None,
                        )
                        if proxy_provider:
                            mapped_proxy_uri = proxy_provider.get_proxy(mapped_value)
                            if mapped_proxy_uri:
                                proxy = mapped_proxy_uri
                                self.log.info(
                                    f"Using mapped proxy from {proxy_provider.__class__.__name__}: {sanitize_proxy_for_log(proxy)}"
                                )
                            else:
                                self.log.warning(
                                    f"Failed to get proxy for mapped value '{sanitize_proxy_for_log(mapped_value)}', using default"
                                )
                        else:
                            self.log.warning(f"Proxy provider '{proxy_provider_name}' not found, using default proxy")
                    else:
                        # No specific provider, try all providers
                        for proxy_provider in ctx.obj.proxy_providers:
                            mapped_proxy_uri = proxy_provider.get_proxy(mapped_value)
                            if mapped_proxy_uri:
                                proxy = mapped_proxy_uri
                                self.log.info(
                                    f"Using mapped proxy from {proxy_provider.__class__.__name__}: {sanitize_proxy_for_log(proxy)}"
                                )
                                break
                        else:
                            self.log.warning(
                                f"No provider could resolve mapped value '{sanitize_proxy_for_log(mapped_value)}', using default"
                            )

            if not proxy:
                # don't override the explicit proxy set by the user, even if they may be geoblocked
                with console.status("Checking if current region is Geoblocked...", spinner="dots"):
                    if self.GEOFENCE:
                        # Service has geofence - need fresh IP check to determine if proxy needed
                        try:
                            current_region = get_ip_info(self.session)["country"].lower()
                            if any(x.lower() == current_region for x in self.GEOFENCE):
                                self.log.info("Service is not Geoblocked in your region")
                            else:
                                requested_proxy = self.GEOFENCE[0]  # first is likely main region
                                self.log.info(
                                    f"Service is Geoblocked in your region, getting a Proxy to {requested_proxy}"
                                )
                                for proxy_provider in ctx.obj.proxy_providers:
                                    proxy = proxy_provider.get_proxy(requested_proxy)
                                    if proxy:
                                        self.log.info(f"Got Proxy from {proxy_provider.__class__.__name__}")
                                        break
                        except Exception as e:
                            self.log.warning(f"Failed to check geofence: {e}")
                            current_region = None
                    else:
                        self.log.info("Service has no Geofence")

            if proxy:
                self.session.proxies.update({"all": proxy})
                proxy_parse = urlparse(proxy)
                if proxy_parse.username and proxy_parse.password:
                    self.session.headers.update(
                        {
                            "Proxy-Authorization": base64.b64encode(
                                f"{proxy_parse.username}:{proxy_parse.password}".encode("utf8")
                            ).decode()
                        }
                    )
                # Always verify proxy IP - proxies can change exit nodes
                try:
                    proxy_ip_info = get_ip_info(self.session)
                    self.current_region = proxy_ip_info.get("country", "").lower() if proxy_ip_info else None
                except Exception as e:
                    self.log.warning(f"Failed to verify proxy IP: {e}")
                    # Fallback to extracting region from proxy config
                    self.current_region = get_region_from_proxy(proxy)
            else:
                # No proxy, use cached IP info for title caching (non-critical)
                try:
                    ip_info = get_cached_ip_info(self.session)
                    self.current_region = ip_info.get("country", "").lower() if ip_info else None
                except Exception as e:
                    self.log.debug(f"Failed to get cached IP info: {e}")
                    self.current_region = None

    # Optional Abstract functions
    # The following functions may be implemented by the Service.
    # Otherwise, the base service code (if any) of the function will be executed on call.
    # The functions will be executed in shown order.

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        """
        Authenticate the Service with Cookies and/or Credentials (Email/Username and Password).

        This is effectively a login() function. Any API calls or object initializations
        needing to be made, should be made here. This will be run before any of the
        following abstract functions.

        You should avoid storing or using the Credential outside this function.
        Make any calls you need for any Cookies, Tokens, or such, then use those.

        The Cookie jar should also not be stored outside this function. However, you may load
        the Cookie jar into the service session.
        """
        if cookies is not None:
            if not isinstance(cookies, CookieJar):
                raise TypeError(f"Expected cookies to be a {CookieJar}, not {cookies!r}.")
            self.session.cookies.update(cookies)

        # Store credential for cache key generation
        self.credential = credential

    def search(self) -> Generator[SearchResult, None, None]:
        """
        Search by query for titles from the Service.

        The query must be taken as a CLI argument by the Service class.
        Ideally just re-use the title ID argument (i.e. self.title).

        Search results will be displayed in the order yielded.
        """
        raise NotImplementedError(f"Search functionality has not been implemented by {self.__class__.__name__}")

    def get_widevine_service_certificate(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Union[bytes, str]:
        """
        Get the Widevine Service Certificate used for Privacy Mode.

        :param challenge: The service challenge, providing this to a License endpoint should return the
            privacy certificate that the service uses.
        :param title: The current `Title` from get_titles that is being executed. This is provided in
            case it has data needed to be used, e.g. for a HTTP request.
        :param track: The current `Track` needing decryption. Provided for same reason as `title`.
        :return: The Service Privacy Certificate as Bytes or a Base64 string. Don't Base64 Encode or
            Decode the data, return as is to reduce unnecessary computations.
        """

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        """
        Get a Widevine License message by sending a License Request (challenge).

        This License message contains the encrypted Content Decryption Keys and will be
        read by the Cdm and decrypted.

        This is a very important request to get correct. A bad, unexpected, or missing
        value in the request can cause your key to be detected and promptly banned,
        revoked, disabled, or downgraded.

        :param challenge: The license challenge from the Widevine CDM.
        :param title: The current `Title` from get_titles that is being executed. This is provided in
            case it has data needed to be used, e.g. for a HTTP request.
        :param track: The current `Track` needing decryption. Provided for same reason as `title`.
        :return: The License response as Bytes or a Base64 string. Don't Base64 Encode or
            Decode the data, return as is to reduce unnecessary computations.
        """

    # Required Abstract functions
    # The following functions *must* be implemented by the Service.
    # The functions will be executed in shown order.

    @abstractmethod
    def get_titles(self) -> Titles_T:
        """
        Get Titles for the provided title ID.

        Return a Movies, Series, or Album objects containing Movie, Episode, or Song title objects respectively.
        The returned data must be for the given title ID, or a spawn of the title ID.

        At least one object is expected to be returned, or it will presume an invalid Title ID was
        provided.

        You can use the `data` dictionary class instance attribute of each Title to store data you may need later on.
        This can be useful to store information on each title that will be required like any sub-asset IDs, or such.
        """

    def get_titles_cached(self, title_id: str = None) -> Titles_T:
        """
        Cached wrapper around get_titles() to reduce redundant API calls.

        This method checks the cache before calling get_titles() and handles
        fallback to cached data when API calls fail.

        Args:
            title_id: Optional title ID for cache key generation.
                     If not provided, will try to extract from service instance.

        Returns:
            Titles object (Movies, Series, or Album)
        """
        # Try to get title_id from service instance if not provided
        if title_id is None:
            # Different services store the title ID in different attributes
            if hasattr(self, "title"):
                title_id = self.title
            elif hasattr(self, "title_id"):
                title_id = self.title_id
            else:
                # If we can't determine title_id, just call get_titles directly
                self.log.debug("Cannot determine title_id for caching, bypassing cache")
                return self.get_titles()

        # Get cache control flags from context
        no_cache = False
        reset_cache = False
        if self.ctx and self.ctx.parent:
            no_cache = self.ctx.parent.params.get("no_cache", False)
            reset_cache = self.ctx.parent.params.get("reset_cache", False)

        # Get account hash for cache key
        account_hash = get_account_hash(self.credential)

        # Use title cache to get titles with fallback support
        return self.title_cache.get_cached_titles(
            title_id=str(title_id),
            fetch_function=self.get_titles,
            region=self.current_region,
            account_hash=account_hash,
            no_cache=no_cache,
            reset_cache=reset_cache,
        )

    @abstractmethod
    def get_tracks(self, title: Title_T) -> Tracks:
        """
        Get Track objects of the Title.

        Return a Tracks object, which itself can contain Video, Audio, Subtitle or even Chapters.
        Tracks.videos, Tracks.audio, Tracks.subtitles, and Track.chapters should be a List of Track objects.

        Each Track in the Tracks should represent a Video/Audio Stream/Representation/Adaptation or
        a Subtitle file.

        While one Track should only hold information for one stream/downloadable, try to get as many
        unique Track objects per stream type so Stream selection by the root code can give you more
        options in terms of Resolution, Bitrate, Codecs, Language, e.t.c.

        No decision making or filtering of which Tracks get returned should happen here. It can be
        considered an error to filter for e.g. resolution, codec, and such. All filtering based on
        arguments will be done by the root code automatically when needed.

        Make sure you correctly mark which Tracks are encrypted or not, and by which DRM System
        via its `drm` property.

        If you are able to obtain the Track's KID (Key ID) as a 32 char (16 bit) HEX string, provide
        it to the Track's `kid` variable as it will speed up the decryption process later on. It may
        or may not be needed, that depends on the service. Generally if you can provide it, without
        downloading any of the Track's stream data, then do.

        :param title: The current `Title` from get_titles that is being executed.
        :return: Tracks object containing Video, Audio, Subtitles, and Chapters, if available.
        """

    @abstractmethod
    def get_chapters(self, title: Title_T) -> Chapters:
        """
        Get Chapters for the Title.

        Parameters:
            title: The current Title from `get_titles` that is being processed.

        You must return a Chapters object containing 0 or more Chapter objects.

        You do not need to set a Chapter number or sort/order the chapters in any way as
        the Chapters class automatically handles all of that for you. If there's no
        descriptive name for a Chapter then do not set a name at all.

        You must not set Chapter names to "Chapter {n}" or such. If you (or the user)
        wants "Chapter {n}" style Chapter names (or similar) then they can use the config
        option `chapter_fallback_name`. For example, `"Chapter {i:02}"` for "Chapter 01".
        """

    # Optional Event methods

    def on_segment_downloaded(self, track: AnyTrack, segment: Path) -> None:
        """
        Called when one of a Track's Segments has finished downloading.

        Parameters:
            track: The Track object that had a Segment downloaded.
            segment: The Path to the Segment that was downloaded.
        """

    def on_track_downloaded(self, track: AnyTrack) -> None:
        """
        Called when a Track has finished downloading.

        Parameters:
            track: The Track object that was downloaded.
        """

    def on_track_decrypted(self, track: AnyTrack, drm: DRM_T, segment: Optional[m3u8.Segment] = None) -> None:
        """
        Called when a Track has finished decrypting.

        Parameters:
            track: The Track object that was decrypted.
            drm: The DRM object it decrypted with.
            segment: The HLS segment information that was decrypted.
        """

    def on_track_repacked(self, track: AnyTrack) -> None:
        """
        Called when a Track has finished repacking.

        Parameters:
            track: The Track object that was repacked.
        """

    def on_track_multiplex(self, track: AnyTrack) -> None:
        """
        Called when a Track is about to be Multiplexed into a Container.

        Note: Right now only MKV containers are multiplexed but in the future
        this may also be called when multiplexing to other containers like
        MP4 via ffmpeg/mp4box.

        Parameters:
            track: The Track object that was repacked.
        """


__all__ = ("Service",)
