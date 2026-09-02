import logging
from abc import ABCMeta, abstractmethod
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

if TYPE_CHECKING:
    from unshackle.core.api.input_bridge import InputBridge

import click
import m3u8
import requests
from requests.adapters import HTTPAdapter, Retry
from rich.padding import Padding
from rich.rule import Rule

from unshackle.core.cacher import Cacher
from unshackle.core.config import config
from unshackle.core.console import console, prompt_user
from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.drm import DRM_T
from unshackle.core.proxies.basic import Basic
from unshackle.core.search_result import SearchResult
from unshackle.core.session import (
    BACKOFF_FACTOR,
    CONNECT_TIMEOUT,
    MAX_BACKOFF,
    MAX_RETRIES,
    POOL_MAX_SIZE,
    READ_TIMEOUT,
    RETRY_METHODS,
    STATUS_FORCELIST,
)
from unshackle.core.title_cacher import TitleCacher, get_account_hash, get_region_from_proxy
from unshackle.core.titles import Title_T, Titles_T, remap_titles
from unshackle.core.tracks import Chapters, Tracks
from unshackle.core.tracks.video import Video
from unshackle.core.utils.ip_info import get_ip_info
from unshackle.core.utils.redact import mask_proxy

# Default (connect, read) timeout for the requests path, mirroring RnetSession's
# connect_timeout / read_timeout construction defaults. A per-request timeout= wins.
DEFAULT_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)


class TimeoutSession(requests.Session):
    """requests.Session applying DEFAULT_TIMEOUT when the caller passes none.

    requests has no native default timeout. Without one, a stalled connect or
    read hangs forever. RnetSession bounds every request through its client's
    connect_timeout/read_timeout, so this mirrors that on the requests path.
    A per-request non-None ``timeout=`` wins. :class:`TimeoutHTTPAdapter` on the
    mounted adapters still replaces an explicit ``timeout=None`` with the default,
    so there is no unbounded read, as with RnetSession where the client-level
    timeouts always apply. Pass a large timeout instead.
    """

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return super().request(*args, **kwargs)


class TimeoutHTTPAdapter(HTTPAdapter):
    """HTTPAdapter applying DEFAULT_TIMEOUT when the caller passes none.

    Backstops :class:`TimeoutSession` for the ``session.send(prepared)`` path,
    which bypasses ``Session.request``. RnetSession bounds those too through its
    client, so this keeps parity. A per-request non-None ``timeout=`` wins.
    ``None`` (unset, or explicitly passed) gets the default, because the adapter
    cannot distinguish the two, and rnet has no unbounded mode either.
    """

    def __init__(self, *args: Any, timeout: Any = DEFAULT_TIMEOUT, **kwargs: Any) -> None:
        self.default_timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request: Any, **kwargs: Any) -> Any:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.default_timeout
        return super().send(request, **kwargs)


def grow_session_pool(session: Any, size: int) -> None:
    """Grow a shared requests session's connection pool to ``size`` before track downloads start.

    The worker threads of every track draw on this one pool, because the downloader never
    remounts an HTTP session the caller passes in (see downloaders/requests.py). The pool must hold
    ``downloads * workers`` connections, or threads queue for a slot instead of reading.
    Call this before any download thread exists: a remount races with other threads that call
    ``get_adapter``. RnetSession does not block on its idle-pool cap, so this function skips it.
    """
    if not isinstance(session, requests.Session):
        return
    adapter = session.get_adapter("https://")
    if not isinstance(adapter, HTTPAdapter) or getattr(adapter, "_pool_maxsize", 0) >= size:
        return
    grown = TimeoutHTTPAdapter(
        max_retries=adapter.max_retries,
        pool_connections=size,
        pool_maxsize=size,
        pool_block=True,
        timeout=getattr(adapter, "default_timeout", DEFAULT_TIMEOUT),
    )
    session.mount("https://", grown)
    session.mount("http://", grown)


@dataclass
class TrackRequest:
    """Holds what the user requested for video codec and range selection.

    Services read from this instead of ctx.parent.params for vcodec/range.

    Attributes:
        codecs: Requested codecs from CLI. Empty list means no filter (accept any).
        ranges: Requested ranges from CLI. Defaults to [SDR].
    """

    codecs: list[Video.Codec] = field(default_factory=list)
    ranges: list[Video.Range] = field(default_factory=lambda: [Video.Range.SDR])
    best_available: bool = False


