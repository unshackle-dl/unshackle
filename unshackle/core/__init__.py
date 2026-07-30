import hashlib
import os
from pathlib import Path
from typing import Callable, Optional

__version__ = "5.3.0"

_PKG = Path(__file__).parent.parent
# Framework code only. Services are user-swappable, so they are not part of the identity.
_CODE_DIRS = ("core", "commands", "utils", "vaults")


def _raise(error: OSError) -> None:
    raise error


def code_files() -> list[str]:
    """Framework source paths relative to the package root, in a platform-stable order."""
    rels = ["__main__.py"]
    for name in _CODE_DIRS:
        for root, dirs, files in os.walk(_PKG / name, onerror=_raise):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            rel = os.path.relpath(root, _PKG)
            rels.extend(os.path.join(rel, f).replace(os.sep, "/") for f in files if f.endswith(".py"))
    return sorted(rels)


def code_hash(
    files: Optional[list[str]] = None,
    read: Optional[Callable[[str], bytes]] = None,
) -> str:
    """
    md5 of the framework source. Identifies the running code, not the release it claims to be.

    Pass `files` and `read` to digest a different byte source, such as blobs from git history
    (see tools/resolve_code_hash.py). Returns "" when the source cannot be read.
    """
    if read is None:

        def read(rel: str) -> bytes:
            return (_PKG / rel).read_bytes()

    digest = hashlib.md5(usedforsecurity=False)
    try:
        for rel in code_files() if files is None else files:
            digest.update(rel.encode())
            digest.update(read(rel))
    except OSError:
        return ""
    return digest.hexdigest()


__code_hash__ = code_hash()[:7]
