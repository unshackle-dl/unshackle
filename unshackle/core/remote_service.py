"""Remote service implementation for connecting to remote unshackle servers."""

import logging
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

    def _make_request(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Make an API request to the remote server.

        Automatically includes cookies and credentials in the request.

        Args:
            endpoint: API endpoint path (e.g., "/api/remote/DSNP/titles")
            data: Optional JSON data to send

        Returns:
            Response JSON data

        Raises:
            ConnectionError: If the request fails
        """
        url = f"{self.remote_url}{endpoint}"

        # Ensure data is a dictionary
        if data is None:
            data = {}

        # Add cookies and credentials to request if available
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

            # Apply session data if present
            if "session" in result:
                deserialize_session(result["session"], self.session)

            return result

        except requests.RequestException as e:
            self.log.error(f"Remote API request failed: {e}")
            raise ConnectionError(f"Failed to communicate with remote server: {e}")

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

        # Add any additional parameters
        if hasattr(self.ctx, "params"):
            if self.ctx.params.get("proxy"):
                data["proxy"] = self.ctx.params["proxy"]
            if self.ctx.params.get("no_proxy"):
                data["no_proxy"] = True

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

        # Add context parameters
        if hasattr(self.ctx, "params"):
            if self.ctx.params.get("proxy"):
                data["proxy"] = self.ctx.params["proxy"]
            if self.ctx.params.get("no_proxy"):
                data["no_proxy"] = True

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

        # Add context parameters
        if hasattr(self.ctx, "params"):
            if self.ctx.params.get("proxy"):
                data["proxy"] = self.ctx.params["proxy"]
            if self.ctx.params.get("no_proxy"):
                data["no_proxy"] = True

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

        # Add context parameters
        if hasattr(self.ctx, "params"):
            if self.ctx.params.get("proxy"):
                data["proxy"] = self.ctx.params["proxy"]
            if self.ctx.params.get("no_proxy"):
                data["no_proxy"] = True

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
