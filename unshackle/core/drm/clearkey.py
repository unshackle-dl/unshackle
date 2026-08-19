from __future__ import annotations

import base64
import logging
import shutil
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urljoin

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import unpad
from m3u8.model import Key
from requests import Session

from unshackle.core.session import RnetSession
from unshackle.core.utilities import log_event

log = logging.getLogger(__name__)

# MPEG-TS: 188-byte packets, sync byte 0x47 at every packet boundary (shared with sample_aes).
SYNC_BYTE = 0x47
TS_PACKET_SIZE = 188
# Consecutive packet boundaries that must carry the sync byte to call a segment clear MPEG-TS.
TS_SYNC_CHECKS = 4


class ClearKey:
    """AES-128 ClearKey (HLS) DRM system."""

    def __init__(self, key: Union[bytes, str], iv: Optional[Union[bytes, str]] = None):
        """
        Give the IV where possible. If you do not give an IV, unshackle sets it to
        \x00 of the same bit-size as the content key.
        """
        if isinstance(key, str):
            key = bytes.fromhex(key.replace("0x", ""))
        if not isinstance(key, bytes):
            raise ValueError(f"Expected AES Key to be bytes, not {key!r}")
        if not iv:
            iv = b"\x00"
        if isinstance(iv, str):
            iv = bytes.fromhex(iv.replace("0x", ""))
        if not isinstance(iv, bytes):
            raise ValueError(f"Expected IV to be bytes, not {iv!r}")

        if len(iv) < len(key):
            iv = iv * (len(key) - len(iv) + 1)

        self.key: bytes = key
        self.iv: bytes = iv
        self._warned_clear: bool = False

    def warn_clear(self, reason: str) -> None:
        """Warn once per track that unshackle passed a segment through undecrypted."""
        if not self._warned_clear:
            log.warning("ClearKey: manifest signaled AES-128 encryption but %s; passing segment through.", reason)
            self._warned_clear = True

    def decrypt(self, path: Path) -> None:
        """Decrypt a Track with AES-128 ClearKey (HLS) DRM."""
        if not path or not path.exists():
            raise ValueError("Tried to decrypt a file that does not exist.")

        data = path.read_bytes()

        # Mislabeled-clear guard: plaintext MPEG-TS the manifest wrongly tags AES-128. Require the
        # 0x47 sync byte at 4 consecutive packet boundaries plus an exact packet-count length, so
        # real CBC ciphertext matching all of these is negligible (~1/256^4).
        if (
            len(data) % TS_PACKET_SIZE == 0
            and len(data) >= TS_PACKET_SIZE * TS_SYNC_CHECKS
            and all(data[i * TS_PACKET_SIZE] == SYNC_BYTE for i in range(TS_SYNC_CHECKS))
        ):
            self.warn_clear("the segment is clear MPEG-TS")
            return

        # Valid AES-128-CBC ciphertext is always a 16-byte multiple; anything else is not
        # ciphertext (truncated/clear/SAMPLE-AES). Decrypting would raise, so pass it through.
        if len(data) % AES.block_size != 0:
            self.warn_clear(f"the segment length ({len(data)} bytes) is not a multiple of {AES.block_size}")
            return

        log_event(
            "drm_decrypt",
            level="DEBUG",
            message=f"Decrypting {path.name} with AES-CBC",
            drm_type="ClearKey",
            file=path.name,
        )

        decrypted = AES.new(self.key, AES.MODE_CBC, self.iv).decrypt(data)

        try:
            decrypted = unpad(decrypted, AES.block_size)
        except ValueError:
            # the decrypted data is likely already in the block size boundary
            pass

        decrypted_path = path.with_suffix(f".decrypted{path.suffix}")
        decrypted_path.write_bytes(decrypted)

        path.unlink()
        shutil.move(decrypted_path, path)

    @classmethod
    def from_m3u_key(cls, m3u_key: Key, session: Optional[Session] = None) -> ClearKey:
        """
        Load a ClearKey from an M3U(8) Playlist's EXT-X-KEY.

        Parameters:
            m3u_key: A `Key` object parsed from a m3u(8) playlist using
                the `m3u8` library.
            session: Optional HTTP session used to request external URIs with.
                Useful to set headers, proxies, cookies, and so forth.
        """
        if not isinstance(m3u_key, Key):
            raise ValueError(f"Provided M3U Key is in an unexpected type {m3u_key!r}")
        if not isinstance(session, (Session, RnetSession, type(None))):
            raise TypeError(f"Expected session to be a {Session} or {RnetSession}, not a {type(session)}")

        if not m3u_key.method.startswith("AES"):
            raise ValueError(f"Provided M3U Key is not an AES Clear Key, {m3u_key.method}")
        if not m3u_key.uri:
            raise ValueError("No URI in M3U Key, unable to get Key.")

        if not session:
            session = Session()

        if not session.headers.get("User-Agent"):
            # commonly needed default for HLS playlists
            session.headers["User-Agent"] = "smartexoplayer/1.1.0 (Linux;Android 8.0.0) ExoPlayerLib/2.13.3"

        if m3u_key.uri.startswith("data:"):
            media_types, payload = m3u_key.uri[5:].split(",", 1)
            media_types = media_types.split(";")
            # Materialise the key bytes here so the 16-byte check covers both encodings: base64
            # decodes to bytes, a raw payload is latin-1 bytes. A raw str would otherwise reach
            # __init__ and be reinterpreted through fromhex.
            if "base64" in media_types:
                key = base64.b64decode(payload)
            else:
                key = payload.encode("latin-1")
            if len(key) != 16:
                raise ValueError(f"Unexpected Length of Key ({len(key)} bytes) in M3U data: URI.")
        else:
            url = urljoin(m3u_key.base_uri, m3u_key.uri)
            log_event(
                "drm_key_fetch",
                level="DEBUG",
                message="Fetching ClearKey from M3U key URI",
                drm_type="ClearKey",
                request={"url": url},
            )
            res = session.get(url)
            res.raise_for_status()
            if not res.content:
                raise EOFError("Unexpected Empty Response by M3U Key URI.")
            if len(res.content) < 16:
                raise EOFError(f"Unexpected Length of Key ({len(res.content)} bytes) in M3U Key.")
            key = res.content

        if m3u_key.iv:
            iv = bytes.fromhex(m3u_key.iv.replace("0x", ""))
        else:
            iv = None

        return cls(key=key, iv=iv)


__all__ = ("ClearKey",)
