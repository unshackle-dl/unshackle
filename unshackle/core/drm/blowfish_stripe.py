from __future__ import annotations

from pathlib import Path
from typing import Union

from Cryptodome.Cipher import Blowfish

from unshackle.core.utilities import log_event


class BlowfishStripe:
    """Blowfish CBC stripe cipher: the service encrypts every third full 2048-byte chunk and leaves the rest clear.

    Each encrypted chunk is its own CBC run with a fixed IV. The service gives the content key, so there is no licence.
    """

    CHUNK = 2048
    IV = bytes(range(8))

    def __init__(self, key: Union[bytes, str]):
        self.key: bytes = bytes.fromhex(key) if isinstance(key, str) else key

    def decrypt_chunk(self, chunk: bytes) -> bytes:
        """Decrypt one encrypted chunk."""
        return Blowfish.new(self.key, Blowfish.MODE_CBC, iv=self.IV).decrypt(chunk)  # nosec B304

    def decrypt(self, path: Path) -> None:
        """Decrypt the file to a new file that then replaces it. A short trailing chunk stays clear."""
        if not path or not path.exists():
            raise ValueError("Tried to decrypt a file that does not exist.")
        log_event(
            "drm_decrypt",
            level="DEBUG",
            message=f"Decrypting {path.name} with the Blowfish stripe cipher",
            drm_type="BlowfishStripe",
            file=path.name,
        )
        decrypted_path = path.with_suffix(f".decrypted{path.suffix}")
        with path.open("rb") as src, decrypted_path.open("wb") as dst:
            while stripe := src.read(self.CHUNK * 3):
                if len(stripe) >= self.CHUNK:
                    stripe = self.decrypt_chunk(stripe[: self.CHUNK]) + stripe[self.CHUNK :]
                dst.write(stripe)
        decrypted_path.replace(path)


__all__ = ("BlowfishStripe",)
