"""Whole-file CENC decryption with mp4decrypt or shaka-packager, shared by Widevine, PlayReady and ClearKeyCENC."""

import logging
import shutil
import subprocess
import textwrap
from pathlib import Path
from time import monotonic
from typing import Any, Callable
from uuid import UUID

from rich.text import Text

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.drm.key_args import shaka_noise
from unshackle.core.drm.verify import (
    decrypter_for,
    has_encrypted_sample_entry,
    kidless_track_kids,
    read_top_level_box,
)
from unshackle.core.utilities import log_event

log = logging.getLogger(__name__)


def mp4decrypt_or_shaka(drm: Any, path: Path) -> None:
    """Decrypt ``path`` with mp4decrypt, and again with shaka-packager when mp4decrypt copied it through.

    mp4decrypt copies a track it has no content key for unchanged, so its first moof stays the same.
    """
    first_moof = read_top_level_box(path, b"moof")
    drm.decrypt_with_mp4decrypt(path)
    if has_encrypted_sample_entry(path) and read_top_level_box(path, b"moof") == first_moof:
        log.warning(f"mp4decrypt left {path.name} encrypted, decrypting it with shaka-packager")
        drm.decrypt_with_shaka_packager(path)


def decrypt_cenc(drm: Any, path: Path, drm_type: str) -> None:
    """Decrypt ``path`` in place with the decrypter ``decrypter_for`` picks, using the content keys of ``drm``."""
    if not drm.content_keys:
        raise ValueError("Cannot decrypt a Track without any Content Keys...")

    if not path or not path.exists():
        raise ValueError("Tried to decrypt a file that does not exist.")

    decrypter = decrypter_for(path)
    tool = "mp4decrypt" if decrypter == "mp4decrypt" else "shaka-packager"

    log_event(
        "drm_decrypt",
        level="DEBUG",
        message=f"Decrypting {path.name} with {tool}",
        drm_type=drm_type,
        tool=tool,
        file=path.name,
        key_count=len(drm.content_keys),
    )

    decrypt_start = monotonic()
    if decrypter == "mp4decrypt":
        mp4decrypt_or_shaka(drm, path)
    else:
        drm.decrypt_with_shaka_packager(path)

    log_event(
        "drm_decrypt_complete",
        level="DEBUG",
        message=f"Decrypted {path.name} with {tool}",
        drm_type=drm_type,
        tool=tool,
        file=path.name,
        duration_ms=round((monotonic() - decrypt_start) * 1000, 1),
        output_size=path.stat().st_size if path.exists() else 0,
    )


def mp4decrypt(path: Path, key_args_for: Callable[[dict[int, UUID]], list[str]]) -> None:
    """Decrypt ``path`` in place with mp4decrypt, taking the ``--key`` arguments from ``key_args_for(track_kids)``."""
    if not binaries.Mp4decrypt:
        raise EnvironmentError("mp4decrypt executable not found but is required.")

    output_path = path.with_stem(f"{path.stem}_decrypted")
    key_args = key_args_for(kidless_track_kids(read_top_level_box(path, b"moov") or b""))
    cmd = [str(binaries.Mp4decrypt), "--show-progress", *key_args, str(path), str(output_path)]

    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr if e.stderr else f"mp4decrypt failed with exit code {e.returncode}"
        raise subprocess.CalledProcessError(e.returncode, cmd, output=e.stdout, stderr=error_msg)

    if not output_path.exists():
        raise RuntimeError(f"mp4decrypt failed: output file {output_path} was not created")
    if output_path.stat().st_size == 0:
        raise RuntimeError(f"mp4decrypt failed: output file {output_path} is empty")

    path.unlink()
    shutil.move(output_path, path)


def shaka_decrypt(path: Path, keys: str, label: str) -> None:
    """Decrypt ``path`` in place with shaka-packager and its ``--keys`` value ``keys``.

    Only a non-zero exit is a failure; its non-noise log lines go to the console under ``[label]``.
    """
    if not binaries.ShakaPackager:
        raise EnvironmentError("Shaka Packager executable not found but is required.")

    output_path = path.with_stem(f"{path.stem}_decrypted")
    config.directories.temp.mkdir(parents=True, exist_ok=True)
    arguments: list[str | Path] = [
        f"input={path},stream=0,output={output_path},output_format=MP4",
        "--enable_raw_key_decryption",
        "--keys",
        keys,
        "--use_fake_clock_for_muxer",
        "--temp_dir",
        config.directories.temp,
    ]

    try:
        p = subprocess.Popen(
            [binaries.ShakaPackager, *arguments],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        stream_skipped = False
        shaka_log_buffer = ""
        assert p.stderr is not None
        for line in iter(p.stderr.readline, ""):
            line = line.strip()
            if not line:
                continue
            if "Skip stream" in line:
                # file/segment was so small that it didn't have any actual data, ignore
                stream_skipped = True
            if shaka_noise(line):
                continue
            shaka_log_buffer += f"{line}\n"

        p.wait()

        if p.returncode != 0:
            if shaka_log_buffer:
                shaka_log_buffer = "\n            ".join(
                    textwrap.wrap(shaka_log_buffer.rstrip(), width=console.width - 22, initial_indent="")
                )
                console.log(Text.from_ansi(f"\n[{label}]: " + shaka_log_buffer))
            raise subprocess.CalledProcessError(p.returncode, [binaries.ShakaPackager, *arguments])

        path.unlink()
        if not stream_skipped:
            shutil.move(output_path, path)
    except subprocess.CalledProcessError as e:
        if e.returncode == 0xC000013A:  # STATUS_CONTROL_C_EXIT
            raise KeyboardInterrupt()
        raise
