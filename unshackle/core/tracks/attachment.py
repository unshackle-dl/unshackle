from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse
from zlib import crc32

import requests

from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_LICENCE_ONLY
from unshackle.core.session import RnetSession

AnySession = Union[requests.Session, RnetSession]


class Attachment:
    def __init__(
        self,
        path: Union[Path, str, None] = None,
        url: Optional[str] = None,
        name: Optional[str] = None,
        mime_type: Optional[str] = None,
        description: Optional[str] = None,
        session: Optional[AnySession] = None,
    ):
        """
        Make a new Attachment.

        If providing a path, the file must already exist.
        If providing a URL, download() fetches the file during the download phase.
        You must give either path or url.

        If name is not provided it will use the file name (without extension).
        If mime_type is not provided, it will try to guess it.

        Args:
            path: Path to an existing file.
            url: URL to download the attachment from.
            name: Name of the attachment.
            mime_type: MIME type of the attachment.
            description: Description of the attachment.
            session: Optional requests HTTP session to download the file with.
        """
        if path is None and url is None:
            raise ValueError("Either path or url must be provided.")

        self.url = url
        self.session = session
        self.file_name: Optional[str] = None

        if url:
            if not isinstance(url, str):
                raise ValueError("The attachment URL must be a string.")

            parsed_url = urlparse(url)
            file_name = os.path.basename(parsed_url.path) or "attachment"

            if name:
                safe_name = re.sub(r'[<>:"/\\|?*]', "", name).replace(" ", "_")
                file_name = f"{safe_name}{os.path.splitext(file_name)[1]}"

            self.file_name = file_name

        if path is not None and not isinstance(path, (str, Path)):
            raise ValueError(f"Invalid attachment path type: expected str or Path, got {type(path).__name__}.")

        if path is not None:
            path = Path(path)
            if not path.exists():
                raise ValueError("The attachment file does not exist.")

        if path is not None:
            name = (name or path.stem).strip()
        else:
            name = (name or Path(file_name).stem).strip()
        mime_type = (mime_type or "").strip() or None
        description = (description or "").strip() or None

        if not mime_type:
            suffix = path.suffix.lower() if path is not None else Path(file_name).suffix.lower()
            mime_type = {
                ".ttf": "application/x-truetype-font",
                ".otf": "application/vnd.ms-opentype",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
            }.get(suffix, mimetypes.guess_type(file_name if path is None else path)[0])
            if not mime_type:
                raise ValueError("The attachment mime-type could not be automatically detected.")

        self.path = path
        self.name = name
        self.mime_type = mime_type
        self.description = description

    def download(
        self,
        session: Optional[AnySession] = None,
        *,
        no_proxy_download: bool = False,
    ) -> None:
        """Download a URL-backed attachment to the temp directory."""
        if self.path is not None or not self.url or DOWNLOAD_LICENCE_ONLY.is_set():
            return

        from unshackle.core.tracks.track import direct_session

        session = session or self.session or requests.Session()
        if no_proxy_download and any(session.proxies.values()):
            session = direct_session(session)

        download_path = config.directories.temp / (self.file_name or "attachment")
        try:
            response = session.get(self.url, stream=True)
            response.raise_for_status()
            download_path.parent.mkdir(parents=True, exist_ok=True)
            with open(download_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
        except Exception as e:
            raise ValueError(f"Failed to download attachment from URL: {e}")

        self.path = download_path

    def __repr__(self) -> str:
        return "{name}({items})".format(
            name=self.__class__.__name__, items=", ".join([f"{k}={repr(v)}" for k, v in self.__dict__.items()])
        )

    def __str__(self) -> str:
        return " | ".join(filter(bool, ["ATT", self.name, self.mime_type, self.description]))

    def to_dict(self) -> dict[str, Optional[str]]:
        """Serialise a URL-backed attachment for export/import."""
        return {"url": self.url, "name": self.name, "mime_type": self.mime_type, "description": self.description}

    @property
    def id(self) -> str:
        """Compute an ID from the attachment data."""
        if self.path and self.path.exists():
            checksum = crc32(self.path.read_bytes())
        elif self.url:
            checksum = crc32(self.url.encode("utf8"))
        else:
            checksum = crc32(self.name.encode("utf8"))
        return hex(checksum)

    def delete(self) -> None:
        if self.path and self.path.exists():
            self.path.unlink()
        self.path = None

    @classmethod
    def from_url(
        cls,
        url: str,
        name: Optional[str] = None,
        mime_type: Optional[str] = None,
        description: Optional[str] = None,
        session: Optional[AnySession] = None,
    ) -> "Attachment":
        """
        Make an attachment from a URL.

        Args:
            url: URL to download the attachment from.
            name: Name of the attachment.
            mime_type: MIME type of the attachment.
            description: Description of the attachment.
            session: Optional requests HTTP session to download the file with.

        Returns:
            Attachment: A new attachment instance.
        """
        return cls(url=url, name=name, mime_type=mime_type, description=description, session=session)


__all__ = ("Attachment",)
