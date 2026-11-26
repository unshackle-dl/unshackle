import logging
import time
from abc import abstractmethod
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from unshackle.core.api.input_bridge import InputBridge

import click
import requests
from requests.adapters import HTTPAdapter, Retry
from rich.padding import Padding
from rich.rule import Rule

from unshackle.core.cacher import Cacher
from unshackle.core.config import config
from unshackle.core.console import console, prompt_user, show_notice, show_qr
from unshackle.core.credential import Credential
from unshackle.core.launch import launch_keys
from unshackle.core.proxies.basic import Basic
from unshackle.core.proxies.resolve import pick_proxy
from unshackle.core.remote_protocol import APIErrorCode, coded
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
from unshackle.core.title_cacher import TitleCacher
from unshackle.core.title_source import TitleSource
from unshackle.core.titles import Title_T, Titles_T, remap_titles
from unshackle.core.tracks import Chapters, Tracks
from unshackle.core.tracks.video import Video
from unshackle.core.utils import run_proxy
from unshackle.core.utils.collections import ci_get
from unshackle.core.utils.ip_info import get_ip_info, verify_proxy_exit
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

    __attrs__ = [*HTTPAdapter.__attrs__, "default_timeout"]

    def __init__(self, *args: Any, timeout: Any = DEFAULT_TIMEOUT, **kwargs: Any) -> None:
        self.default_timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> requests.Response:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = getattr(self, "default_timeout", DEFAULT_TIMEOUT)
        return super().send(request, *args, **kwargs)


def grow_session_pool(session: Any, size: int) -> None:
    """Grow a shared requests session's connection pool to ``size`` before track downloads start.

    The worker threads of every track draw on this one pool, because the downloader never
    remounts an HTTP session the caller passes in (see downloaders/requests.py). The pool must hold
    ``downloads * workers`` connections, or threads queue for a slot instead of reading.
    Call this before any download thread exists: the rebuild races with other threads that call
    ``get_adapter``. RnetSession does not block on its idle-pool cap, so this function skips it.

    Every mounted adapter grows in place, through its own ``init_poolmanager``. A service can
    mount an :class:`HTTPAdapter` subclass, such as ``SSLCiphers``, on any prefix. Mounting a new
    adapter over it would drop that subclass state, and its TLS context with it.
    """
    if not isinstance(session, requests.Session):
        return
    for adapter in {id(a): a for a in session.adapters.values()}.values():
        if not isinstance(adapter, HTTPAdapter) or getattr(adapter, "_pool_maxsize", 0) >= size:
            continue
        adapter.init_poolmanager(size, size, block=True)
        adapter.proxy_manager.clear()


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
    generate_dv: bool = False


def sanitize_proxy_for_log(uri: str | None, mask_host: bool = False) -> str | None:
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


T = TypeVar("T")


