"""Remote service implementation for connecting to remote unshackle servers."""

import logging
import time
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any, Dict, Optional, Union

import click
import requests
from rich.padding import Padding
from rich.rule import Rule

from unshackle.core.api.session_serializer import deserialize_session
from unshackle.core.console import console
from unshackle.core.credential import Credential
from unshackle.core.local_session_cache import get_local_session_cache
from unshackle.core.search_result import SearchResult
from unshackle.core.titles import Episode, Movie, Movies, Series
from unshackle.core.tracks import Chapter, Chapters, Tracks
from unshackle.core.tracks.audio import Audio
from unshackle.core.tracks.subtitle import Subtitle
from unshackle.core.tracks.video import Video


class RemoteService:
    """
    Remote Service wrapper that connects to a remote unshackle server.

    This class mimics the Service interface but delegates all operations
    to a remote unshackle server via API calls. It receives session data
    from the remote server which is then used locally for downloading.
    """

    ALIASES: tuple[str, ...] = ()
    GEOFENCE: tuple[str, ...] = ()

    def __init__(
        self,
        ctx: click.Context,
        remote_url: str,
        api_key: str,
        service_tag: str,
        service_metadata: Dict[str, Any],
        **kwargs,
    ):
        """
        Initialize remote service.

        Args:
            ctx: Click context
            remote_url: Base URL of the remote unshackle server
            api_key: API key for authentication
            service_tag: The service tag on the remote server (e.g., "DSNP")
            service_metadata: Metadata about the service from remote discovery
            **kwargs: Additional service-specific parameters
        """
        console.print(Padding(Rule(f"[rule.text]Remote Service: {service_tag}"), (1, 2)))

        self.log = logging.getLogger(f"RemoteService.{service_tag}")
        self.remote_url = remote_url.rstrip("/")
        self.api_key = api_key
        self.service_tag = service_tag
        self.service_metadata = service_metadata
        self.ctx = ctx
        self.kwargs = kwargs

        # Set GEOFENCE and ALIASES from metadata
        if "geofence" in service_metadata:
            self.GEOFENCE = tuple(service_metadata["geofence"])
        if "aliases" in service_metadata:
            self.ALIASES = tuple(service_metadata["aliases"])

        # Create a session for API calls to the remote server
        self.api_session = requests.Session()
        self.api_session.headers.update({"X-API-Key": self.api_key, "Content-Type": "application/json"})

        # This session will receive data from remote for actual downloading
        self.session = requests.Session()

        # Store authentication state
        self.authenticated = False
        self.credential = None
        self.cookies_content = None  # Raw cookie file content to send to remote

        # Get profile from context if available
        self.profile = "default"
        if hasattr(ctx, "obj") and hasattr(ctx.obj, "profile"):
            self.profile = ctx.obj.profile or "default"

        # Initialize proxy providers for resolving proxy credentials
        self._proxy_providers = None
        if hasattr(ctx, "obj") and hasattr(ctx.obj, "proxy_providers"):
            self._proxy_providers = ctx.obj.proxy_providers

    def _resolve_proxy_locally(self, proxy: str) -> Optional[str]:
        """
        Resolve proxy parameter locally using client's proxy providers.

        This allows the client to resolve proxy providers (like NordVPN) and
        send the full proxy URI with credentials to the server.

        Args:
            proxy: Proxy parameter (e.g., "nordvpn:ca1066", "us2104", or full URI)

        Returns:
            Resolved proxy URI with credentials, or None if no_proxy
        """
        if not proxy:
            return None

        import re

        # If already a full URI, return as-is
        if re.match(r"^https?://", proxy):
            self.log.debug(f"Using explicit proxy URI: {proxy}")
            return proxy

        # Try to resolve using local proxy providers
        if self._proxy_providers:
            try:
                from unshackle.core.api.handlers import resolve_proxy

                resolved = resolve_proxy(proxy, self._proxy_providers)
                self.log.info(f"Resolved proxy '{proxy}' to: {resolved}")
                return resolved
            except Exception as e:
                self.log.warning(f"Failed to resolve proxy locally: {e}")
                # Fall back to sending proxy parameter as-is for server to resolve
                return proxy
        else:
            self.log.debug(f"No proxy providers available, sending proxy as-is: {proxy}")
            return proxy

    def _add_proxy_to_request(self, data: Dict[str, Any]) -> None:
        """
        Add resolved proxy information to request data.

        Resolves proxy using local proxy providers and adds to request.
        Server will use the resolved proxy URI (with credentials).

        Args:
            data: Request data dictionary to modify
        """
        if hasattr(self.ctx, "params"):
            no_proxy = self.ctx.params.get("no_proxy", False)
            proxy_param = self.ctx.params.get("proxy")

            if no_proxy:
                data["no_proxy"] = True
            elif proxy_param:
                # Resolve proxy locally to get credentials
                resolved_proxy = self._resolve_proxy_locally(proxy_param)
                if resolved_proxy:
                    data["proxy"] = resolved_proxy
                    self.log.debug(f"Sending resolved proxy to server: {resolved_proxy}")

    def _make_request(self, endpoint: str, data: Optional[Dict[str, Any]] = None, retry_count: int = 0) -> Dict[str, Any]:
        """
        Make an API request to the remote server with retry logic.

        Automatically handles authentication:
        1. Check for cached session - send with request if found
        2. If session expired, re-authenticate automatically
        3. If no session, send credentials (server tries to auth)
        4. If server returns AUTH_REQUIRED, authenticate locally
        5. Retry request with new session

        Args:
            endpoint: API endpoint path (e.g., "/api/remote/DSNP/titles")
            data: Optional JSON data to send
            retry_count: Current retry attempt (for internal use)

        Returns:
            Response JSON data

        Raises:
            ConnectionError: If the request fails after all retries
        """
        url = f"{self.remote_url}{endpoint}"
        max_retries = 3  # Max network retries
        retry_delays = [2, 4, 8]  # Exponential backoff in seconds

        # Ensure data is a dictionary
        if data is None:
            data = {}

        # Priority 1: Check for pre-authenticated session in local cache
        cache = get_local_session_cache()
        cached_session = cache.get_session(self.remote_url, self.service_tag, self.profile)

        if cached_session:
            # Send pre-authenticated session data (server never stores it)
            self.log.debug(f"Using cached session for {self.service_tag}")
            data["pre_authenticated_session"] = cached_session
        else:
            # Priority 2: Fallback to credentials/cookies (old behavior)
            # This allows server to authenticate if no local session exists
            if self.cookies_content:
                data["cookies"] = self.cookies_content

            if self.credential:
                data["credential"] = {"username": self.credential.username, "password": self.credential.password}

        try:
            if data:
                response = self.api_session.post(url, json=data)
            else:
                response = self.api_session.get(url)

            response.raise_for_status()
            result = response.json()

            # Check if session expired - re-authenticate automatically
            if result.get("error_code") == "SESSION_EXPIRED":
                console.print(f"[yellow]Session expired for {self.service_tag}[/yellow]")
                console.print("[cyan]Re-authenticating...[/cyan]")

                # Delete expired session from cache
                cache.delete_session(self.remote_url, self.service_tag, self.profile)

                # Perform local authentication
                session_data = self._authenticate_locally()

                if session_data:
                    # Save to cache for future requests
                    cache.store_session(
                        remote_url=self.remote_url,
                        service_tag=self.service_tag,
                        profile=self.profile,
                        session_data=session_data
                    )

                    # Retry request with new session
                    data["pre_authenticated_session"] = session_data
                    # Remove old auth data
                    data.pop("cookies", None)
                    data.pop("credential", None)

                    # Retry the request
                    response = self.api_session.post(url, json=data)
                    response.raise_for_status()
                    result = response.json()

            # Check if server requires authentication
            elif result.get("error_code") == "AUTH_REQUIRED" and not cached_session:
                console.print(f"[yellow]Authentication required for {self.service_tag}[/yellow]")
                console.print("[cyan]Authenticating locally...[/cyan]")

                # Perform local authentication
                session_data = self._authenticate_locally()

                if session_data:
                    # Save to cache for future requests
                    cache.store_session(
                        remote_url=self.remote_url,
                        service_tag=self.service_tag,
                        profile=self.profile,
                        session_data=session_data
                    )

                    # Retry request with authenticated session
                    data["pre_authenticated_session"] = session_data
                    # Remove old auth data
                    data.pop("cookies", None)
                    data.pop("credential", None)

                    # Retry the request
                    response = self.api_session.post(url, json=data)
                    response.raise_for_status()
                    result = response.json()

            # Apply session data if present
            if "session" in result:
                deserialize_session(result["session"], self.session)

            return result

        except requests.RequestException as e:
            # Retry on network errors with exponential backoff
            if retry_count < max_retries:
                delay = retry_delays[retry_count]
                self.log.warning(f"Request failed (attempt {retry_count + 1}/{max_retries + 1}): {e}")
                self.log.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
                return self._make_request(endpoint, data, retry_count + 1)
            else:
                self.log.error(f"Remote API request failed after {max_retries + 1} attempts: {e}")
                raise ConnectionError(f"Failed to communicate with remote server after {max_retries + 1} attempts: {e}")

    def _authenticate_locally(self) -> Optional[Dict[str, Any]]:
        """
        Authenticate the service locally when server requires it.

        This performs interactive authentication (browser, 2FA, etc.)
        and returns the authenticated session.

        Returns:
            Serialized session data or None if authentication fails
        """
        from unshackle.core.remote_auth import RemoteAuthenticator

        try:
            authenticator = RemoteAuthenticator(self.remote_url, self.api_key)
            session_data = authenticator.authenticate_service_locally(self.service_tag, self.profile)
            console.print("[green]✓ Authentication successful![/green]")
            return session_data

        except Exception as e:
            console.print(f"[red]✗ Authentication failed: {e}[/red]")
            self.log.error(f"Local authentication failed: {e}")
            return None

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        """
        Prepare authentication data to send to remote service.

        Stores cookies and credentials to send with each API request.
        The remote server will use these for authentication.

        Args:
            cookies: Cookie jar from local configuration
            credential: Credentials from local configuration
        """
        self.log.info("Preparing authentication for remote server...")
        self.credential = credential

        # Read cookies file content if cookies provided
        if cookies and hasattr(cookies, "filename") and cookies.filename:
            try:
                from pathlib import Path

                cookie_file = Path(cookies.filename)
                if cookie_file.exists():
                    self.cookies_content = cookie_file.read_text()
                    self.log.info(f"Loaded cookies from {cookie_file}")
            except Exception as e:
                self.log.warning(f"Could not read cookie file: {e}")

        self.authenticated = True
        self.log.info("Authentication data ready for remote server")

    def search(self, query: Optional[str] = None) -> Generator[SearchResult, None, None]:
        """
        Search for content on the remote service.

        Args:
            query: Search query string

        Yields:
            SearchResult objects
        """
        if query is None:
            query = self.kwargs.get("query", "")

        self.log.info(f"Searching remote service for: {query}")

        data = {"query": query}

        # Add proxy information (resolved locally with credentials)
        self._add_proxy_to_request(data)

        response = self._make_request(f"/api/remote/{self.service_tag}/search", data)

        if response.get("status") == "success" and "results" in response:
            for result in response["results"]:
                yield SearchResult(
                    id_=result["id"],
                    title=result["title"],
                    description=result.get("description"),
                    label=result.get("label"),
                    url=result.get("url"),
                )

    def get_titles(self) -> Union[Movies, Series]:
        """
        Get titles from the remote service.

        Returns:
            Movies or Series object containing title information
        """
        title = self.kwargs.get("title")

        if not title:
            raise ValueError("No title provided")

        self.log.info(f"Getting titles from remote service for: {title}")

        data = {"title": title}

        # Add additional parameters
        for key, value in self.kwargs.items():
            if key not in ["title"]:
                data[key] = value

        # Add proxy information (resolved locally with credentials)
        self._add_proxy_to_request(data)

        response = self._make_request(f"/api/remote/{self.service_tag}/titles", data)

        if response.get("status") != "success" or "titles" not in response:
            raise ValueError(f"Failed to get titles from remote: {response.get('message', 'Unknown error')}")

        titles_data = response["titles"]

        # Deserialize titles
        titles = []
        for title_info in titles_data:
            if title_info["type"] == "movie":
                titles.append(
                    Movie(
                        id_=title_info.get("id", title),
                        service=self.__class__,
                        name=title_info["name"],
                        year=title_info.get("year"),
                        data=title_info,
                    )
                )
            elif title_info["type"] == "episode":
                titles.append(
                    Episode(
                        id_=title_info.get("id", title),
                        service=self.__class__,
                        title=title_info.get("series_title", title_info["name"]),
                        season=title_info.get("season", 0),
                        number=title_info.get("number", 0),
                        name=title_info.get("name"),
                        year=title_info.get("year"),
                        data=title_info,
                    )
                )

        # Return appropriate container
        if titles and isinstance(titles[0], Episode):
            return Series(titles)
        else:
            return Movies(titles)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        """
        Get tracks from the remote service.

        Args:
            title: Title object to get tracks for

        Returns:
            Tracks object containing video, audio, and subtitle tracks
        """
        self.log.info(f"Getting tracks from remote service for: {title}")

        title_input = self.kwargs.get("title")
        data = {"title": title_input}

        # Add episode information if applicable
        if isinstance(title, Episode):
            data["season"] = title.season
            data["episode"] = title.number

        # Add additional parameters
        for key, value in self.kwargs.items():
            if key not in ["title"]:
                data[key] = value

        # Add proxy information (resolved locally with credentials)
        self._add_proxy_to_request(data)

        response = self._make_request(f"/api/remote/{self.service_tag}/tracks", data)

        if response.get("status") != "success":
            raise ValueError(f"Failed to get tracks from remote: {response.get('message', 'Unknown error')}")

        # Handle multiple episodes response
        if "episodes" in response:
            # For multiple episodes, return tracks for the matching title
            for episode_data in response["episodes"]:
                episode_title = episode_data["title"]
                if (
                    isinstance(title, Episode)
                    and episode_title.get("season") == title.season
                    and episode_title.get("number") == title.number
                ):
                    return self._deserialize_tracks(episode_data, title)

            raise ValueError(f"Could not find tracks for {title.season}x{title.number} in remote response")

        # Single title response
        return self._deserialize_tracks(response, title)

    def _deserialize_tracks(self, data: Dict[str, Any], title: Union[Movie, Episode]) -> Tracks:
        """
        Deserialize tracks from API response.

        Args:
            data: Track data from API
            title: Title object these tracks belong to

        Returns:
            Tracks object
        """
        tracks = Tracks()

        # Deserialize video tracks
        for video_data in data.get("video", []):
            video = Video(
                id_=video_data["id"],
                url="",  # URL will be populated during download from manifests
                codec=Video.Codec[video_data["codec"]],
                bitrate=video_data.get("bitrate", 0) * 1000 if video_data.get("bitrate") else None,
                width=video_data.get("width"),
                height=video_data.get("height"),
                fps=video_data.get("fps"),
                range_=Video.Range[video_data["range"]] if video_data.get("range") else None,
                language=video_data.get("language"),
                drm=video_data.get("drm"),
            )
            tracks.add(video)

        # Deserialize audio tracks
        for audio_data in data.get("audio", []):
            audio = Audio(
                id_=audio_data["id"],
                url="",  # URL will be populated during download
                codec=Audio.Codec[audio_data["codec"]],
                bitrate=audio_data.get("bitrate", 0) * 1000 if audio_data.get("bitrate") else None,
                channels=audio_data.get("channels"),
                language=audio_data.get("language"),
                descriptive=audio_data.get("descriptive", False),
                drm=audio_data.get("drm"),
            )
            if audio_data.get("atmos"):
                audio.atmos = True
            tracks.add(audio)

        # Deserialize subtitle tracks
        for subtitle_data in data.get("subtitles", []):
            subtitle = Subtitle(
                id_=subtitle_data["id"],
                url="",  # URL will be populated during download
                codec=Subtitle.Codec[subtitle_data["codec"]],
                language=subtitle_data.get("language"),
                forced=subtitle_data.get("forced", False),
                sdh=subtitle_data.get("sdh", False),
                cc=subtitle_data.get("cc", False),
            )
            tracks.add(subtitle)

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        """
        Get chapters from the remote service.

        Args:
            title: Title object to get chapters for

        Returns:
            Chapters object
        """
        self.log.info(f"Getting chapters from remote service for: {title}")

        title_input = self.kwargs.get("title")
        data = {"title": title_input}

        # Add episode information if applicable
        if isinstance(title, Episode):
            data["season"] = title.season
            data["episode"] = title.number

        # Add proxy information (resolved locally with credentials)
        self._add_proxy_to_request(data)

        response = self._make_request(f"/api/remote/{self.service_tag}/chapters", data)

        if response.get("status") != "success":
            self.log.warning(f"Failed to get chapters from remote: {response.get('message', 'Unknown error')}")
            return Chapters()

        chapters = Chapters()
        for chapter_data in response.get("chapters", []):
            chapters.add(Chapter(timestamp=chapter_data["timestamp"], name=chapter_data.get("name")))

        return chapters

    @staticmethod
    def get_session() -> requests.Session:
        """
        Create a session for the remote service.

        Returns:
            A requests.Session object
        """
        session = requests.Session()
        return session
