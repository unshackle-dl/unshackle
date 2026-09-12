from pathlib import Path
from typing import Any, Callable, Optional
from uuid import UUID

DECRYPT_HOOK: Optional[Callable[..., None]] = None


def decrypt_track(
    drm: Any,
    path: Path,
    licence: Optional[Callable] = None,
    track_kid: Optional[UUID] = None,
    decrypt: bool = True,
) -> None:
    """Decrypt ``path`` with ``drm`` through the installed hook, or directly when there is none.

    ``decrypt=False`` means the downloader already decrypted the segments in place; the hook
    then only checks the result, because there is no ciphertext left to retry with.
    """
    if DECRYPT_HOOK:
        DECRYPT_HOOK(drm, path, licence, track_kid, decrypt)
    elif decrypt:
        drm.decrypt(path)