class Service(TitleSource):
    """The Service Base Class.

    A Service must define the abstract methods. The rest are optional overrides that fall back to the base
    implementation when a Service does not define them. The main flow operates the HTTP session and
    authentication methods first, then titles, then tracks and chapters. The license callbacks operate later
    still, during track download.
    """

    ALIASES: tuple[str, ...] = ()  # alternative tags for the service, matched without case.
    GEOFENCE: tuple[str, ...] = ()  # list of ip regions required to use the service. empty list == no specific region.
    GEOBLOCK: tuple[str, ...] = ()  # ip regions where the service refuses to work; everything else is allowed.
    ANONYMOUS_SEARCH: bool = False  # search() needs no login; a search then skips cookies and authenticate().
    # vault namespace override; when set, key vault read/write uses this tag instead of the service's own.
    VAULT_TAG: str | None = None
    # Auth methods the service accepts ("cookies"/"credentials"); when None the REST /services
    # endpoint infers them from authenticate().
    AUTH_METHODS: tuple[str, ...] | None = None
    # Config keys a remote client may supply for a session its own device licenses (a device identity
    # such as an ESN); the server's own values for these keys are withheld from that session.
    CLIENT_CONFIG: tuple[str, ...] = ()
    # Every service option that makes the service call ``cdm.lend_server_device()``. A remote session with
    # one of them set keeps the server CDM even when the remote client picked its own device, and the server
    # loads the first-listed device of its cdm entry, whatever system the client's device is.
    SERVER_DEVICE_OPTIONS: tuple[str, ...] = ()
    # Cache key prefixes that hold state built from the device identity (a device ID, device session keys), never
    # login state. A remote session on the server's device keeps them on the server, per API key.
    IDENTITY_CACHE: tuple[str, ...] = ()

    def __init__(self, ctx: click.Context) -> None:
        console.print(Padding(Rule(f"[rule.text]Service: {self.__class__.__name__}"), (1, 2)))

        self.config = ctx.obj.config

        self.log = logging.getLogger(self.__class__.__name__)

        self.session = self.get_session()
        self.cache = Cacher(self.__class__.__name__)
        self.title_cache: TitleCacher = TitleCacher(self.__class__.__name__)
        self.cache_dir = config.directories.cache / self.__class__.__name__

        self.ctx = ctx
        self.credential: Credential | None = None  # Will be set in authenticate()
        self.current_region = None  # Will be set based on proxy/geolocation
        self._input_bridge: InputBridge | None = None

        # Set track request from CLI params - services can read/override in their __init__
        vcodec = ctx.parent.params.get("vcodec") if ctx.parent else None
        range_ = ctx.parent.params.get("range_") if ctx.parent else None
        best_available = ctx.parent.params.get("best_available", False) if ctx.parent else False
        generate_dv = ctx.parent.params.get("generate_dv", False) if ctx.parent else False
        self.track_request = TrackRequest(
            codecs=list(vcodec) if vcodec else [],
            ranges=list(range_) if range_ else [Video.Range.SDR],
            best_available=bool(best_available),
            generate_dv=bool(generate_dv),
        )

        keys = launch_keys(ctx)
        whose = "the server's" if keys.get("served") else "your"
        if not ctx.parent or not ctx.parent.params.get("no_proxy"):
            proxy = ctx.parent.params["proxy"] if ctx.parent else None
            proxy_query = keys.get("proxy_query")
            proxy_provider_name = keys.get("proxy_provider")

            service_name = self.__class__.__name__
            service_config_dict = ci_get(config.services, service_name) or {}
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
                    try:
                        proxy_provider, proxy = pick_proxy(ctx.obj.proxy_providers, mapped_value, proxy_provider_name)
                        self.log.info(
                            f"Using mapped proxy from {proxy_provider.__class__.__name__}: "
                            f"{sanitize_proxy_for_log(proxy, mask_host=isinstance(proxy_provider, Basic))}"
                        )
                    except ValueError:
                        self.log.warning(
                            f"No provider could resolve mapped value '{sanitize_proxy_for_log(mapped_value)}', using default"
                        )

            if not proxy:
                # don't override the explicit proxy set by the user, even if they may be geoblocked
                with console.status("Checking if current region is Geoblocked...", spinner="dots"):
                    if self.GEOFENCE or self.GEOBLOCK:
                        try:
                            current_region = (get_ip_info(self.session) or {}).get("country", "").lower()
                            if not current_region:
                                self.log.warning(f"Could not find {whose} region, so the region check does not run")
                            elif self.is_region_allowed(current_region):
                                self.log.info("Service is not Geoblocked in your region")
                            elif not (fence_targets := [x for x in self.GEOFENCE if self.is_region_allowed(x)]):
                                raise coded(
                                    click.ClickException(
                                        f"Service is not available in {whose} region ({current_region.upper()})"
                                    ),
                                    APIErrorCode.GEOFENCE,
                                )
                            else:
                                requested_proxy = fence_targets[0]  # first is likely main region
                                self.log.info(
                                    f"Service is Geoblocked in your region, getting a Proxy to {requested_proxy}"
                                )
                                try:
                                    proxy_provider, proxy = pick_proxy(ctx.obj.proxy_providers, requested_proxy)
                                    self.log.info(f"Got Proxy from {proxy_provider.__class__.__name__}")
                                except ValueError:
                                    self.log.warning(f"No proxy available for {requested_proxy}")
                        except click.ClickException:
                            raise
                        except Exception as e:
                            self.log.warning(f"Failed to check geofence: {e}")
                    else:
                        self.log.info("Service has no region restrictions")

            if proxy:
                self.session.proxies.update({"all": proxy})
                if isinstance(self.session, requests.Session):
                    run_proxy.pin(self.session)
                # Don't set Proxy-Authorization manually: both rnet (Proxy.all) and
                # requests authenticate from the credentials embedded in the proxy URL.
                # A manual header here was malformed (no "Basic " scheme) and broke
                # plaintext-http forward-proxy requests with HTTP 407.
                # Verify the proxy IP every time, because a proxy can change its exit node. A dead
                # proxy fails here, not after every service request has used up its retries.
                exit_region = verify_proxy_exit(self.session).get("country")
                self.current_region = exit_region
                # Only GEOBLOCK applies here: an explicit --proxy outside GEOFENCE stays the user's call.
                if exit_region and any(x.lower() == exit_region.lower() for x in self.GEOBLOCK):
                    raise coded(
                        click.ClickException(
                            f"Service is not available in {whose} proxy's region ({exit_region.upper()})"
                        ),
                        APIErrorCode.GEOFENCE,
                    )
            else:
                # No proxy, use cached IP info for title caching (non-critical)
                try:
                    ip_info = get_ip_info(self.session, cached=True)
                    self.current_region = ip_info.get("country", "").lower() if ip_info else None
                except Exception as e:
                    self.log.debug(f"Failed to get cached IP info: {e}")
                    self.current_region = None

        run_proxy.watch(type(self), run_proxy.proxy_of(self.session))

    @property
    def login_cookies(self) -> CookieJar | None:
        """The HTTP session cookies, which dl saves to the profile's cookie file after each title."""
        return self.session.cookies

    @classmethod
    def is_region_allowed(cls, region: str | None) -> bool:
        """True when an exit IP in `region` (two-letter region code, any case) can use the service.

        An unknown region (None or empty) is allowed, because there is nothing to compare.
        """
        if not region:
            return True
        region = region.lower()
        if any(x.lower() == region for x in cls.GEOBLOCK):
            return False
        return not cls.GEOFENCE or any(x.lower() == region for x in cls.GEOFENCE)

    def get_tracks_for_variants(
        self,
        title: Title_T,
        fetch_fn: Callable[..., Tracks],
    ) -> Tracks:
        """Call fetch_fn for each codec/range combo in track_request, merge results.

        Video, audio and subtitles merge from every combo, so a track that only one manifest
        offers is kept. The helper skips a later track whose ``Tracks.signature`` matches a held
        one, because its ID can differ between manifests. Chapters and attachments come from the
        first combo only.

        Services that need separate API calls per codec/range combo can use this
        helper from their get_tracks() implementation.

        The fetch_fn signature should be: (title, codec, range_) -> Tracks

        For HYBRID range, this helper calls fetch_fn with HDR10 and DV separately,
        then merges the DV tracks into the HDR10 result. With no DV video and
        ``generate_dv`` requested, it merges the HDR10P tracks instead.

        Args:
            title: The title to process.
            fetch_fn: A callable that fetches tracks for a specific codec/range.
        """
        all_tracks = Tracks()
        first = True

        def merge(tracks: Tracks) -> None:
            nonlocal first
            if first:
                all_tracks.add(tracks, warn_only=True)
                first = False
                return
            held = {Tracks.signature(track) for track in all_tracks}
            all_tracks.add([track for track in tracks if Tracks.signature(track) not in held], warn_only=True)

        codecs: Sequence[Video.Codec | None] = self.track_request.codecs or [None]
        ranges = self.track_request.ranges or [Video.Range.SDR]

        for range_val in ranges:
            if range_val == Video.Range.HYBRID:
                # HYBRID: fetch HDR10 first, then DV
                for codec_val in codecs:
                    try:
                        hdr_tracks = fetch_fn(title, codec=codec_val, range_=Video.Range.HDR10)
                    except (ValueError, SystemExit) as e:
                        if self.track_request.best_available:
                            self.log.warning(f" - HDR10 not available for HYBRID, skipping ({e})")
                            continue
                        raise
                    merge(hdr_tracks)

                    try:
                        dv_tracks = fetch_fn(title, codec=codec_val, range_=Video.Range.DV)
                    except (ValueError, SystemExit):
                        self.log.info(" - No DolbyVision manifest available for HYBRID")
                        dv_tracks = Tracks()
                    merge(dv_tracks)
                    has_hdr10p = any(v.range == Video.Range.HDR10P for v in all_tracks.videos)
                    if not dv_tracks.videos and self.track_request.generate_dv and not has_hdr10p:
                        # the HDR10 request may not return HDR10+, which the conversion needs as its base
                        try:
                            hdr10p_tracks = fetch_fn(title, codec=codec_val, range_=Video.Range.HDR10P)
                        except (ValueError, SystemExit):
                            self.log.info(" - No HDR10+ manifest available for HYBRID")
                        else:
                            merge(hdr10p_tracks)
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
                    merge(tracks)

        return all_tracks

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
                    retry_after_max=int(MAX_BACKOFF),
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
        :returns: List of dicts, each with a ``name`` and optional ``candidates`` and ``desc``.
        """
        return []

    def authenticate(self, cookies: CookieJar | None = None, credential: Credential | None = None) -> None:
        """
        Authenticate the Service with Cookies and/or Credentials (Email/Username and Password).

        This is effectively a login() function. Any API calls or object initializations
        needing to be made, should be made here. unshackle operates this method before
        any of the following abstract functions. The one exception is a search on a
        Service that sets ``ANONYMOUS_SEARCH``, which does not operate this method.

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

    def request_input(self, prompt: str, qr: str | None = None) -> str:
        """Request interactive input from the user.

        Locally, prompts through the shared rich console so the prompt renders alongside
        Live progress. A remote-dl session relays it through its :class:`InputBridge`, and a
        REST download job relays it through ``prompt_user`` to the job's ``input_prompt``.
        *qr* is the text of a QR code for the user to scan; the terminal that asks the user draws it.
        """
        if self._input_bridge is not None:
            return self._input_bridge.request_input(prompt, qr=qr)
        return prompt_user(prompt, qr)

    def wait_for_approval(
        self, message: str, check: Callable[[], T | None], timeout: float, interval: float = 3.0, qr: str | None = None
    ) -> T:
        """Show ``message``, then call ``check`` until it returns a value.

        Use it for a login that the user approves on another device, such as a pair code.
        ``check`` returns None while the approval is pending, and must return within a few
        seconds. Raises TimeoutError after ``timeout`` seconds, and RuntimeError when the
        remote session no longer exists. A REST download job shows ``message`` as its ``notice``.
        *qr* is the text of a QR code for the user to scan; a remote client gets it with the log record of ``message``.
        """
        self.log.info(message, extra={"qr": qr})
        if qr and self._input_bridge is None:
            show_qr(qr)
        show_notice(message, qr)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if self._input_bridge is not None:
                    self._input_bridge.ensure_active()
                result = check()
                if result is not None:
                    return result
                if time.monotonic() + interval >= deadline:
                    raise coded(TimeoutError(f"No approval within {timeout:.0f}s"), APIErrorCode.AUTH_FAILED)
                time.sleep(interval)
        finally:
            show_notice(None)

    def search(self) -> Generator[SearchResult, None, None]:
        """
        Find titles from the Service by query.

        The Service class must take the query as a CLI argument.
        Ideally re-use the title ID argument (that is, self.title).

        unshackle displays the search results in the order yielded.
        """
        raise NotImplementedError(f"Search functionality has not been implemented by {self.__class__.__name__}")

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

    def get_titles_cached(self, title_id: str | None = None) -> Titles_T:
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
        identity = self.cache_identity()
        if title_id is None:
            title_id = identity.title_id
        if title_id is None:
            self.log.debug("Cannot determine title_id for caching, bypassing cache")
            return self.apply_title_map(self.get_titles())

        no_cache = False
        reset_cache = False
        if self.ctx and self.ctx.parent:
            no_cache = self.ctx.parent.params.get("no_cache", False)
            reset_cache = self.ctx.parent.params.get("reset_cache", False)

        titles = self.title_cache.get_cached_titles(
            title_id=str(title_id),
            fetch_function=self.get_titles,
            region=identity.region,
            account_hash=identity.account_hash,
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


__all__ = ("Service", "TrackRequest")