def sanitize_proxy_for_log(uri: Optional[str], mask_host: bool = False) -> Optional[str]:
    """
    Sanitise a proxy URI for logs by masking any embedded userinfo (username/password).

    ``serve`` sends these log lines to the client of a remote session, so the mask is
    unconditional and debug mode never lifts it. ``mask_host`` hides the hostname as
    well, for a proxy that came from the user-supplied ``Basic`` proxy provider.
    """
    if uri is None:
        return None
    if not isinstance(uri, str):
        return str(uri)
    return mask_proxy(uri, mask_host=mask_host, allow_debug=False)


class Service(metaclass=ABCMeta):
    """The Service Base Class.

    A Service must define the abstract methods. The rest are optional overrides that fall back to the base
    implementation when a Service does not define them. The main flow operates the HTTP session and
    authentication methods first, then titles, then tracks and chapters. The license callbacks operate later
    still, during track download.
    """

    ALIASES: tuple[str, ...] = ()  # alternative tags for the service, matched without case.
    GEOFENCE: tuple[str, ...] = ()  # list of ip regions required to use the service. empty list == no specific region.
    ANIME: bool = False  # service catalogue is anime; metadata lookups prefer AniList. Title.anime overrides per title.
    DAILY: bool = False  # catalog is daily/date-based. episodes are named by air date. Title.daily overrides per title.
    # vault namespace override; when set, key vault read/write uses this tag instead of the service's own.
    VAULT_TAG: Optional[str] = None
    # Auth methods the service accepts ("cookies"/"credentials"); when None the REST /services
    # endpoint infers them from authenticate().
    AUTH_METHODS: Optional[tuple[str, ...]] = None

    def __init__(self, ctx: click.Context):
        console.print(Padding(Rule(f"[rule.text]Service: {self.__class__.__name__}"), (1, 2)))

        self.config = ctx.obj.config

        self.log = logging.getLogger(self.__class__.__name__)

        self.session = self.get_session()
        self.cache = Cacher(self.__class__.__name__)
        self.title_cache = TitleCacher(self.__class__.__name__)
        self.cache_dir = config.directories.cache / self.__class__.__name__

        self.ctx = ctx
        self.credential = None  # Will be set in authenticate()
        self.current_region = None  # Will be set based on proxy/geolocation
        self._input_bridge: Optional[InputBridge] = None

        # Set track request from CLI params - services can read/override in their __init__
        vcodec = ctx.parent.params.get("vcodec") if ctx.parent else None
        range_ = ctx.parent.params.get("range_") if ctx.parent else None
        best_available = ctx.parent.params.get("best_available", False) if ctx.parent else False
        self.track_request = TrackRequest(
            codecs=list(vcodec) if vcodec else [],
            ranges=list(range_) if range_ else [Video.Range.SDR],
            best_available=bool(best_available),
        )

        if not ctx.parent or not ctx.parent.params.get("no_proxy"):
            if ctx.parent:
                proxy = ctx.parent.params["proxy"]
                proxy_query = ctx.parent.params.get("proxy_query")
                proxy_provider_name = ctx.parent.params.get("proxy_provider")
            else:
                proxy = None
                proxy_query = None
                proxy_provider_name = None

            service_name = self.__class__.__name__
            service_config_dict = config.services.get(service_name, {})
            proxy_map = service_config_dict.get("proxy_map", {})

            if proxy_map and proxy_query:
                if proxy_provider_name:
                    full_proxy_key = f"{proxy_provider_name}:{proxy_query}"
                else:
                    full_proxy_key = proxy_query

                mapped_value = proxy_map.get(full_proxy_key)
                if mapped_value:
                    self.log.info(
                        f"Found service-specific proxy mapping: {full_proxy_key} -> {sanitize_proxy_for_log(mapped_value)}"
                    )
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
                                    f"Using mapped proxy from {proxy_provider.__class__.__name__}: "
                                    f"{sanitize_proxy_for_log(proxy, mask_host=isinstance(proxy_provider, Basic))}"
                                )
                            else:
                                self.log.warning(
                                    f"Failed to get proxy for mapped value '{sanitize_proxy_for_log(mapped_value)}', using default"
                                )
                        else:
                            self.log.warning(f"Proxy provider '{proxy_provider_name}' not found, using default proxy")
                    else:
                        for proxy_provider in ctx.obj.proxy_providers:
                            mapped_proxy_uri = proxy_provider.get_proxy(mapped_value)
                            if mapped_proxy_uri:
                                proxy = mapped_proxy_uri
                                self.log.info(
                                    f"Using mapped proxy from {proxy_provider.__class__.__name__}: "
                                    f"{sanitize_proxy_for_log(proxy, mask_host=isinstance(proxy_provider, Basic))}"
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
                                if not proxy:
                                    self.log.warning(
                                        f"No proxy available for {requested_proxy}. "
                                        f"Pass --proxy with a proxy in {requested_proxy}, or the request can fail."
                                    )
                        except Exception as e:
                            self.log.warning(f"Failed to check geofence: {e}")
                            current_region = None
                    else:
                        self.log.info("Service has no Geofence")

            if proxy:
                self.session.proxies.update({"all": proxy})
                # Don't set Proxy-Authorization manually: both rnet (Proxy.all) and
                # requests authenticate from the credentials embedded in the proxy URL.
                # A manual header here was malformed (no "Basic " scheme) and broke
                # plaintext-http forward-proxy requests with HTTP 407.
                # Always verify proxy IP - proxies can change exit nodes
                try:
                    proxy_ip_info = get_ip_info(self.session)
                    self.current_region = proxy_ip_info.get("country", "").lower() if proxy_ip_info else None
                except Exception as e:
                    self.log.warning(f"Failed to verify proxy IP: {e}")
                    self.current_region = get_region_from_proxy(proxy)
            else:
                # No proxy, use cached IP info for title caching (non-critical)
                try:
                    ip_info = get_ip_info(self.session, cached=True)
                    self.current_region = ip_info.get("country", "").lower() if ip_info else None
                except Exception as e:
                    self.log.debug(f"Failed to get cached IP info: {e}")
                    self.current_region = None

    def get_tracks_for_variants(
        self,
        title: Title_T,
        fetch_fn: Callable[..., Tracks],
    ) -> Tracks:
        """Call fetch_fn for each codec/range combo in track_request, merge results.

        Services that need separate API calls per codec/range combo can use this
        helper from their get_tracks() implementation.

        The fetch_fn signature should be: (title, codec, range_) -> Tracks

        For HYBRID range, this helper calls fetch_fn with HDR10 and DV separately,
        then merges the DV video tracks into the HDR10 result.

        Args:
            title: The title to process.
            fetch_fn: A callable that fetches tracks for a specific codec/range.
        """
        all_tracks = Tracks()
        first = True

        codecs = self.track_request.codecs or [None]
        ranges = self.track_request.ranges or [Video.Range.SDR]

        for range_val in ranges:
            if range_val == Video.Range.HYBRID:
                # HYBRID: fetch HDR10 first (full tracks), then DV (video only)
                for codec_val in codecs:
                    try:
                        hdr_tracks = fetch_fn(title, codec=codec_val, range_=Video.Range.HDR10)
                    except (ValueError, SystemExit) as e:
                        if self.track_request.best_available:
                            self.log.warning(f" - HDR10 not available for HYBRID, skipping ({e})")
                            continue
                        raise
                    if first:
                        all_tracks.add(hdr_tracks, warn_only=True)
                        if hdr_tracks.manifest_url and not all_tracks.manifest_url:
                            all_tracks.manifest_url = hdr_tracks.manifest_url
                        first = False
                    else:
                        for video in hdr_tracks.videos:
                            all_tracks.add(video, warn_only=True)

                    try:
                        dv_tracks = fetch_fn(title, codec=codec_val, range_=Video.Range.DV)
                        for video in dv_tracks.videos:
                            all_tracks.add(video, warn_only=True)
                    except (ValueError, SystemExit):
                        self.log.info(" - No DolbyVision manifest available for HYBRID")
            else:
                for codec_val in codecs:
                    try:
                        tracks = fetch_fn(title, codec=codec_val, range_=range_val)
                    except (ValueError, SystemExit) as e:
                        if self.track_request.best_available:
                            codec_name = codec_val.name if codec_val else "default"
                            self.log.warning(f" - {range_val.name}/{codec_name} not available, skipping ({e})")
                            continue
                        raise
                    if first:
                        all_tracks.add(tracks, warn_only=True)
                        if tracks.manifest_url and not all_tracks.manifest_url:
                            all_tracks.manifest_url = tracks.manifest_url
                        first = False
                    else:
                        for video in tracks.videos:
                            all_tracks.add(video, warn_only=True)

        return all_tracks

    # Deprecated 5.5.0 shim for service repos still on the old underscored name; drop once they migrate.
    _get_tracks_for_variants = get_tracks_for_variants

    @staticmethod
    def get_session() -> requests.Session:
        """
        Creates a Python-requests HTTP session, adds common headers
        from config, cookies, retry handler, and a proxy if available.
        :returns: Prepared Python-requests HTTP session
        """
        session = TimeoutSession()
        session.headers.update(config.headers)
        # Retry / pool policy mirrors RnetSession via the shared constants in session.py:
        # same total, forcelist, backoff cap, allowed_methods (incl. POST, so license
        # requests retry on both paths), and pool size. Accepted divergence: urllib3's
        # backoff_jitter adds an absolute 0..N seconds whereas rnet jitters ±10% of the
        # backoff, the only intentional difference, not worth hand-rolling a retry loop.
        session.mount(
            "https://",
            TimeoutHTTPAdapter(
                max_retries=Retry(
                    total=MAX_RETRIES,
                    backoff_factor=BACKOFF_FACTOR,
                    backoff_max=MAX_BACKOFF,
                    backoff_jitter=0.2,
                    status_forcelist=STATUS_FORCELIST,
                    allowed_methods=RETRY_METHODS,
                    respect_retry_after_header=True,
                ),
                pool_connections=POOL_MAX_SIZE,
                pool_maxsize=POOL_MAX_SIZE,
                pool_block=True,
            ),
        )
        session.mount("http://", session.adapters["https://"])
        return session

    @staticmethod
    def get_binaries() -> list[dict]:
        """
        Declare custom binary dependencies required by this service.
        :returns: List of dicts specifying name, candidates, desc, etc.
        """
        return []

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        """
        Authenticate the Service with Cookies and/or Credentials (Email/Username and Password).

        This is effectively a login() function. Any API calls or object initializations
        needing to be made, should be made here. unshackle operates this method before
        any of the following abstract functions.

        You should avoid storing or using the Credential outside this function.
        Make any calls you need for any Cookies, Tokens, or such, then use those.

        Do not store the cookies outside this function either. However, you can load
        the cookies into the service HTTP session.
        """
        if cookies is not None:
            if not isinstance(cookies, CookieJar):
                raise TypeError(f"Expected cookies to be a {CookieJar}, not {cookies!r}.")
            self.session.cookies.update(cookies)

        # Store credential for cache key generation
        self.credential = credential

    def request_input(self, prompt: str) -> str:
        """Request interactive input from the user.

        When running locally (CLI), prompts through the shared rich console so the
        prompt renders correctly alongside Live progress / log handlers.
        When running in serve mode with an :class:`InputBridge` attached,
        delegates to the bridge which relays the prompt to the remote client.
        """
        if self._input_bridge is not None:
            return self._input_bridge.request_input(prompt)
        return prompt_user(prompt)

    def search(self) -> Generator[SearchResult, None, None]:
        """
        Find titles from the Service by query.

        The Service class must take the query as a CLI argument.
        Ideally re-use the title ID argument (that is, self.title).

        unshackle displays the search results in the order yielded.
        """
        raise NotImplementedError(f"Search functionality has not been implemented by {self.__class__.__name__}")

    def get_widevine_service_certificate(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Union[bytes, str]:
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

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
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

    def get_playready_license(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Optional[Union[bytes, str]]:
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
        # Delegates license handling to the Widevine license method by default if a service-specific PlayReady implementation is not provided.
        return self.get_widevine_license(challenge=challenge, title=title, track=track)

    def get_clearkey_license(
        self, *, challenge: bytes, title: Title_T, track: AnyTrack
    ) -> Optional[Union[bytes, str, dict]]:
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
            `content_keys` in get_tracks.
        """
        return None

    @abstractmethod
    def get_titles(self) -> Titles_T:
        """
        Get Titles for the provided title ID.

        Return a Movies, Series, or Album objects containing Movie, Episode, or Song title objects respectively.
        The returned data must be for the given title ID, or a spawn of the title ID.

        You must return at least one object. If you do not, unshackle presumes an invalid
        Title ID.

        You can use the `data` dictionary class instance attribute of each Title to store data you may need later on.
        This can be useful to store information on each title that you need later, like any sub-asset IDs, or such.
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
        if title_id is None:
            # Different services store the title ID in different attributes
            if hasattr(self, "title"):
                title_id = self.title
            elif hasattr(self, "title_id"):
                title_id = self.title_id
            else:
                # If we can't determine title_id, just call get_titles directly
                self.log.debug("Cannot determine title_id for caching, bypassing cache")
                return self.apply_title_map(self.get_titles())

        no_cache = False
        reset_cache = False
        if self.ctx and self.ctx.parent:
            no_cache = self.ctx.parent.params.get("no_cache", False)
            reset_cache = self.ctx.parent.params.get("reset_cache", False)

        # Get account hash for cache key
        account_hash = get_account_hash(self.credential)

        titles = self.title_cache.get_cached_titles(
            title_id=str(title_id),
            fetch_function=self.get_titles,
            region=self.current_region,
            account_hash=account_hash,
            no_cache=no_cache,
            reset_cache=reset_cache,
        )
        return self.apply_title_map(titles)

    def apply_title_map(self, titles: Titles_T) -> Titles_T:
        """
        Rewrite service-provided titles using the per-service ``title_map`` config.

        ``title_map`` lives under ``services.<TAG>`` in unshackle.yaml. Applied after the
        title cache so config edits take effect without a cache reset, and before any
        ``--enrich`` override so enrich wins. See ``remap_titles`` for the match rules.
        """
        return remap_titles(titles, (self.config or {}).get("title_map") or {})

    @abstractmethod
    def get_tracks(self, title: Title_T) -> Tracks:
        """
        Get Track objects of the Title.

        Return a Tracks object, which itself can contain Video, Audio, Subtitle or even Chapters.
        Tracks.videos, Tracks.audio, Tracks.subtitles, and Track.chapters should be a List of Track objects.

        Each Track in the Tracks should represent a Video/Audio track, Representation, or
        Adaptation, or a Subtitle file.

        While one Track should only hold information for one downloadable track, try to get as many
        unique Track objects per track type so track selection by the root code can give you more
        options in terms of Resolution, Bitrate, Codecs, Language, and such.

        No decision making or filtering of which Tracks get returned should happen here. It can be
        considered an error to filter for e.g. resolution, codec, and such. All filtering based on
        arguments will be done by the root code automatically when needed.

        Make sure you correctly mark which Tracks have encryption, and which DRM System they
        use, with the `drm` property.

        If you can get the Track's KID (Key ID) as a 32 char (16 bit) HEX string, give it to the
        Track's `kid` variable, as it will speed up the decryption process later on. The service
        decides whether it is necessary. Generally if you can give it, without downloading any of
        the Track's media data, then do.

        :param title: The current `Title` from get_titles that unshackle processes now.
        :return: Tracks object containing Video, Audio, Subtitles, and Chapters, if available.
        """

    @abstractmethod
    def get_chapters(self, title: Title_T) -> Chapters:
        """
        Get Chapters for the Title.

        Parameters:
            title: The current Title from `get_titles` that unshackle processes now.

        You must return a Chapters object containing 0 or more Chapter objects.

        You do not need to set a Chapter number or sort/order the chapters in any way as
        the Chapters class automatically handles all of that for you. If there is no
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
            segment: The Path to the downloaded Segment.
        """

    def on_track_downloaded(self, track: AnyTrack) -> None:
        """
        Called when a Track has finished downloading.

        Parameters:
            track: The downloaded Track object.
        """

    def on_track_decrypted(self, track: AnyTrack, drm: DRM_T, segment: Optional[m3u8.Segment] = None) -> None:
        """
        Called when a Track has finished decrypting.

        Parameters:
            track: The decrypted Track object.
            drm: The DRM object it decrypted with.
            segment: The decrypted HLS segment information.
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


__all__ = ("Service", "TrackRequest")
