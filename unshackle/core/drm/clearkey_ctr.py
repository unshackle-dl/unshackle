from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from Cryptodome.Cipher import AES

from unshackle.core import binaries
from unshackle.core.drm.paged_aac import PAGED_AAC, cut, to_adts
from unshackle.core.utilities import log_event
from unshackle.core.utils.subprocess import run_step

log = logging.getLogger(__name__)


class ClearKeyCTR:
    """AES-128-CTR over a whole file: the service gives the IV and returns the content key through ``licence``.

    The key resolver asks ``licence`` at the licence step of a download or of a `--skip-dl` run, so listing tracks costs no request.
    A cipher built with ``key`` holds its content key from the start.
    ``magic`` is the signature the decrypted file must start with; a mismatch raises and keeps the input.
    ``skip`` drops that many decrypted bytes from the start of the file: a header that sits in front of the media.
    ``rewrite_paged`` rewrites a decrypted file in the paged AAC container (``PAGED_AAC``).
    """

    def __init__(
        self,
        licence: Callable[[], bytes | str] | None,
        iv: bytes | str,
        magic: bytes | str,
        skip: int = 0,
        key: bytes | str | None = None,
    ) -> None:
        self.iv: bytes = bytes.fromhex(iv) if isinstance(iv, str) else iv
        self.magic: bytes = bytes.fromhex(magic) if isinstance(magic, str) else magic
        if len(self.iv) != 16:
            raise ValueError(f"ClearKeyCTR needs a 16-byte IV, got {len(self.iv)}")
        if not self.magic:
            raise ValueError("ClearKeyCTR needs the signature of the decrypted file")
        if skip < 0:
            raise ValueError(f"ClearKeyCTR needs a skip of 0 or more, got {skip}")
        self.licence: Callable[[], bytes | str] | None = licence
        self.skip: int = skip
        self.key: bytes | None = None
        if key is not None:
            self.take_key(key)

    def take_key(self, key: bytes | str) -> bytes:
        """Hold and return a content key: the one the licence callback returned, or one another source licensed."""
        key = bytes.fromhex(key) if isinstance(key, str) else key
        if len(key) != 16:
            raise ValueError(f"ClearKeyCTR needs a 16-byte key, got {len(key)}")
        self.key = key
        return key

    def content_key(self) -> bytes:
        """The content key, licensed on first use."""
        if self.key is not None:
            return self.key
        if self.licence is None:
            raise ValueError("ClearKeyCTR holds no content key: none was licensed")
        return self.take_key(self.licence())

    def decrypt(self, path: Path) -> None:
        """Decrypt the file to a new file that then replaces it.

        The dropped header goes through the cipher too, so the keystream stays aligned with the file.
        """
        if not path or not path.exists():
            raise ValueError("Tried to decrypt a file that does not exist.")
        key = self.content_key()
        log_event(
            "drm_decrypt",
            level="DEBUG",
            message=f"Decrypting {path.name} with AES-CTR",
            drm_type="ClearKeyCTR",
            file=path.name,
        )
        cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=int.from_bytes(self.iv, "big"))
        decrypted_path = path.with_suffix(f".decrypted{path.suffix}")
        try:
            with path.open("rb") as src, decrypted_path.open("wb") as dst:
                cipher.decrypt(src.read(self.skip))
                while chunk := src.read(1 << 20):
                    dst.write(cipher.decrypt(chunk))
            with decrypted_path.open("rb") as decrypted:
                start = decrypted.read(len(self.magic))
            if start != self.magic:
                raise ValueError(f"Decrypted {path.name} does not start with {self.magic!r}")
            if self.magic == PAGED_AAC:
                rewrite_paged(decrypted_path)
        except BaseException:
            decrypted_path.unlink(missing_ok=True)
            raise
        decrypted_path.replace(path)


def rewrite_paged(path: Path) -> None:
    """Replace a decrypted paged AAC file with the M4A file MP4Box makes of it, or with one ADTS stream.

    The M4A file declares HE-AACv2, and its edit list skips the coder delay and ends at the sample count of the header
    page. Without MP4Box, or when its step fails, the file is one ADTS stream, which declares AAC-LC.
    """
    data = path.read_bytes()
    stream = to_adts(data)
    tool, timing = binaries.MP4Box, cut(data)
    if tool and timing:
        delay, true, rate = timing
        adts, output = path.with_suffix(".paged.aac"), path.with_suffix(".paged.m4a")
        try:
            adts.write_bytes(stream)
            run_step(
                [
                    tool,
                    "-for-test",
                    "--moovts=-1",
                    "-add",
                    f"{adts}:psx",
                    "-edits",
                    f"1=re0-{true}/{rate},{delay}/{rate}",
                    "-new",
                    output,
                ],
                output=output,
                label="paged AAC to M4A",
            )
            output.replace(path)
            return
        except (OSError, RuntimeError) as error:
            log.warning(f"MP4Box failed on {path.name}, wrote one ADTS stream")
            log.debug(error)
        finally:
            adts.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
    if not tool:
        log.debug(f"MP4Box not found, {path.name} stays one ADTS stream")
    path.write_bytes(stream)


def whole_file_cipher(track: Any) -> ClearKeyCTR | None:
    """The whole-file cipher of a track, with or without a content key."""
    return next((d for d in getattr(track, "drm", None) or [] if isinstance(d, ClearKeyCTR)), None)
