import hashlib
import os
from pathlib import Path
from typing import Callable, Optional

from rich_click.patch import patch as _patch_click_help

_patch_click_help()

__version__ = "5.4.0"

_PKG = Path(__file__).parent.parent
# Framework code only. Services are user-swappable, so they are not part of the identity.
_CODE_DIRS = ("core", "commands", "utils", "vaults")


def _raise(error: OSError) -> None:
    raise error


def code_files() -> list[str]:
    """Framework source paths relative to the package root, in a platform-stable order."""
    pkg = str(_PKG)
    rels = ["__main__.py"]
    for name in _CODE_DIRS:
        for root, dirs, files in os.walk(os.path.join(pkg, name), onerror=_raise):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            rel = os.path.relpath(root, pkg).replace(os.sep, "/")
            rels.extend(f"{rel}/{f}" for f in files if f.endswith(".py"))
    return sorted(rels)


def code_hash(
    files: Optional[list[str]] = None,
    read: Optional[Callable[[str], bytes]] = None,
) -> str:
    """
    sha1 of the framework source. Two installs on the same version differ here if their code does.

    Pass `files` and `read` to digest a different byte source, such as blobs from git history
    (see tools/resolve_code_hash.py). Returns "" when the source cannot be read.
    """
    if read is None:
        pkg = str(_PKG)

        def read(rel: str) -> bytes:
            with open(os.path.join(pkg, rel), "rb") as fh:
                return fh.read()

    digest = hashlib.sha1(usedforsecurity=False)
    try:
        for rel in code_files() if files is None else files:
            digest.update(rel.encode())
            digest.update(read(rel))
    except OSError:
        return ""
    return digest.hexdigest()


__code_hash__ = code_hash()[:7]
