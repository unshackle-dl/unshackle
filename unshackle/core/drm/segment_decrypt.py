from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional, Union

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.drm.clearkey_cenc import ClearKeyCENC
from unshackle.core.drm.playready import PlayReady
from unshackle.core.drm.widevine import Widevine

SEGMENT_DRM_T = Union[Widevine, PlayReady, ClearKeyCENC]

MAX_WORKERS = 16


def can_use(drm: Any, decryption: str) -> bool:
    """Whether unshackle can decrypt a track's segments one by one as they download."""
    return (
        isinstance(drm, (Widevine, PlayReady, ClearKeyCENC))
        and str(decryption).lower() == "mp4decrypt"
        and config.decrypt_segments
        and bool(binaries.Mp4decrypt)
    )


class SegmentDecrypter:
    """Decrypts fMP4 segments as they arrive, via mp4decrypt --fragments-info, on a thread pool.

    mp4decrypt cannot read a bare `moof+mdat` segment on its own, so every call passes the
    track's init segment through --fragments-info for the moov it needs. One last call
    decrypts the init segment itself without that flag, which rewrites encv/enca back to the
    real codec 4CC. Concatenating that init segment with the decrypted segments gives the same
    bytes as decrypting the merged file in one pass.
    """

    def __init__(self, drm: SEGMENT_DRM_T, init_data: bytes, work_dir: Path, workers: Optional[int] = None) -> None:
        if not binaries.Mp4decrypt:
            raise EnvironmentError("mp4decrypt executable not found but is required.")

        self.drm = drm
        self.key_args = drm.mp4decrypt_key_args()

        work_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir = Path(tempfile.mkdtemp(prefix="segdec-", dir=work_dir))
        self.init_path = self.work_dir / "init.mp4"
        self.init_path.write_bytes(init_data)

        self.pool = ThreadPoolExecutor(max_workers=min(workers or os.cpu_count() or 4, MAX_WORKERS))
        self.futures: list[Future] = []

    def submit(self, segment: Path) -> None:
        """Queue one downloaded segment for decryption."""
        self.futures.append(self.pool.submit(self.decrypt_segment, segment))

    def decrypt_segment(self, segment: Path) -> None:
        output = segment.with_suffix(segment.suffix + ".dec")
        try:
            self.run(["--fragments-info", str(self.init_path), str(segment), str(output)])
            os.replace(output, segment)
        finally:
            output.unlink(missing_ok=True)

    def finish(self) -> bytes:
        """Wait for every queued segment, then return the decrypted init segment bytes."""
        try:
            for future in self.futures:
                future.result()
            decrypted_init = self.work_dir / "init_decrypted.mp4"
            self.run([str(self.init_path), str(decrypted_init)])
            return decrypted_init.read_bytes()
        finally:
            self.close()

    def close(self) -> None:
        """Drop any pending work and remove the temporary files. Safe to call twice."""
        for future in self.futures:
            future.cancel()
        self.futures.clear()
        self.pool.shutdown(wait=True)
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def run(self, args: list[str]) -> None:
        cmd = [str(binaries.Mp4decrypt), *self.key_args, *args]
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"mp4decrypt failed on {args[-2]}: {e.stderr.strip() if e.stderr else f'exit code {e.returncode}'}"
            ) from e
