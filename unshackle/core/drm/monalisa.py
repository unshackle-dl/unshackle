"""
MonaLisa DRM System.

A WASM-based DRM system that uses local content key extraction from ticket data.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Union
from uuid import UUID

from unshackle.core.cdm.monalisa import MonaLisaCDM

log = logging.getLogger(__name__)


class MonaLisa:
    """
    MonaLisa DRM System.

    Unlike Widevine/PlayReady, MonaLisa does not use a challenge/response flow
    with a license server. Instead, the service API gives the PSSH value (ticket)
    directly, and a WASM module extracts the content keys locally.
    """

    class Exceptions:
        class TicketNotFound(Exception):
            """Raised when the caller gives no PSSH/ticket data."""

        class KeyExtractionFailed(Exception):
            """Raised when content key extraction from the ticket fails."""

    def __init__(
        self,
        ticket: Union[str, bytes],
        device_path: Path,
        tool_path: Optional[Path] = None,
        **kwargs: Any,
    ):
        """
        Initialise MonaLisa DRM.

        Args:
            ticket: PSSH value from service API (base64 string or raw bytes).
            device_path: Path to the CDM device file (.mld).
            tool_path: Optional path to an external decryption binary.
            **kwargs: Additional metadata stored in self.data.

        Raises:
            TicketNotFound: If ticket/PSSH is empty.
            KeyExtractionFailed: If content key extraction fails.
        """
        if not ticket:
            raise MonaLisa.Exceptions.TicketNotFound("No PSSH/ticket data provided.")

        self._ticket = ticket
        self._device_path = device_path
        self._tool_path = tool_path
        self._decrypted_segments: int = 0
        self._kid: Optional[UUID] = None
        self._key: Optional[str] = None
        self.data: dict = kwargs or {}

        self.extract_keys()

    def extract_keys(self) -> None:
        """Extract keys from the ticket using the MonaLisa CDM."""

        try:
            cdm = MonaLisaCDM(device_path=self._device_path)
            session_id = cdm.open()
            try:
                keys = cdm.extract_keys(self._ticket)
                if keys:
                    kid_hex = keys.get("kid")
                    if kid_hex:
                        self._kid = UUID(hex=kid_hex)
                    self._key = keys.get("key")
            finally:
                cdm.close(session_id)
        except Exception as e:
            raise MonaLisa.Exceptions.KeyExtractionFailed(f"Failed to extract keys: {e}")

    @classmethod
    def from_ticket(
        cls,
        ticket: Union[str, bytes],
        device_path: Path,
        tool_path: Optional[Path] = None,
    ) -> MonaLisa:
        """
        Make a MonaLisa DRM instance from a PSSH/ticket.

        Args:
            ticket: PSSH value from service API.
            device_path: Path to the CDM device file (.mld).
            tool_path: Optional path to an external decryption binary.

        Returns:
            MonaLisa DRM instance with extracted keys.
        """
        return cls(
            ticket=ticket,
            device_path=device_path,
            tool_path=tool_path,
        )

    @property
    def kid(self) -> Optional[UUID]:
        """Get the Key ID."""
        return self._kid

    @property
    def key(self) -> Optional[str]:
        """Get the content key as hex string."""
        return self._key

    @property
    def pssh(self) -> str:
        """
        Get the raw PSSH/ticket value as a string.

        Returns:
            The raw PSSH value as a base64 string.
        """
        if isinstance(self._ticket, bytes):
            try:
                return self._ticket.decode("utf-8")
            except UnicodeDecodeError:
                # Tickets are typically base64, so ASCII is a reasonable fallback.
                try:
                    return self._ticket.decode("ascii")
                except UnicodeDecodeError as e:
                    raise ValueError(
                        f"Ticket bytes must be UTF-8 text or ASCII base64; got undecodable bytes (len={len(self._ticket)})"
                    ) from e
        return self._ticket

    @property
    def content_id(self) -> Optional[str]:
        """
        Extract the Content ID from the PSSH for display.

        The PSSH contains an embedded Content ID at bytes 21-75 with format:
        H5DCID-V3-P1-YYYYMMDD-HHMMSS-MEDIAID-TIMESTAMP-SUFFIX

        Returns:
            The Content ID string if extractable, None otherwise.
        """
        try:
            if isinstance(self._ticket, bytes):
                data = self._ticket
            else:
                data = base64.b64decode(self._ticket)

            # Content ID is at bytes 21-75 (55 bytes)
            if len(data) >= 76:
                content_id = data[21:76].decode("ascii")
                if content_id.startswith("H5DCID-"):
                    return content_id
        except (ValueError, TypeError):
            pass

        return None

    @property
    def content_keys(self) -> dict[UUID, str]:
        """
        Get content keys in the same format as Widevine/PlayReady.

        Returns:
            Dictionary mapping KID to the content key hex string.
        """
        if self._kid and self._key:
            return {self._kid: self._key}
        return {}

    @property
    def key_pair(self) -> str:
        """Get formatted KID:KEY pair for CLI decryptors (32hex:32hex)."""
        if self._kid and self._key:
            return f"{self._kid.hex}:{self._key}"
        return ""

    def decrypt_segment(self, segment_path: Path, output_path: Optional[Path] = None) -> None:
        """
        Decrypt an individual segment file using the configured CLI tool via subprocess.

        Args:
            segment_path: Path to the segment file to decrypt.
            output_path: Optional path for the decrypted output segment.
        """
        self._execute_decrypt(segment_path, output_path)
        self._decrypted_segments += 1

    def decrypt(self, path: Path, output_path: Optional[Path] = None, *, is_segment: bool = False) -> None:
        """
        Decrypt a media file (segment or track) using the configured CLI tool via subprocess.

        Args:
            path: Path to the target file to decrypt.
            output_path: Optional path for the decrypted output file.
            is_segment: Set True if this is an individual segment rather than a full track.
        """
        if is_segment:
            return self.decrypt_segment(path, output_path)

        # If segments were already decrypted during chunk downloads, skip reprocessing on final track.
        if self._decrypted_segments > 0:
            log.debug(
                "MonaLisa: Track already decrypted during segment downloads (%s segments)", self._decrypted_segments
            )
            return

        self._execute_decrypt(path, output_path)

    def _execute_decrypt(self, path: Path, output_path: Optional[Path] = None) -> None:
        """Execute external CLI tool via subprocess to decrypt the target file."""
        if not path or not path.exists():
            raise ValueError(f"Target file does not exist: {path}")

        if not self._tool_path or not self._tool_path.exists():
            return

        if not self.key or not self.kid:
            raise MonaLisa.Exceptions.KeyExtractionFailed("Missing content key for decryption.")

        target_output = output_path or path
        temp_enc = path.with_name(f"{path.name}.temp_enc")

        try:
            if path.exists():
                path.replace(temp_enc)
            else:
                raise FileNotFoundError(f"Target file disappeared: {path}")

            cmd = [
                str(self._tool_path),
                "-i",
                str(temp_enc),
                "-o",
                str(target_output),
                "-k",
                self.key_pair,
            ]

            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            process = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=startupinfo,
                timeout=60,
            )

            if process.returncode != 0 or not target_output.exists():
                raise RuntimeError(f"MonaLisa decryption failed for {path.name}: {process.stderr or 'unknown error'}")

        finally:
            if temp_enc.exists():
                try:
                    temp_enc.unlink()
                except Exception:
                    pass
