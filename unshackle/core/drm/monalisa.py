"""
MonaLisa DRM System.

A WASM-based DRM system that uses local key extraction and two-stage
segment decryption (ML-Worker binary + AES-ECB).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Union
from uuid import UUID

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import unpad

log = logging.getLogger(__name__)


class MonaLisa:
    """
    MonaLisa DRM System.

    Unlike Widevine/PlayReady, MonaLisa does not use a challenge/response flow
    with a license server. Instead, the PSSH value (ticket) is provided directly
    by the service API, and keys are extracted locally via a WASM module.

    Decryption is performed in two stages:
    1. ML-Worker binary: Removes MonaLisa encryption layer (bbts -> ents)
    2. AES-ECB decryption: Final decryption with service-provided key
    """

    class Exceptions:
        class TicketNotFound(Exception):
            """Raised when no PSSH/ticket data is provided."""

        class KeyExtractionFailed(Exception):
            """Raised when key extraction from the ticket fails."""

        class WorkerNotFound(Exception):
            """Raised when the ML-Worker binary is not found."""

        class DecryptionFailed(Exception):
            """Raised when segment decryption fails."""

    def __init__(
        self,
        ticket: Union[str, bytes],
        aes_key: Union[str, bytes],
        device_path: Path,
        **kwargs: Any,
    ):
        """
        Initialize MonaLisa DRM.

        Args:
            ticket: PSSH value from service API (base64 string or raw bytes).
            aes_key: AES-ECB key for second-stage decryption (hex string or bytes).
            device_path: Path to the CDM device file (.mld).
            **kwargs: Additional metadata stored in self.data.

        Raises:
            TicketNotFound: If ticket/PSSH is empty.
            KeyExtractionFailed: If key extraction fails.
        """
        if not ticket:
            raise MonaLisa.Exceptions.TicketNotFound("No PSSH/ticket data provided.")

        self._ticket = ticket

        # Store AES key for second-stage decryption
        if isinstance(aes_key, str):
            self._aes_key = bytes.fromhex(aes_key)
        else:
            self._aes_key = aes_key

        self._device_path = device_path
        self._kid: Optional[UUID] = None
        self._key: Optional[str] = None
        self.data: dict = kwargs or {}

        # Extract keys immediately
        self._extract_keys()

    def _extract_keys(self) -> None:
        """Extract keys from the ticket using the MonaLisa CDM."""
        # Import here to avoid circular import
        from unshackle.core.cdm.monalisa import MonaLisaCDM

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
        aes_key: Union[str, bytes],
        device_path: Path,
    ) -> MonaLisa:
        """
        Create a MonaLisa DRM instance from a PSSH/ticket.

        Args:
            ticket: PSSH value from service API.
            aes_key: AES-ECB key for second-stage decryption.
            device_path: Path to the CDM device file (.mld).

        Returns:
            MonaLisa DRM instance with extracted keys.
        """
        return cls(ticket=ticket, aes_key=aes_key, device_path=device_path)

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
        import base64

        try:
            # Decode base64 PSSH to get raw bytes
            if isinstance(self._ticket, bytes):
                data = self._ticket
            else:
                data = base64.b64decode(self._ticket)

            # Content ID is at bytes 21-75 (55 bytes)
            if len(data) >= 76:
                content_id = data[21:76].decode("ascii")
                # Validate it looks like a content ID
                if content_id.startswith("H5DCID-"):
                    return content_id
        except Exception:
            pass

        return None

    @property
    def content_keys(self) -> dict[UUID, str]:
        """
        Get content keys in the same format as Widevine/PlayReady.

        Returns:
            Dictionary mapping KID to key hex string.
        """
        if self._kid and self._key:
            return {self._kid: self._key}
        return {}

    def decrypt_segment(self, segment_path: Path) -> None:
        """
        Decrypt a single segment using two-stage decryption.

        Stage 1: ML-Worker binary (bbts -> ents)
        Stage 2: AES-ECB decryption (ents -> ts)

        Args:
            segment_path: Path to the encrypted segment file.

        Raises:
            WorkerNotFound: If ML-Worker binary is not available.
            DecryptionFailed: If decryption fails at any stage.
        """
        if not self._key:
            return

        # Import here to avoid circular import
        from unshackle.core.cdm.monalisa import MonaLisaCDM

        worker_path = MonaLisaCDM.get_worker_path()
        if not worker_path or not worker_path.exists():
            raise MonaLisa.Exceptions.WorkerNotFound("ML-Worker not found.")

        bbts_path = segment_path.with_suffix(".bbts")
        ents_path = segment_path.with_suffix(".ents")

        try:
            if segment_path.exists():
                segment_path.replace(bbts_path)
            else:
                raise MonaLisa.Exceptions.DecryptionFailed(f"Segment file does not exist: {segment_path}")

            # Stage 1: ML-Worker decryption
            cmd = [str(worker_path), str(self._key), str(bbts_path), str(ents_path)]

            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            worker_timeout_s = 60
            process = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                startupinfo=startupinfo,
                timeout=worker_timeout_s,
            )

            if process.returncode != 0:
                raise MonaLisa.Exceptions.DecryptionFailed(
                    f"ML-Worker failed for {segment_path.name}: {process.stderr}"
                )

            if not ents_path.exists():
                raise MonaLisa.Exceptions.DecryptionFailed(
                    f"Decrypted .ents file was not created for {segment_path.name}"
                )

            # Stage 2: AES-ECB decryption
            with open(ents_path, "rb") as f:
                ents_data = f.read()

            crypto = AES.new(self._aes_key, AES.MODE_ECB)
            decrypted_data = unpad(crypto.decrypt(ents_data), AES.block_size)

            # Write decrypted segment back to original path
            with open(segment_path, "wb") as f:
                f.write(decrypted_data)

        except MonaLisa.Exceptions.DecryptionFailed:
            raise
        except subprocess.TimeoutExpired as e:
            log.error("ML-Worker timed out after %ss for %s", worker_timeout_s, segment_path.name)
            raise MonaLisa.Exceptions.DecryptionFailed(
                f"ML-Worker timed out after {worker_timeout_s}s for {segment_path.name}"
            ) from e
        except Exception as e:
            raise MonaLisa.Exceptions.DecryptionFailed(f"Failed to decrypt segment {segment_path.name}: {e}")
        finally:
            if ents_path.exists():
                os.remove(ents_path)
            if bbts_path != segment_path and bbts_path.exists():
                os.remove(bbts_path)

    def decrypt(self, _path: Path) -> None:
        """
        MonaLisa uses per-segment decryption during download via the
        on_segment_downloaded callback. By the time this method is called,
        the content has already been decrypted and muxed into a container.

        Args:
            path: Path to the file (ignored).
        """
        pass
